#!/usr/bin/env python3
"""
process_receipts.py  —  Core receipt-processing logic.

Public API:
  initialize_models()          — check LM Studio connectivity
  process_receipts_batch(...)  — full pipeline, returns a summary dict
  extract_receipt_data(...)    — send one image to LM Studio, get structured dict
  generate_spreadsheet(...)    — build the Excel workbook from processed results
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import sys
import time
import concurrent.futures
import threading
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

try:
    import fitz  # PyMuPDF
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

from openai import OpenAI
from PIL import Image, ImageChops, ImageOps

from spreadsheet_theme import build_themed_workbook

# ── Configuration ──────────────────────────────────────────────────────────────
LMSTUDIO_BASE_URL    = os.getenv("LMSTUDIO_BASE_URL",    "http://127.0.0.1:1234/v1")
OLMOCR_MODEL_ID      = os.getenv("OLMOCR_MODEL_ID",      "")
GEMMA_SMALL_MODEL_ID = os.getenv("GEMMA_SMALL_MODEL_ID", "")
GEMMA_LARGE_MODEL_ID = os.getenv("GEMMA_LARGE_MODEL_ID", "")
GEMMA_MODEL_ID       = os.getenv("GEMMA_MODEL_ID",       "")

# Build tag — surfaced in the web UI footer and the workbook footer so you can
# confirm which build is actually running (handy after a `docker compose up`
# that may have reused a stale image). Override at build time with BUILD_TAG.
APP_VERSION = os.getenv("BUILD_TAG", "2026.06.11")
MAX_PARALLEL_REQUESTS = int(os.getenv("MAX_PARALLEL_REQUESTS", "0"))  # 0 = no cap (ThreadPoolExecutor default)
# Per-request timeout (seconds) for the LM Studio / OpenAI client. Without it a
# hung model request blocks a worker thread forever; bounded retries cover
# transient drops. Override via LLM_TIMEOUT.
LLM_TIMEOUT          = float(os.getenv("LLM_TIMEOUT", "120"))
LLM_MAX_RETRIES      = int(os.getenv("LLM_MAX_RETRIES", "2"))
RECEIPTS_FOLDER      = os.getenv("RECEIPTS_FOLDER", "receipts")
OUTPUT_FOLDER        = os.getenv("OUTPUT_FOLDER",   "output")

# ── Single authoritative app-config location ────────────────────────────────────
# The web server, the watch daemon, and the scheduler ALL read and write this one
# file. It is defined here, in the shared module, so there is exactly one source of
# truth — previously server.py and watch_mode.py each recomputed their own path,
# which let a Docker-internal copy and a host-output copy drift apart. It lives
# under the mounted OUTPUT_FOLDER so settings survive container rebuilds.
APP_CONFIG_FILENAME = ".app_config.json"
CONFIG_FILE         = Path(OUTPUT_FOLDER) / APP_CONFIG_FILENAME

IMAGE_EXTENSIONS     = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
PDF_EXTENSIONS       = {".pdf"}
ARCHIVE_EXTENSIONS   = {".zip"}
# Archives are expanded into their member images/PDFs at intake; the members
# (not the archive) are what gets queued, so SUPPORTED_EXTENSIONS — the set the
# pipeline treats as directly processable — deliberately stays images + PDFs.
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS
IMAGE_MAX_PX         = 1568
# Hard cap on pages rendered from a single PDF — a huge or maliciously-crafted
# PDF could otherwise exhaust disk by expanding to thousands of JPEGs. Override
# via PDF_MAX_PAGES.
PDF_MAX_PAGES        = int(os.getenv("PDF_MAX_PAGES", "50"))

# Runtime state — both selectable from the UI
# _active_ocr_model:    empty string = skip dedicated OCR step, use distill model directly
# _active_distill_model: model used for unified extraction + audit
_active_ocr_model:    str = ""           # no dedicated OCR model by default
_active_distill_model: str = ""           # populated by initialize_models() at startup

# Reasoning ("thinking") mode — a single global toggle for OCR + distillation.
# Off by default: receipt extraction is structured JSON at temperature 0, which
# rarely benefits from chain-of-thought and runs slower with it. Toggle from the UI.
_thinking_enabled: bool = False


def _thinking_body(budget: int) -> dict:
    """Return the LM Studio extra_body fragment for the current reasoning mode."""
    if _thinking_enabled:
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {"thinking": {"type": "disabled"}}

# Brand / keyword sets and the known-vendor database live in vendor_db so the
# offline parser can name a real vendor (not the store address) and so the lists
# have a single home. The category-scoring patterns below are built from them.
from vendor_db import FUEL_VENDORS, FUEL_KEYWORDS, MATS_VENDORS, match_vendor


def _kw_pattern(kw: str) -> "re.Pattern[str]":
    """Word-boundary regex for one vendor/keyword match against lowercased text.

    Plain substring matching misfired badly on raw OCR text: "76" matched street
    addresses, store numbers and any price ending in .76, and "gas" matched
    "Las Vegas".  Purely numeric brands ("76") additionally must not touch
    digits, decimal points, '#', '$' or ',' so prices, store numbers, addresses
    and zip codes never count as a fuel-vendor sighting.
    """
    esc = re.escape(kw)
    if kw.isdigit():
        return re.compile(rf"(?<![a-z0-9.,#$]){esc}(?![a-z0-9.,])")
    return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])")


_FUEL_VENDOR_PATTERNS  = {kw: _kw_pattern(kw) for kw in FUEL_VENDORS}
_FUEL_KEYWORD_PATTERNS = {kw: _kw_pattern(kw) for kw in FUEL_KEYWORDS}
_MATS_VENDOR_PATTERNS  = {kw: _kw_pattern(kw) for kw in MATS_VENDORS}

MONTH_MAP: dict[str, int] = {
    "january": 1,   "february": 2,  "march": 3,
    "april": 4,     "may": 5,       "june": 6,
    "july": 7,      "august": 8,    "september": 9,
    "october": 10,  "november": 11, "december": 12,
    "jaunary": 1, "feburary": 2, "jan": 1, "feb": 2, "mar": 3,
    "apr": 4,     "jun": 6,     "jul": 7, "aug": 8, "sep": 9,
    "oct": 10,    "nov": 11,    "dec": 12,
}


# ── Prompts ────────────────────────────────────────────────────────────────────

OLMOCR_RAW_PROMPT = (
    "Transcribe ALL visible text from this receipt image. "
    "Include every word, number, date, total, and label exactly as shown. "
    "Output only the raw transcribed text — no JSON, no formatting, no commentary."
)

# Stage 2 unified distillation — extraction + audit in one call.
# job_name and job_number are intentionally omitted (user provides those manually).
_UNIFIED_DISTILLATION_TEMPLATE = (
    "You are a receipt data extractor and expense auditor. Parse the following raw OCR text "
    "from a receipt and return ONLY a JSON object:\n\n"
    "{{\n"
    '  "date": "YYYY-MM-DD",\n'
    '  "vendor": "store or vendor name",\n'
    '  "amount": 0.00,\n'
    '  "category": "fuel | mats | misc",\n'
    '  "expense_description": null,\n'
    '  "summary": "one-sentence description WITHOUT the dollar amount, e.g. Lunch at Butchs Grinders",\n'
    '  "flags": []\n'
    "}}\n\n"
    "Category rules:\n"
    '- "fuel": gas stations (Shell, Chevron, Arco, Mobil, 76, Valero, etc.)\n'
    '  → set expense_description = null\n'
    '- "mats": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies\n'
    '- "misc": everything else (restaurants, hotel, meals, phone bills, WiFi, coffee, etc.)\n\n'
    "Field rules:\n"
    "- Use TOTAL or GRAND TOTAL for amount\n"
    "- date must be YYYY-MM-DD\n"
    "- summary: one sentence, vendor and purpose only, do NOT include the dollar amount\n"
    "- Do NOT include job_name or job_number — user provides those manually\n"
    "- flags: JSON array of flag objects for issues:\n"
    '  * fuel > $200  → {{"flag": "Amount exceeds $200 fuel threshold"}}\n'
    '  * mats > $500  → {{"flag": "Amount exceeds $500 mats threshold"}}\n'
    '  * misc > $300  → {{"flag": "Amount exceeds $300 misc threshold"}}\n'
    '  * amount=0, missing vendor, or garbled date → {{"flag": "OCR error: reason"}}\n'
    "  * date outside 6-month window from {today} → "
    '{{"flag": "Date outside 6-month window"}}\n'
    "  * Return [] if no issues\n\n"
    "Return ONLY valid JSON — no markdown, no extra text.\n\n"
    "Receipt OCR text:\n{ocr_text}"
)

# Direct-vision fallback (same schema)
_GEMMA_VISION_TEMPLATE = """\
You are a receipt data extractor and expense auditor. Analyze this receipt image and return ONLY a JSON object:

{{
  "date": "YYYY-MM-DD",
  "vendor": "store or vendor name",
  "amount": 0.00,
  "category": "fuel | mats | misc",
  "expense_description": null,
  "summary": "one-sentence description WITHOUT the dollar amount, e.g. Lunch at Butchs Grinders",
  "flags": []
}}

Category rules:
- "fuel": gas stations (Shell, Chevron, Arco, Mobil, 76, etc.) → expense_description=null
- "mats": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies
- "misc": everything else (restaurants, hotel, meals, phone bills, WiFi, coffee, etc.)

Amount: use TOTAL or GRAND TOTAL.
Date: YYYY-MM-DD from transaction date.
Summary: vendor and purpose only — do NOT include the dollar amount.
Do NOT include job_name or job_number.

flags:
- fuel > $200, mats > $500, misc > $300 → {{"flag": "Amount exceeds threshold"}}
- amount=0, missing vendor, garbled date → {{"flag": "OCR error: reason"}}
- date outside 6-month window from {today} → {{"flag": "Date outside 6-month window"}}
Return [] if no issues.

Return ONLY valid JSON, no markdown."""


# ── Model management ───────────────────────────────────────────────────────────

def _api_base() -> str:
    return LMSTUDIO_BASE_URL.rstrip("/").removesuffix("/v1")


def _fuzzy_match(model_id: str, loaded_ids: list[str]) -> bool:
    key = re.sub(r"[-_/]", "", model_id.lower())
    for mid in loaded_ids:
        if key in re.sub(r"[-_/]", "", mid.lower()):
            return True
    return False


def _try_load_model(model_id: str) -> bool:
    base = _api_base()
    try:
        req = urllib.request.Request(
            f"{base}/v1/models", headers={"Authorization": "Bearer lmstudio"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            loaded = [m["id"] for m in data.get("data", [])]
            if _fuzzy_match(model_id, loaded):
                return True
    except Exception:
        pass
    try:
        payload = json.dumps({"identifier": model_id}).encode()
        req = urllib.request.Request(
            f"{base}/api/v0/models/load", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status == 200
    except Exception:
        return False


def list_available_models() -> list[str]:
    """Return all model IDs currently loaded in LM Studio."""
    base = _api_base()
    try:
        req = urllib.request.Request(
            f"{base}/v1/models", headers={"Authorization": "Bearer lmstudio"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


def _looks_like_chat_model(model_id: str) -> bool:
    """Heuristic: exclude embedding / audio / reranker models from auto-selection."""
    low = model_id.lower()
    return not any(tag in low for tag in
                   ("embed", "bge-", "rerank", "whisper", "tts", "clip", "vae"))


def initialize_models() -> str:
    """Check LM Studio connectivity and adopt whatever model is loaded.

    If the configured distill model isn't actually loaded, fall back to a loaded
    Gemma if present, otherwise the first loaded chat-capable model — so the app
    works out of the box with whatever the user has running in LM Studio.
    """
    global _active_distill_model
    available = list_available_models()
    if available:
        print(f"[models] {len(available)} model(s) available: {available}")
        if not _active_distill_model or _active_distill_model not in available:
            chosen = (
                next((m for m in available if "gemma" in m.lower()), None)
                or next((m for m in available if _looks_like_chat_model(m)), None)
                or available[0]
            )
            _active_distill_model = chosen
            print(f"[models] Auto-selected loaded model: {chosen}")
    else:
        print("[models] LM Studio not reachable or no models loaded")

    print(f"[models] OCR model: {_active_ocr_model or '(none — using distill model for vision)'}")
    print(f"[models] Distill model: {_active_distill_model}")
    return _active_distill_model


# ── Image encoding ─────────────────────────────────────────────────────────────
#
# CANONICAL PIPELINE ORDER:  greyscale → autocrop → OCR/text extraction → … → compress
#
#   0. greyscale — flatten the stored image to high-contrast grayscale BEFORE any
#                  OCR/LLM (grayscale_image_file), in place, so both the OCR engine
#                  and the vision model read the same cleaned-up file. No-op when
#                  GRAYSCALE_ENABLED is off.
#   1. autocrop  — trim uniform background borders (autocrop_image_file / the
#                  in-memory autocrop_receipt inside encode_image).
#   2. OCR/text  — run extraction (LM Studio vision/OCR or the RapidOCR fallback)
#                  against the autocropped, full-resolution image.
#   3. compress  — DEFERRED to spreadsheet-generation time (compress_result_images
#                  / generate_spreadsheet). Re-encoding/downscaling the stored
#                  file once, at export, keeps OCR reading the sharpest image and
#                  shrinks the output folder and the embedded workbook images in a
#                  single pass. Compression may REWRITE the file with a new suffix
#                  (.png/.jpeg → .jpg); compress_result_images updates the stored
#                  _image_path / _new_filename so later lookups still resolve.
#
# Keeping compression OUT of the per-receipt path means nothing downstream can be
# handed a stale pre-compress path (the old "[Errno 2] No such file or directory"
# bug); the file the worker stores is exactly the file OCR read.

AUTOCROP_ENABLED   = os.getenv("AUTOCROP_ENABLED", "1").lower() not in ("0", "false", "no")
AUTOCROP_MIN_RATIO = 0.40   # skip crop that would keep <40% of the image area
AUTOCROP_MAX_RATIO = 0.95   # skip crop that trims almost nothing
AUTOCROP_MARGIN    = 0.02   # safety margin re-added around the detected bbox
_AUTOCROP_THRESHOLD = 24    # min grayscale delta from background to count as content

# Stored-image compression — re-encode every saved receipt to an optimized JPEG
# so phone photos don't bloat the output folder or the embedded workbook images.
# All three are runtime-adjustable from the web UI (Settings → Image processing).
COMPRESS_ENABLED = os.getenv("COMPRESS_ENABLED", "1").lower() not in ("0", "false", "no")
JPEG_QUALITY     = int(os.getenv("JPEG_QUALITY", "85"))    # 40 (smaller) … 95 (sharper)
STORE_MAX_PX     = int(os.getenv("STORE_MAX_PX", "2000"))  # cap the longest side of stored images

# Greyscale (black-&-white) pre-pass — convert receipts to high-contrast grayscale
# BEFORE OCR/LLM. Phone photos of receipts carry colour tints, shadows and uneven
# lighting that hurt OCR; flattening to autocontrasted grayscale gives both the OCR
# engine and the vision model a cleaner image to read. Applied in place so every
# later step (OCR, distillation, rename, compression, the embedded workbook image,
# the web preview) finds the same converted file — no extra path to track.
GRAYSCALE_ENABLED = os.getenv("GRAYSCALE_ENABLED", "1").lower() not in ("0", "false", "no")


def autocrop_receipt(img: Image.Image) -> Image.Image:
    """Trim uniform background borders around a receipt photo.

    Conservative by design: returns the original image unchanged whenever the
    detected crop is suspiciously aggressive (<40% of the area kept), trims
    almost nothing, or detection fails for any reason.
    """
    if not AUTOCROP_ENABLED:
        return img
    try:
        gray = img.convert("L")
        w, h = gray.size
        if w < 64 or h < 64:
            return img
        # Background estimated from the median of the four 8x8 corner patches
        samples = []
        for box in ((0, 0, 8, 8), (w - 8, 0, w, 8),
                    (0, h - 8, 8, h), (w - 8, h - 8, w, h)):
            samples.extend(gray.crop(box).tobytes())
        samples.sort()
        bg = samples[len(samples) // 2]

        diff = ImageChops.difference(gray, Image.new("L", gray.size, bg))
        mask = diff.point(lambda p: 255 if p > _AUTOCROP_THRESHOLD else 0)
        bbox = mask.getbbox()
        if not bbox:
            return img

        mx = int(w * AUTOCROP_MARGIN)
        my = int(h * AUTOCROP_MARGIN)
        left   = max(0, bbox[0] - mx)
        top    = max(0, bbox[1] - my)
        right  = min(w, bbox[2] + mx)
        bottom = min(h, bbox[3] + my)

        kept = ((right - left) * (bottom - top)) / float(w * h)
        if kept < AUTOCROP_MIN_RATIO or kept > AUTOCROP_MAX_RATIO:
            return img
        return img.crop((left, top, right, bottom))
    except Exception:
        return img


def autocrop_image_file(path: Path) -> bool:
    """Auto-crop a stored receipt image in place. Returns True if cropped."""
    try:
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            img = raw.convert("RGB")
            cropped = autocrop_receipt(img)
            if cropped.size == img.size:
                return False
            if path.suffix.lower() in (".jpg", ".jpeg"):
                cropped.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            else:
                cropped.save(path)
        return True
    except Exception:
        return False


def grayscale_image_file(path: Path) -> bool:
    """Convert a stored receipt image to high-contrast grayscale, in place.

    A pre-OCR pass: phone receipts are often tinted, shadowed, or low-contrast,
    which trips up both the OCR engine and the vision model.  Converting to an
    autocontrasted single channel sharpens the text without the harsh artefacts of
    a hard 1-bit threshold (which would also hurt the embedded receipt image).

    The file keeps its original path and suffix, so every later step — OCR,
    distillation, autocrop, rename, deferred compression, the workbook image, and
    the web preview — still finds it exactly where it was.  Returns True when the
    file was rewritten, False when disabled or on any error (best-effort: a failed
    conversion must never block extraction).
    """
    if not GRAYSCALE_ENABLED:
        return False
    try:
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            fmt  = (raw.format or "").upper()
            gray = ImageOps.autocontrast(raw.convert("L"), cutoff=1)
        if path.suffix.lower() in (".jpg", ".jpeg"):
            gray.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        elif fmt:
            gray.save(path, format=fmt)
        else:
            gray.save(path)
        return True
    except Exception:
        return False


def compress_image_file(path: Path) -> Path:
    """Re-encode a stored receipt image as an optimized JPEG to shrink its size.

    Honors the runtime JPEG_QUALITY / STORE_MAX_PX settings, downscales oversized
    photos, and converts non-JPEG formats (PNG, HEIC-as-PNG, etc.) to JPEG. Returns
    the path of the resulting file — which may carry a new ``.jpg`` suffix — or the
    original path unchanged when compression is disabled or anything goes wrong.
    """
    if not COMPRESS_ENABLED:
        return path
    try:
        target = path.with_suffix(".jpg")
        with Image.open(path) as raw:
            if getattr(raw, "format", None) == "MPO":
                raw.seek(0)
            img = raw.convert("RGB")
            if max(img.size) > STORE_MAX_PX:
                ratio = STORE_MAX_PX / max(img.size)
                img = img.resize(
                    (round(img.width * ratio), round(img.height * ratio)), Image.LANCZOS,
                )
            img.save(target, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        if target != path and path.exists():
            path.unlink()
        return target
    except Exception:
        return path


def encode_image(path: Path) -> tuple[str, str]:
    raw = Image.open(path)
    if getattr(raw, "format", None) == "MPO":
        raw.seek(0)
    img = raw.convert("RGB")
    if max(img.size) > IMAGE_MAX_PX:
        ratio = IMAGE_MAX_PX / max(img.size)
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


def pdf_to_images(pdf_path: Path, dest_dir: Path) -> list[Path]:
    """Convert each PDF page to a JPEG in dest_dir. Returns list of image paths."""
    if not HAS_PYMUPDF:
        print(f"[pdf] PyMuPDF not installed — skipping {pdf_path.name}")
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    try:
        doc = fitz.open(str(pdf_path))
        # Safety cap: never expand a single PDF to more than PDF_MAX_PAGES JPEGs,
        # so a huge or crafted PDF can't exhaust disk.
        page_count = len(doc)
        n_pages = min(page_count, PDF_MAX_PAGES)
        multi = n_pages > 1
        for i, page in enumerate(doc):
            if i >= PDF_MAX_PAGES:
                print(f"[pdf] {pdf_path.name}: {page_count} pages exceeds cap "
                      f"PDF_MAX_PAGES={PDF_MAX_PAGES} — skipping the remaining "
                      f"{page_count - PDF_MAX_PAGES} page(s)")
                break
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            suffix = f"_p{i + 1}" if multi else ""
            img_path = dest_dir / f"{pdf_path.stem}{suffix}.jpg"
            pix.save(str(img_path))
            out.append(img_path)
        doc.close()
    except Exception as exc:
        print(f"[pdf] Failed to convert {pdf_path.name}: {exc}")
    return out


# Zip-bomb / abuse guards for archive extraction.
ARCHIVE_MAX_FILES = int(os.getenv("ARCHIVE_MAX_FILES", "1000"))             # member cap
ARCHIVE_MAX_BYTES = int(os.getenv("ARCHIVE_MAX_BYTES", str(1024 ** 3)))     # 1 GiB uncompressed cap


class _ArchiveTooLarge(Exception):
    """Internal sentinel — decompressed output exceeded ARCHIVE_MAX_BYTES."""


def extract_archive(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Extract the supported image/PDF members of a .zip into dest_dir (flattened).

    Returns the list of extracted file paths (images and PDFs), ready to flow
    through the same queueing path as a directly-uploaded file.  Designed to be
    safe against hostile archives:

      * Zip-slip — every member is written under its *basename* only, so entries
        like ``../../etc/x`` or absolute paths can't escape dest_dir.
      * Zip-bomb — extraction stops once ARCHIVE_MAX_FILES members or
        ARCHIVE_MAX_BYTES of actual decompressed bytes have been written.
      * Junk — non-image/PDF members, directories, and dotfiles are skipped, so a
        zip's incidental README/.DS_Store never reaches the queue.

    Best-effort: a corrupt archive or a single bad member is logged and skipped,
    never raised, so one bad upload can't take down the request.
    """
    import zipfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_bytes = 0
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if len(extracted) >= ARCHIVE_MAX_FILES:
                    print(f"[zip] {archive_path.name}: stopped at {ARCHIVE_MAX_FILES}-file cap")
                    break
                if info.is_dir():
                    continue
                name = os.path.basename(info.filename)          # flatten → zip-slip safe
                if not name or name.startswith("."):
                    continue
                if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue

                # Collision-safe target name within dest_dir.
                target = dest_dir / name
                stem, ext = Path(name).stem, Path(name).suffix
                i = 1
                while target.exists():
                    target = dest_dir / f"{stem}_{i}{ext}"
                    i += 1

                try:
                    with zf.open(info) as src, open(target, "wb") as dst:
                        while True:
                            if total_bytes >= ARCHIVE_MAX_BYTES:
                                raise _ArchiveTooLarge()
                            chunk = src.read(64 * 1024)
                            if not chunk:
                                break
                            total_bytes += len(chunk)
                            if total_bytes > ARCHIVE_MAX_BYTES:
                                raise _ArchiveTooLarge()
                            dst.write(chunk)
                except _ArchiveTooLarge:
                    target.unlink(missing_ok=True)
                    print(f"[zip] {archive_path.name}: stopped at {ARCHIVE_MAX_BYTES}-byte cap")
                    break
                except Exception as exc:
                    target.unlink(missing_ok=True)
                    print(f"[zip] {archive_path.name}: skipped member {name}: {exc}")
                    continue
                extracted.append(target)
    except zipfile.BadZipFile:
        print(f"[zip] {archive_path.name}: not a valid zip archive")
    except Exception as exc:
        print(f"[zip] Failed to extract {archive_path.name}: {exc}")
    return extracted


# ── AI extraction ──────────────────────────────────────────────────────────────

def _normalize_flags(flags) -> list[dict]:
    """Coerce flags to a list of {"flag": str} dicts.

    LLMs occasionally return flags as bare strings instead of the expected
    {"flag": "..."} dicts.  Normalise here so every downstream consumer can
    safely call .get("flag") without an AttributeError.
    """
    if not flags:
        return []
    result = []
    for f in flags:
        if isinstance(f, dict):
            result.append(f)
        elif f:
            result.append({"flag": str(f)})
    return result


def _is_low_confidence(data: Optional[dict]) -> bool:
    if data is None:
        return True
    if not data.get("amount"):
        return True
    if not (data.get("vendor") or "").strip():
        return True
    return False


def _has_ocr_flag(data: Optional[dict]) -> bool:
    """True if the distillation model flagged an OCR error in this receipt."""
    if not data:
        return False
    flags = _normalize_flags(data.get("flags") or [])
    return any("ocr error" in (f.get("flag") or "").lower() for f in flags)


def _compute_confidence(data: Optional[dict]) -> tuple[int, str]:
    """Return (0–100 pct, comma-separated missing-field string)."""
    if not data:
        return 0, "no data extracted"
    score = 100
    missing: list[str] = []
    if not (data.get("vendor") or "").strip():
        score -= 35; missing.append("vendor")
    if not data.get("amount"):
        score -= 35; missing.append("amount")
    if not data.get("date"):
        score -= 15; missing.append("date")
    if not data.get("category"):
        score -= 5; missing.append("category")
    for _ in data.get("flags") or []:
        score -= 5
    return max(0, min(100, score)), ", ".join(missing)


def _get_fail_reason(data: Optional[dict]) -> str:
    if data is None:
        return "Model returned no data"
    _, issues = _compute_confidence(data)
    if issues:
        return f"Could not extract: {issues}"
    return "Low-confidence extraction"


def _strip_json(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    return match.group(0) if match else raw


def _extract_raw_ocr(client: OpenAI, image_path: Path, model_id: str) -> Optional[str]:
    """Transcribe a receipt to raw text with an LM Studio OCR/vision model.

    Retained for callers that want an LLM-based OCR pass, but no longer part of
    the default pipeline — local RapidOCR (_extract_local_ocr) is the primary
    text source now.
    """
    try:
        b64, mime = encode_image(image_path)
        thinking_body = _thinking_body(4096)
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": OLMOCR_RAW_PROMPT},
            ]}],
            temperature=0.0, max_tokens=2048,
            frequency_penalty=0.1,
            extra_body={**thinking_body, "repeat_penalty": 1.1},
        )
        text = response.choices[0].message.content.strip()
        return text if text else None
    except Exception as exc:
        print(f"[ocr] Extraction failed for {image_path.name}: {exc}")
        return None


# ── Local OCR fallback (RapidOCR) ───────────────────────────────────────────────
# Local CPU OCR used when the LM Studio OCR stage fails or is unreachable. The
# recognized text feeds the same distillation stage as LM Studio OCR.
#
# RapidOCR runs PaddleOCR's PP-OCR models on onnxruntime: the ONNX weights ship
# inside the wheel (no first-run model download) and there is no paddlepaddle /
# paddlex / langchain / setuptools chain to keep version-aligned — which is what
# made the previous PaddleOCR fallback so brittle inside the slim Docker image.

# Honour the legacy PADDLEOCR_ENABLED name so existing deploys / .env files keep
# toggling the fallback after the engine swap.
LOCAL_OCR_ENABLED = (
    os.getenv("LOCAL_OCR_ENABLED", os.getenv("PADDLEOCR_ENABLED", "1")).lower()
    not in ("0", "false", "no")
)

_ocr_engine = None          # RapidOCR instance, or False after an init failure
_ocr_init_error: str = ""   # last engine-init exception (exposed by /debug/ocr-status)
_ocr_lock = threading.Lock()


def _import_rapidocr():
    """Return the RapidOCR class from whichever package is installed.

    Prefer the mature, self-contained ``rapidocr-onnxruntime`` (PP-OCR models
    bundled in the wheel, onnxruntime backend, no runtime download); fall back to
    the newer unified ``rapidocr`` package when that's the one present.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        from rapidocr import RapidOCR  # newer unified package
    return RapidOCR


def _get_ocr_engine():
    """Lazy local-OCR (RapidOCR) singleton. Returns None when disabled/unavailable."""
    global _ocr_engine, _ocr_init_error
    if not LOCAL_OCR_ENABLED:
        return None
    if _ocr_engine is not None:
        return _ocr_engine or None
    with _ocr_lock:
        if _ocr_engine is not None:
            return _ocr_engine or None
        try:
            RapidOCR = _import_rapidocr()
            _ocr_engine = RapidOCR()
            _ocr_init_error = ""
            print("[ocr] RapidOCR fallback engine initialised")
        except Exception as exc:
            print(f"[ocr] RapidOCR unavailable: {exc}")
            _ocr_engine = False
            _ocr_init_error = str(exc)
    return _ocr_engine or None


def _reset_ocr_engine_failure() -> None:
    """Clear a cached engine-init failure so the next call retries.

    A failed init is cached (engine = False) to avoid re-paying a slow doomed
    init on every receipt. Diagnostics call this first so a fixed environment
    (package reinstalled) is picked up without a restart; a working engine is
    never discarded.
    """
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is False:
            _ocr_engine = None


def _rapidocr_lines(out) -> list[str]:
    """Pull recognized text out of a RapidOCR result, in the engine's reading
    order (top-to-bottom, left-to-right), one detected box per line.

    Handles both APIs: the mature rapidocr-onnxruntime returns ``(result,
    elapse)`` where ``result`` is a list of ``[box, text, score]``; the newer
    unified ``rapidocr`` package returns an object exposing a ``.txts`` sequence.
    """
    txts = getattr(out, "txts", None)
    if txts is not None:  # newer unified rapidocr package
        return [str(t) for t in txts if t]
    result = out[0] if isinstance(out, tuple) and out else out  # (result, elapse)
    lines: list[str] = []
    for entry in result or []:
        try:
            text = entry[1]
        except (IndexError, TypeError):
            continue
        if text:
            lines.append(str(text))
    return lines


def _extract_local_ocr(image_path: Path) -> Optional[str]:
    """Run the local OCR engine (RapidOCR) on an image, returning recognized
    lines joined by newlines (None when the engine is unavailable or finds nothing)."""
    engine = _get_ocr_engine()
    if engine is None:
        return None
    try:
        out = engine(str(image_path))
        text = "\n".join(_rapidocr_lines(out)).strip()
        return text or None
    except Exception as exc:
        print(f"[ocr] local OCR failed for {image_path.name}: {exc}")
        return None


# ── Per-item step log ──────────────────────────────────────────────────────────

def _append_step(
    steps: Optional[list],
    step: str,
    label: str,
    detail: str = "",
    *,
    ok: bool = True,
    duration_s: float = 0.0,
) -> None:
    """Append one processing step to the per-item step log. No-op when steps is None."""
    if steps is None:
        return
    steps.append({
        "step":       step,
        "label":      label,
        "detail":     detail,
        "ok":         ok,
        "duration_s": round(duration_s, 2),
    })


# ── Offline rule-based distillation ────────────────────────────────────────────
# When LM Studio is disabled/unreachable the OCR text (from RapidOCR) still needs
# to be turned into structured fields. Sending it to the LM Studio distillation
# model would fail too, so receipts that successfully OCR'd would otherwise land in
# "failed". This pure-regex parser is the genuine offline fallback: no model
# required, so an imported image still produces a usable (if lower-confidence)
# result when the AI backend is down.

_DATE_PATTERNS = (
    # ISO  2026-05-01
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    # US   05/01/2026  or 5-1-26
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b"), "mdy"),
    # Month name  May 1, 2026 / 1 May 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "mname"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b"), "dname"),
)


def _find_date_in_text(text: str) -> str:
    """Best-effort extraction of a transaction date as YYYY-MM-DD ('' if none)."""
    def _norm_year(y: int) -> int:
        return y + 2000 if y < 100 else y

    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif kind == "mdy":
                    mo, d, y = int(m.group(1)), int(m.group(2)), _norm_year(int(m.group(3)))
                elif kind == "mname":
                    mo = MONTH_MAP.get(m.group(1).lower())
                    d, y = int(m.group(2)), int(m.group(3))
                else:  # dname
                    mo = MONTH_MAP.get(m.group(2).lower())
                    d, y = int(m.group(1)), int(m.group(3))
                if not mo:
                    continue
                return date(y, mo, d).isoformat()
            except (ValueError, TypeError):
                continue
    return ""


_ADDRESS_HINTS = re.compile(
    r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|dr|drive|hwy|"
    r"highway|ct|court|pkwy|parkway|ste|suite|unit|apt|fl|floor|"
    r"way|plaza|pike|terrace|trail)\b",
    re.IGNORECASE,
)
_STATE_ZIP = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_PHONE = re.compile(r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")


def _looks_like_address(line: str) -> bool:
    """True when a line looks like a street address, phone, website or city/zip —
    i.e. something the vendor name should NOT be pulled from."""
    s = line.strip()
    low = s.lower()
    if low.startswith(("http", "www.")) or ".com" in low or "@" in low:
        return True
    if _PHONE.search(s) or _STATE_ZIP.search(s):
        return True
    # "123 Main St", "1700 W 7th Ave" — leading street number plus a street word.
    if re.match(r"^\s*\d{1,6}\s+\w", s) and _ADDRESS_HINTS.search(s):
        return True
    return False


def _guess_vendor_line(ocr_text: str) -> str:
    """Pick the most plausible business-name line from OCR text.

    Used only when no known brand matched. Scans the top of the receipt (where
    the name almost always sits), skips address/phone/website/total/number lines,
    and prefers short ALL-CAPS or Title-Case lines that read like a name.
    """
    best_line, best_score = "", -1.0
    for idx, raw in enumerate(ocr_text.splitlines()[:8]):
        s = raw.strip()
        if len(s) < 3 or not any(c.isalpha() for c in s):
            continue
        if _looks_like_address(s):
            continue
        letters = sum(c.isalpha() for c in s)
        digits = sum(c.isdigit() for c in s)
        if digits > letters:                       # mostly a number → not a name
            continue
        if re.search(r"\b(total|subtotal|tax|cash|change|visa|debit|credit|"
                     r"balance|amount|receipt|invoice|tel|phone)\b", s, re.IGNORECASE):
            continue
        score = 5.0 - idx                           # earlier lines score higher
        if s.isupper():
            score += 2.0                            # storefront names are often ALL CAPS
        elif s == s.title():
            score += 1.0                            # Title Case also reads like a name
        if 3 <= len(s) <= 40:
            score += 1.0
        if score > best_score:
            best_line, best_score = s[:60], score
    if best_line:
        return best_line
    # Nothing scored — fall back to the first line with letters (legacy behaviour).
    for raw in ocr_text.splitlines():
        s = raw.strip()
        if len(s) >= 3 and any(c.isalpha() for c in s):
            return s[:60]
    return ""


def _local_distill_from_ocr(ocr_text: str) -> Optional[dict]:
    """Rule-based field extraction from raw OCR text — no LLM involved.

    Returns the same schema the LM distillation produces (so the rest of the
    pipeline is unchanged), or None when there isn't enough to work with. Always
    flags the receipt for manual review since fields were parsed heuristically.
    """
    if not ocr_text or not ocr_text.strip():
        return None

    # Prefer the printed grand total; only when no total line exists fall back
    # to the largest money value (some receipts only print the bare number).
    amount = extract_best_total(ocr_text)
    if not amount:
        candidates = extract_candidate_totals(ocr_text)
        amount = max(candidates) if candidates else 0.0

    # Vendor: first try the known-vendor database (handles the common case where
    # the OCR'd name would otherwise lose out to the store address), then fall
    # back to the address-skipping line heuristic.
    matched = match_vendor(ocr_text)
    if matched:
        vendor, matched_category = matched
    else:
        vendor, matched_category = _guess_vendor_line(ocr_text), None

    if not amount or not vendor:
        return None

    if matched_category:
        category = matched_category
    else:
        low = ocr_text.lower()
        if (any(rx.search(low) for rx in _FUEL_VENDOR_PATTERNS.values())
                or any(rx.search(low) for rx in _FUEL_KEYWORD_PATTERNS.values())):
            category = "fuel"
        elif any(rx.search(low) for rx in _MATS_VENDOR_PATTERNS.values()):
            category = "mats"
        else:
            category = "misc"

    return {
        "date":                _find_date_in_text(ocr_text),
        "vendor":              vendor,
        "amount":              amount,
        "category":            category,
        "expense_description": None,
        "ai_summary":          vendor,
        "flags": [{"flag": "Parsed locally without AI (LM Studio unavailable) — verify fields"}],
        "_local_parse": True,
    }


def _unified_distillation(
    client: OpenAI, ocr_text: str, *, _retry: bool = True,
) -> Optional[dict]:
    """Stage 2: distillation model extracts fields + summary + flags from OCR text."""
    today = date.today().isoformat()
    prompt = _UNIFIED_DISTILLATION_TEMPLATE.format(ocr_text=ocr_text, today=today)
    system_msg = {"role": "system", "content": "You are a receipt data extractor. Respond with valid JSON only."}
    user_msg   = {"role": "user", "content": prompt}

    def _parse(raw: str) -> Optional[dict]:
        raw = _strip_json(raw)
        try:
            result = json.loads(raw)
            result["flags"] = _normalize_flags(result.get("flags") or [])
            # normalise "summary" field name to "ai_summary" used downstream
            if "summary" in result and "ai_summary" not in result:
                result["ai_summary"] = result.pop("summary")
            return result
        except json.JSONDecodeError:
            return None

    thinking_body = _thinking_body(8192)
    try:
        resp = client.chat.completions.create(
            model=_active_distill_model,
            messages=[system_msg, user_msg],
            temperature=0.0, max_tokens=1024,
            frequency_penalty=0.15,
            extra_body={**thinking_body, "repeat_penalty": 1.1},
        )
        result = _parse(resp.choices[0].message.content.strip())
        if result is not None:
            return result
        if _retry:
            print(f"[distill] JSON parse failed, retrying…")
            r2 = client.chat.completions.create(
                model=_active_distill_model,
                messages=[system_msg, user_msg,
                          {"role": "user", "content": "Return ONLY the JSON object — no extra text, no markdown."}],
                temperature=0.0, max_tokens=1024,
                frequency_penalty=0.15,
                extra_body={**thinking_body, "repeat_penalty": 1.1},
            )
            return _parse(r2.choices[0].message.content.strip())
    except Exception as exc:
        print(f"[distill] Exception: {exc}")
    return None


def _extract_with_model(
    client: OpenAI, image_path: Path, model_id: str, *, _retry: bool = True,
) -> Optional[dict]:
    """Direct-vision extraction — used as the sole path when no OCR model is set,
    and as fallback when OCR + distillation yields low-confidence results."""
    today = date.today().isoformat()
    prompt = _GEMMA_VISION_TEMPLATE.replace("{today}", today)

    def _parse(raw: str) -> Optional[dict]:
        raw = _strip_json(raw)
        try:
            result = json.loads(raw)
            result["flags"] = _normalize_flags(result.get("flags") or [])
            if "summary" in result and "ai_summary" not in result:
                result["ai_summary"] = result.pop("summary")
            return result
        except json.JSONDecodeError:
            return None

    thinking_body = _thinking_body(8192)
    try:
        b64, mime = encode_image(image_path)
        system_msg = {"role": "system", "content": "You are a receipt data extractor. Always respond with valid JSON only."}
        user_msg   = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]}
        resp = client.chat.completions.create(
            model=model_id, messages=[system_msg, user_msg],
            temperature=0.0, max_tokens=1024,
            frequency_penalty=0.15,
            extra_body={**thinking_body, "repeat_penalty": 1.1},
        )
        result = _parse(resp.choices[0].message.content.strip())
        if result is not None:
            return result
        if _retry:
            print(f"[extract] JSON parse failed for {image_path.name}, retrying…")
            r2 = client.chat.completions.create(
                model=model_id,
                messages=[system_msg, user_msg,
                          {"role": "user", "content": "Your response was not valid JSON. Return ONLY the JSON object."}],
                temperature=0.0, max_tokens=1024,
                frequency_penalty=0.15,
                extra_body={**thinking_body, "repeat_penalty": 1.1},
            )
            return _parse(r2.choices[0].message.content.strip())
    except Exception as exc:
        print(f"[extract] Exception for {image_path.name}: {exc}")
    return None


def _extract_receipt_with_status(
    client: OpenAI,
    image_path: Path,
    status_cb: Optional[Callable],  # (status, data, model) → None
    step_log: Optional[list] = None,
) -> Optional[dict]:
    """
    OCR-first pipeline with Kanban status callbacks and per-item step logging:

      1. PRIMARY  — local RapidOCR transcribes the receipt text (fast, offline).
      2. DISTILL  — the LM Studio model structures that text into fields. If the
                    LLM is unreachable, an offline rule-based parser fills the
                    fields and flags the receipt for manual review.
      3. RESCUE   — only when OCR produced no usable text (or distillation came
                    back low-confidence) does a vision-capable model read the
                    image directly.

    Each branch is recorded in step_log (if provided) for the per-item process log.
    """
    # Black-&-white pre-pass — runs BEFORE any OCR/LLM call. Converts the stored
    # receipt to high-contrast grayscale in place so both the OCR engine (which
    # reads the file directly) and the vision model (via encode_image) get the
    # cleaner image. In-place, suffix preserved → no downstream path changes.
    grayscale_image_file(image_path)

    def _cb(status: str, data: Optional[dict] = None, model: str = ""):
        if status_cb:
            status_cb(status, data, model)

    t_start = time.perf_counter()

    def _finish(data: Optional[dict], ocr_seconds: float = 0.0,
                distill_seconds: float = 0.0) -> Optional[dict]:
        if data is not None:
            data["_proc_seconds"] = round(time.perf_counter() - t_start, 1)
            if ocr_seconds:
                data["_ocr_seconds"] = round(ocr_seconds, 1)
            if distill_seconds:
                data["_distill_seconds"] = round(distill_seconds, 1)
            if step_log is not None:
                data["_steps"] = list(step_log)
        return data

    def _distill_text(ocr_text: str, ocr_seconds: float,
                      engine: str = "") -> Optional[dict]:
        _cb("distilling", model=_active_distill_model)
        t_distill = time.perf_counter()
        data = _unified_distillation(client, ocr_text)
        distill_dur = time.perf_counter() - t_distill
        local_used = False

        if data is None:
            # LM Studio distillation unreachable/failed → record failure, try offline parser
            _append_step(step_log, "distillation", "Distillation",
                         f"{_active_distill_model} – no response",
                         ok=False, duration_s=distill_dur)
            data = _local_distill_from_ocr(ocr_text)
            local_used = data is not None
            if local_used:
                _append_step(step_log, "local_parse", "Local parse",
                             "offline regex parser (LM Studio unavailable)", ok=True)

        # Ground the model's amount in the OCR text it was given: a value that
        # appears nowhere in the text is replaced by the printed grand total.
        if data is not None and not local_used:
            note = reconcile_amount(data, ocr_text)
            if note:
                data["flags"] = _normalize_flags(data.get("flags") or []) + [{"flag": note}]
                _append_step(step_log, "reconcile", "Amount check", note)

        distill_seconds = time.perf_counter() - t_distill
        if _is_low_confidence(data):
            if not local_used and data is not None:
                # LM Studio responded but result was too sparse to use
                _append_step(step_log, "distillation", "Distillation",
                             f"{_active_distill_model} – incomplete extraction",
                             ok=False, duration_s=distill_dur)
            return None

        # Result passes confidence check — record success if LM Studio handled it
        if not local_used:
            _append_step(step_log, "distillation", "Distillation",
                         _active_distill_model or "", ok=True, duration_s=distill_dur)
        data["_raw_ocr"] = ocr_text
        if engine:
            data["_ocr_engine"] = engine
        return _finish(data, ocr_seconds, distill_seconds)

    try:
        # PRIMARY: local RapidOCR text extraction — fast, runs offline, and is the
        # default path for every receipt now.
        _cb("ocr", model="rapidocr")
        t_ocr = time.perf_counter()
        ocr_text = _extract_local_ocr(image_path)
        ocr_seconds = time.perf_counter() - t_ocr
        if ocr_text:
            _append_step(step_log, "local_ocr", "OCR (RapidOCR)",
                         "primary OCR", ok=True, duration_s=ocr_seconds)
            # Hand the OCR text to the LLM to structure into fields. If LM Studio
            # is unavailable, _distill_text falls back to the offline rule-based
            # parser, which flags the receipt for manual review.
            data = _distill_text(ocr_text, ocr_seconds, engine="rapidocr")
            if data is not None:
                return data
            print(f"[extract] OCR+distill low-confidence for {image_path.name}, "
                  "trying direct vision")
        else:
            _append_step(step_log, "local_ocr", "OCR (RapidOCR)",
                         "no text extracted", ok=False, duration_s=ocr_seconds)

        # RESCUE: OCR found nothing usable (or distillation was low-confidence) —
        # let a vision-capable LLM read the image directly when one is available.
        _cb("distilling", model=_active_distill_model)
        t_distill = time.perf_counter()
        data = _extract_with_model(client, image_path, _active_distill_model)
        vision_dur = time.perf_counter() - t_distill
        if data is not None:
            _append_step(step_log, "vision", "Vision",
                         _active_distill_model or "", ok=True, duration_s=vision_dur)
            return _finish(data, ocr_seconds=ocr_seconds, distill_seconds=vision_dur)
        _append_step(step_log, "vision", "Vision",
                     f"{_active_distill_model} – no response", ok=False, duration_s=vision_dur)
        return None
    except Exception as exc:
        print(f"[extract] Unhandled exception for {image_path.name}: {exc}")
        return None


def extract_receipt_data(client: OpenAI, image_path: Path) -> Optional[dict]:
    """Convenience wrapper without status callbacks."""
    return _extract_receipt_with_status(client, image_path, status_cb=None)


# ── Amount audit (rules-based OCR cross-check) ─────────────────────────────────
# LLMs occasionally hallucinate or mis-copy the total. When raw OCR text is
# available we cross-check the extracted amount against money values that
# appear on total-like lines — pure regex, no model involved.

_TOTAL_KEYWORD_RE = re.compile(
    r"\b(grand\s*total|sub[-\s]?total|subtotal|total\s*due|amount\s*due|"
    r"balance\s*due|total|amount|balance)\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})")

# Lines whose money value IS the receipt's final amount, strongest first.
_GRAND_TOTAL_RE = re.compile(
    r"\b(grand\s*total|total\s*due|amount\s*due|balance\s*due)\b", re.IGNORECASE)
_PLAIN_TOTAL_RE = re.compile(r"\btotal\b", re.IGNORECASE)
_SUBTOTAL_RE    = re.compile(r"\bsub[-\s]?total\b", re.IGNORECASE)
# A "total" line that is really something else (subtotal, tax, tender, change…)
_NON_GRAND_LINE_RE = re.compile(
    r"\b(sub[-\s]?total|subtotal|tax|savings|discount|tender(?:ed)?|tend|"
    r"cash|change|points|rewards?)\b", re.IGNORECASE)


def _money_values(s: str) -> list[float]:
    out = []
    for m in _MONEY_RE.finditer(s):
        try:
            out.append(round(float(m.group(1).replace(",", "")), 2))
        except ValueError:
            pass
    return out


def extract_candidate_totals(text: str) -> list[float]:
    """Money values found on total-like lines of raw receipt text.

    Falls back to every money value in the text when no line mentions a
    total keyword (some receipts only print the bare number).
    """
    if not text:
        return []

    keyword_vals: list[float] = []
    for line in text.splitlines():
        if _TOTAL_KEYWORD_RE.search(line):
            keyword_vals.extend(_money_values(line))
    if keyword_vals:
        return sorted(set(keyword_vals))
    return sorted(set(_money_values(text)))


def extract_best_total(text: str) -> Optional[float]:
    """Best guess at the receipt's printed grand total, or None.

    Tier 1: lines naming the final amount explicitly (GRAND TOTAL, TOTAL DUE,
    AMOUNT DUE, BALANCE DUE).  Tier 2: plain TOTAL lines that aren't really a
    subtotal/tax/tender/change line.  Within a tier the largest value wins
    (e.g. FUEL TOTAL vs. the combined TOTAL on a fuel + car-wash receipt).
    """
    if not text:
        return None
    tier1: list[float] = []
    tier2: list[float] = []
    for line in text.splitlines():
        if _GRAND_TOTAL_RE.search(line):
            tier1.extend(v for v in _money_values(line) if v > 0)
        elif _PLAIN_TOTAL_RE.search(line) and not _NON_GRAND_LINE_RE.search(line):
            tier2.extend(v for v in _money_values(line) if v > 0)
    for vals in (tier1, tier2):
        if vals:
            return max(vals)
    return None


def reconcile_amount(data: Optional[dict], raw_text: str) -> Optional[str]:
    """Replace a hallucinated amount with the receipt's printed grand total.

    The distillation model only ever sees the OCR text, so an extracted amount
    that appears nowhere in that text cannot be a faithful copy of the receipt.
    When the text prints an explicit total line, adopt that value instead.
    Returns a human-readable note (used as a review flag) when the amount was
    changed, or None when it was left alone.
    """
    if not data or not raw_text:
        return None
    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0

    best = extract_best_total(raw_text)

    if amount > 0:
        # Model copied the pre-tax SUBTOTAL? The printed grand total wins.
        if best and best > amount + 0.005:
            sub_vals = [v for line in raw_text.splitlines()
                        if _SUBTOTAL_RE.search(line)
                        for v in _money_values(line)]
            if any(abs(amount - v) < 0.005 for v in sub_vals):
                data["amount"] = best
                return (f"Amount corrected: ${amount:.2f} is the pre-tax subtotal; "
                        f"receipt total is ${best:.2f} — verify")
        candidates = extract_candidate_totals(raw_text)
        if any(abs(amount - c) < 0.005 for c in candidates):
            return None        # amount is printed on the receipt — keep it

        # Last guard before overwriting: only replace an amount that looks like a
        # hallucination. If the model's value appears anywhere among the money
        # values in the raw OCR text, it is a faithful copy — leave it alone.
        # (The subtotal-mismatch case above is the one sanctioned exception.)
        if any(abs(amount - v) < 0.005 for v in _money_values(raw_text)):
            return None

    if not best or abs(best - amount) < 0.005:
        return None

    data["amount"] = best
    if amount > 0:
        return (f"Amount corrected: model said ${amount:.2f} but receipt prints "
                f"total ${best:.2f} — verify")
    return f"Amount taken from printed total ${best:.2f} — verify"


def audit_amount(data: Optional[dict], raw_text: str) -> Optional[str]:
    """Cross-check the model's amount against OCR text.

    Sets data["_amount_verified"] (True/False) and returns a human-readable
    flag string when the amount cannot be found in the OCR text, or None
    when it verifies (or there is nothing to check against).
    """
    if not data or not raw_text:
        return None
    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    candidates = extract_candidate_totals(raw_text)
    if not candidates:
        return None

    if any(abs(amount - c) < 0.005 for c in candidates):
        data["_amount_verified"] = True
        return None

    data["_amount_verified"] = False
    closest = min(candidates, key=lambda c: abs(c - amount))
    return (f"Amount ${amount:.2f} not found in receipt text "
            f"(closest printed total: ${closest:.2f}) — verify manually")


# ── Category classification ────────────────────────────────────────────────────

def classify_category(data: dict) -> str:
    model_cat = (data.get("category") or "misc").lower().strip()
    if model_cat == "materials":
        model_cat = "mats"
    if model_cat not in ("fuel", "mats", "misc"):
        model_cat = "misc"

    vendor   = (data.get("vendor") or "").lower()
    summary  = (data.get("ai_summary") or data.get("summary") or "").lower()
    expense  = (data.get("expense_description") or "").lower()
    raw_ocr  = (data.get("_raw_ocr") or "").lower()
    combined = f"{vendor} {summary} {expense} {raw_ocr}"

    # Fuel: AI-extracted vendor name is strongest signal (+3); fuel vendor found in
    # raw OCR text but not in extracted vendor adds a secondary signal (+2); fuel
    # keywords anywhere in the combined text add +1 each.  All matches are
    # word-bounded (see _kw_pattern) so generic receipt text can't fake a signal.
    # Purely-numeric brands ("76") are too generic in raw OCR — a space-delimited
    # "76" (PUMP 76, 76 MAIN ST, LANE 76) reads as fuel — so they are honoured ONLY
    # when they match the extracted VENDOR field, never the raw OCR text.
    fuel_score = sum(3 for rx in _FUEL_VENDOR_PATTERNS.values() if rx.search(vendor))
    fuel_score += sum(2 for kw, rx in _FUEL_VENDOR_PATTERNS.items()
                      if not kw.isdigit() and rx.search(raw_ocr) and not rx.search(vendor))
    fuel_score += sum(1 for rx in _FUEL_KEYWORD_PATTERNS.values() if rx.search(combined))

    if fuel_score >= 3:
        return "fuel"
    if model_cat == "fuel" and fuel_score >= 1:
        return "fuel"

    if any(rx.search(vendor) for rx in _MATS_VENDOR_PATTERNS.values()):
        return "mats"

    return model_cat


# ── Duplicate detection ────────────────────────────────────────────────────────

def _detect_duplicates(results: list[dict]) -> None:
    seen: dict[tuple, int] = {}
    for i, r in enumerate(results):
        key = (
            (r.get("vendor") or "").lower().strip(),
            r.get("date") or "",
            round(float(r.get("amount") or 0), 2),
        )
        if key[2] == 0:
            continue
        if key in seen:
            first = seen[key]
            if not results[first].get("_flag"):
                results[first]["_flag"] = "Potential duplicate entry"
            if not r.get("_flag"):
                r["_flag"] = f"Duplicate of receipt #{first + 1} (same vendor/date/amount)"
        else:
            seen[key] = i


# ── Photo renaming ─────────────────────────────────────────────────────────────

def sanitize_filename_part(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:40]


def _format_date_mmddyy(raw_date: str) -> str:
    """Convert YYYY-MM-DD (or YYYY-M-D) to MM-DD-YY for filenames."""
    if not raw_date:
        return "unknown"
    # Handle both zero-padded and non-zero-padded dates
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw_date.strip())
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.strftime("%m-%d-%y")
        except ValueError:
            pass
    return sanitize_filename_part(raw_date) or "unknown"


def rename_receipt_image(
    img_path: Path,
    data: dict,
    category: str,
    dest_dir: Optional[Path] = None,
) -> Path:
    """Rename to {category}_{MM-DD-YY}_{Vendor}.ext and optionally move to dest_dir.

    Example: fuel_12-30-24_chevron.jpg
    """
    raw_date   = (data.get("date") or "unknown").strip()
    date_str   = _format_date_mmddyy(raw_date)
    vendor_str = sanitize_filename_part(
        data.get("vendor") or data.get("expense_description") or "receipt"
    )
    cat_str  = category.lower()
    stem     = f"{cat_str}_{date_str}_{vendor_str}" if vendor_str else f"{cat_str}_{date_str}"
    ext      = img_path.suffix.lower()
    out_dir  = dest_dir if dest_dir is not None else img_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    new_path = out_dir / f"{stem}{ext}"

    if new_path.exists() and new_path.resolve() != img_path.resolve():
        counter = 2
        while True:
            candidate = out_dir / f"{stem}_{counter}{ext}"
            if not candidate.exists():
                new_path = candidate
                break
            counter += 1

    if new_path.resolve() != img_path.resolve():
        shutil.move(str(img_path), str(new_path))
    return new_path


# ── Date helpers ───────────────────────────────────────────────────────────────

def sort_key_for_receipt(data: dict) -> date:
    raw = (data.get("date") or "").strip()
    if not raw:
        return date.max
    # Flexible YYYY-M-D parsing (handles missing zero-padding)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # Month-name fallback
    month_num = MONTH_MAP.get(raw.lower())
    if month_num:
        today = date.today()
        year  = today.year if month_num <= today.month else today.year - 1
        return date(year, month_num, 1)
    return date.max


def compute_expense_period(results: list[dict]) -> str:
    dates = []
    for r in results:
        k = sort_key_for_receipt(r)
        if k != date.max:
            dates.append(k)
    if not dates:
        return ""
    fmt = lambda d: d.strftime("%m/%d/%y")
    return f"{fmt(min(dates))} - {fmt(max(dates))}"


# ── Spreadsheet generation ─────────────────────────────────────────────────────

def compress_result_images(results: list[dict], log: Optional[Callable] = None) -> int:
    """Compress each processed receipt image in place.

    This is the deferred *compress* step: instead of re-encoding every receipt
    the moment it is processed, the optimisation now runs once, at spreadsheet
    generation time, so the on-disk output folder and the images embedded in the
    workbook are both shrunk together.

    Idempotent — records are marked with ``_compressed`` so repeat calls (e.g. a
    second export of the same batch) are no-ops.  When compression changes the
    file suffix (``.png`` → ``.jpg``) the ``_image_path`` / ``_new_filename`` /
    ``_compressed_file`` fields are updated so later lookups still resolve.

    Returns the total number of bytes saved.
    """
    if not COMPRESS_ENABLED:
        return 0   # respect the runtime toggle; leave records unmarked so a later
                   # export (with compression re-enabled) still optimises them
    saved = 0
    for r in results:
        if r.get("_compressed"):
            continue
        p_str = r.get("_image_path")
        if not p_str:
            continue
        path = Path(p_str)
        if not path.exists():
            r["_compressed"] = True   # nothing on disk to shrink — don't retry forever
            continue
        try:
            before = path.stat().st_size
        except OSError:
            before = 0
        new_path = compress_image_file(path)
        try:
            after = new_path.stat().st_size
        except OSError:
            after = before
        if new_path != path:
            r["_image_path"] = str(new_path)
            if r.get("_new_filename"):
                r["_new_filename"] = new_path.name
            r["_compressed_file"] = new_path.name
        r["_compressed"] = True
        if before and after and after < before:
            saved += before - after
            if log:
                pct = round((1 - after / before) * 100)
                log(f"[image] {new_path.name}: {before // 1024} KB → {after // 1024} KB (−{pct}%)")
    return saved


def generate_spreadsheet(
    results: list[dict],
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
) -> Optional[Path]:
    if not results:
        return None

    # Use _image_path as-is — images live in the temp/staged folder where they
    # were written during processing. Host path remapping belongs in UI only.
    resolved = list(results)

    # Deferred compression: shrink every stored receipt image now, right before
    # the workbook is built, so the output folder and the embedded images are
    # optimised in a single pass. Idempotent — already-compressed records skip.
    compress_result_images(resolved)

    by_category: dict[str, list] = defaultdict(list)
    for r in resolved:
        by_category[r.get("_category", "misc")].append(r)
    for cat_list in by_category.values():
        cat_list.sort(key=sort_key_for_receipt)

    expense_period = compute_expense_period(resolved)

    wb = build_themed_workbook(
        sections=dict(by_category),
        expense_period=expense_period,
        employee_name=employee_name,
        build_tag=APP_VERSION,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name   = re.sub(r'[^\w\s-]', '', employee_name or '').strip().replace(' ', '_') or 'Employee'
    datestamp   = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"Reimbursements_{safe_name}_{datestamp}.xlsx"
    wb.save(output_path)
    return output_path


# ── Main pipeline ──────────────────────────────────────────────────────────────

def process_receipts_batch(
    receipts_folder: Path,
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
    job_name_default: str = "",
    job_number_default: str = "",
    dry_run: bool = False,
    auto_generate: bool = True,
    use_folder_structure: bool = False,
    progress_callback:       Optional[Callable] = None,
    log_callback:            Optional[Callable] = None,
    cancel_event:            Optional[threading.Event] = None,
    receipt_status_callback: Optional[Callable] = None,
    openai_client=None,
    output_images_dir:       Optional[Path] = None,
) -> dict:
    """
    2-stage pipeline: OCR (optional) → Unified Distillation.

    receipt_status_callback(idx, total, filename, status, data, model):
      Emits per-receipt stage updates for the Kanban board.
      status: queued | ocr | distilling | done | failed
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    def progress(cur: int, tot: int, fname: str):
        if progress_callback:
            progress_callback(cur, tot, fname)

    def receipt_status(idx, tot, fname, status, data, model=""):
        if receipt_status_callback:
            receipt_status_callback(idx, tot, fname, status, data, model)

    if use_folder_structure:
        intake_dir    = receipts_folder / "Intake"
        proc_dir      = receipts_folder / "Processing"
        retry_dir     = receipts_folder / "Failed" / "Retry"
        completed_dir = output_dir / "Completed"
        for d in (intake_dir, proc_dir, retry_dir, completed_dir):
            d.mkdir(parents=True, exist_ok=True)
        scan_dir = intake_dir
    else:
        scan_dir = receipts_folder

    # Expand any PDFs in the scan directory to JPEG images before processing
    _pdf_tmp_dirs: list[Path] = []
    for pdf_path in sorted(scan_dir.glob("*.pdf")):
        tmp = scan_dir / f"_pdf_{pdf_path.stem}"
        pages = pdf_to_images(pdf_path, tmp)
        if pages:
            log(f"[pdf] Expanded {pdf_path.name} → {len(pages)} page(s)")
            _pdf_tmp_dirs.append(tmp)
            # Move original PDF to output so it won't be picked up again
            pdf_dest = (output_images_dir or output_dir) / pdf_path.name
            (output_images_dir or output_dir).mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf_path), str(pdf_dest))

    images = sorted(
        [p for p in scan_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )
    total = len(images)
    if total == 0:
        log("No receipt images found.")
        return {"processed": 0, "skipped": [], "total": 0,
                "output_path": None, "expense_period": "", "results": []}

    log(f"Found {total} receipt image(s).")
    log(f"  OCR model:    {_active_ocr_model or '(none — direct vision)'}")
    log(f"  Distill model: {_active_distill_model}")
    client = openai_client if openai_client is not None else OpenAI(
        base_url=LMSTUDIO_BASE_URL, api_key="lmstudio",
        timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES,
    )

    results: list[dict] = []
    skipped: list[str]  = []

    for i, img_path in enumerate(images, start=1):
        receipt_status(i, total, img_path.name, "queued", None)

    futures_map: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS or None) as executor:
        for i, img_path in enumerate(images, start=1):
            if cancel_event and cancel_event.is_set():
                log("Processing stopped by user.")
                break

            if use_folder_structure:
                proc_path = proc_dir / img_path.name
                img_path.rename(proc_path)
                img_path = proc_path

            idx = i
            img = img_path

            def make_status_cb(ridx, rname):
                def cb(status, data=None, model=""):
                    receipt_status(ridx, total, rname, status, data, model)
                    progress(ridx, total, rname)
                return cb

            future = executor.submit(
                _extract_receipt_with_status, client, img, make_status_cb(idx, img_path.name)
            )
            futures_map[future] = (idx, img_path)

        raw_results: list[tuple] = []
        for future in concurrent.futures.as_completed(futures_map):
            ridx, img_path = futures_map[future]
            try:
                data = future.result()
            except Exception as exc:
                log(f"  [{ridx}/{total}] ERROR — {img_path.name}: {exc}")
                data = None
            raw_results.append((ridx, img_path, data))

    raw_results.sort(key=lambda t: t[0])
    for ridx, img_path, data in raw_results:
        if cancel_event and cancel_event.is_set():
            log("Processing stopped by user.")
            break

        log(f"  [{ridx}/{total}] {img_path.name}")

        if data is None or _is_low_confidence(data):
            reason = "extraction failed" if data is None else "low confidence"
            log(f"    SKIPPED — {reason}")
            skipped.append(img_path.name)
            receipt_status(ridx, total, img_path.name, "failed", None)
            if use_folder_structure:
                try:
                    img_path.rename(retry_dir / img_path.name)
                except Exception:
                    pass
            continue

        category = classify_category(data)
        data["_category"] = category
        data["_original_index"] = ridx

        # Always use user-supplied job fields — never trust LLM extraction for these
        if category == "fuel":
            data["expense_description"] = None
        data["job_name"]   = job_name_default or None
        data["job_number"] = job_number_default or None

        flags_list = _normalize_flags(data.get("flags") or [])
        if flags_list and not data.get("_flag"):
            data["_flag"] = flags_list[0].get("flag", "")

        # Autocrop now; compression is deferred to generate_spreadsheet so the
        # stored image keeps full resolution until the report is built.
        autocrop_image_file(img_path)
        if use_folder_structure:
            dest_dir = completed_dir
            renamed    = rename_receipt_image(img_path, data, category)
            final_path = dest_dir / renamed.name
            try:
                shutil.move(str(renamed), str(final_path))
            except Exception:
                final_path = renamed
        else:
            dest = output_images_dir if output_images_dir is not None else None
            final_path = rename_receipt_image(img_path, data, category, dest)

        data["_new_filename"] = final_path.name
        data["_file"]         = final_path.name
        data["_image_path"]   = str(final_path)

        log(f"    [{category.upper():5}] {data.get('vendor','?')} — "
            f"${data.get('amount',0):.2f}  →  {final_path.name}")
        if data.get("_flag"):
            log(f"    FLAG: {data['_flag']}")

        receipt_status(ridx, total, img_path.name, "done", data)
        results.append(data)

    if not results:
        log("No receipts were successfully processed.")
        return {"processed": 0, "skipped": skipped, "total": total,
                "output_path": None, "expense_period": "", "results": []}

    _detect_duplicates(results)
    expense_period = compute_expense_period(results)
    log(f"\nExpense period: {expense_period or '(no parseable dates)'}")

    output_path: Optional[Path] = None
    if auto_generate and not dry_run:
        output_path = generate_spreadsheet(results, output_dir, employee_name)
        if output_path:
            log(f"Saved: {output_path}")
    elif dry_run:
        log("Dry run — workbook not saved.")

    processed = len(results)
    log(f"Done. {processed}/{total} receipts processed"
        + (f", {len(skipped)} skipped." if skipped else "."))

    return {
        "processed":      processed,
        "skipped":        skipped,
        "total":          total,
        "output_path":    output_path,
        "expense_period": expense_period,
        "results":        results,
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts",         default=RECEIPTS_FOLDER)
    parser.add_argument("--output-dir",       default=OUTPUT_FOLDER)
    parser.add_argument("--employee",         default="Duane Hamilton")
    parser.add_argument("--job-name",         default="")
    parser.add_argument("--job-number",       default="")
    parser.add_argument("--dry-run",          action="store_true")
    parser.add_argument("--folder-structure", action="store_true")
    args = parser.parse_args()

    receipts = Path(args.receipts)
    out_dir  = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not receipts.exists():
        print(f"ERROR: Receipts folder not found: {receipts}"); sys.exit(1)

    initialize_models()
    process_receipts_batch(
        receipts_folder=receipts,
        output_dir=out_dir,
        employee_name=args.employee,
        job_name_default=args.job_name,
        job_number_default=args.job_number,
        dry_run=args.dry_run,
        auto_generate=True,
        use_folder_structure=args.folder_structure,
    )


if __name__ == "__main__":
    main()
