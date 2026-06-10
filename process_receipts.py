#!/usr/bin/env python3
"""
process_receipts.py
Core receipt-processing logic.  Can be used as a CLI or imported by the GUI.

Public API:
  initialize_models()          — load olmOCR-2 at startup, fall back to Gemma
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
import sys
import concurrent.futures
import threading
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

from openai import OpenAI
from PIL import Image

from spreadsheet_theme import build_themed_workbook

# ── Configuration ──────────────────────────────────────────────────────────────
LMSTUDIO_BASE_URL    = os.getenv("LMSTUDIO_BASE_URL",    "http://127.0.0.1:1234/v1")
OLMOCR_MODEL_ID      = os.getenv("OLMOCR_MODEL_ID",      "allenai/olmOCR-2-7B")
GEMMA_SMALL_MODEL_ID = os.getenv("GEMMA_SMALL_MODEL_ID", "google/gemma-4-12b-qat")
GEMMA_LARGE_MODEL_ID = os.getenv("GEMMA_LARGE_MODEL_ID", "google/gemma-4-26b-a4b-qat")
GEMMA_MODEL_ID       = os.getenv("GEMMA_MODEL_ID",       GEMMA_SMALL_MODEL_ID)
MAX_PARALLEL_REQUESTS = int(os.getenv("MAX_PARALLEL_REQUESTS", "4"))
RECEIPTS_FOLDER      = os.getenv("RECEIPTS_FOLDER", "receipts")
OUTPUT_FOLDER        = os.getenv("OUTPUT_FOLDER",   "output")
# Host-side absolute path prefix for file links in the generated spreadsheet.
# When running in Docker, set this to the host path that maps to OUTPUT_FOLDER.
HOST_OUTPUT_PATH     = os.getenv("HOST_OUTPUT_PATH", "")
IMAGE_EXTENSIONS     = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
# Qwen2-VL (olmOCR-2 base) recommends ≤1568 px per side
IMAGE_MAX_PX         = 1568

# Runtime state — updated by initialize_models() and POST /models/gemma
_active_model:           str = GEMMA_MODEL_ID   # primary model
_active_gemma_model:     str = GEMMA_MODEL_ID
_active_secondary_model: str = ""               # OCR-stage model; empty = primary-only

FUEL_VENDORS = {
    "shell", "chevron", "arco", "mobil", "exxon", "bp", "76", "valero",
    "marathon", "speedway", "sunoco", "citgo", "texaco", "pilot", "loves",
    "casey", "kwik trip", "wawa", "quiktrip", "circle k", "ampm",
    "gas station", "fuel station", "petro",
}
MATS_VENDORS = {
    "home depot", "lowes", "lowe's", "menards", "ace hardware", "true value",
    "harbor freight", "fastenal", "grainger", "blueprint", "print shop",
    "reprographics", "planning department", "building supply",
}

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

# olmOCR-2 step 1: raw text transcription only — NO logic, NO JSON
OLMOCR_RAW_PROMPT = (
    "Transcribe ALL visible text from this receipt image. "
    "Include every word, number, date, total, and label exactly as shown. "
    "Output only the raw transcribed text — no JSON, no formatting, no commentary."
)

# Gemma step 2 (unified): extract structured data + AI summary + audit flags in one call.
# Replaces the old separate parse + audit review steps.
_UNIFIED_DISTILLATION_TEMPLATE = (
    "You are a receipt data extractor and expense auditor. Parse the following raw OCR text "
    "from a receipt and return ONLY a JSON object with ALL of these fields:\n\n"
    "{{\n"
    '  "date": "YYYY-MM-DD",\n'
    '  "vendor": "store or vendor name",\n'
    '  "amount": 0.00,\n'
    '  "category": "fuel | mats | misc",\n'
    '  "expense_description": null,\n'
    '  "summary": "one-sentence description of what was purchased, e.g. Lunch at Butchs Grinders",\n'
    '  "flags": []\n'
    "}}\n\n"
    "Category rules:\n"
    '- "fuel": gas stations (Shell, Chevron, Arco, Mobil, 76, Valero, etc.)\n'
    '  → set expense_description = null\n'
    '- "mats": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies\n'
    '- "misc": everything else (restaurants, hotel, meals, phone bills, WiFi, coffee, etc.)\n\n'
    "Field rules:\n"
    "- Use TOTAL or GRAND TOTAL for amount\n"
    "- date must be YYYY-MM-DD from the transaction date\n"
    "- summary: one sentence describing what was purchased at the vendor (no dollar amount)\n"
    "- flags: JSON array of flag objects for any issues found:\n"
    '  * High amount: fuel > $200 → {{"flag": "Fuel total exceeds $200 threshold"}}\n'
    '  * High amount: mats > $500 → {{"flag": "Materials total exceeds $500 threshold"}}\n'
    '  * High amount: misc > $300 → {{"flag": "Misc total exceeds $300 threshold"}}\n'
    '  * OCR error: amount=0, missing vendor, garbled date → {{"flag": "OCR error: reason"}}\n'
    "  * Date outside 6-month window from {today} → "
    '{{"flag": "Date outside 6-month window"}}\n'
    "  * Return [] if no issues\n\n"
    "Return ONLY valid JSON — no markdown, no extra text.\n\n"
    "Receipt OCR text:\n{ocr_text}"
)

# Primary direct-vision extraction (used when no secondary OCR model is configured)
_GEMMA_VISION_TEMPLATE = """\
You are a receipt data extractor and expense auditor. Analyze this receipt image and return ONLY a JSON object:

{{
  "date": "YYYY-MM-DD",
  "vendor": "store or vendor name",
  "amount": 0.00,
  "category": "fuel | mats | misc",
  "expense_description": null,
  "summary": "one-sentence description of what was purchased, e.g. Lunch at Butchs Grinders",
  "flags": []
}}

Category rules:
- "fuel": gas stations (Shell, Chevron, Arco, Mobil, 76, etc.) → expense_description=null
- "mats": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies
- "misc": everything else (restaurants, hotel, meals, phone bills, WiFi, coffee, etc.)

Amount: use TOTAL or GRAND TOTAL.
Date: YYYY-MM-DD from transaction date.
summary: describe what was purchased at the vendor (no dollar amount).

flags: array of objects if issues found:
- fuel > $200, mats > $500, misc > $300 → {{"flag": "Amount exceeds threshold"}}
- amount=0, missing vendor, garbled date → {{"flag": "OCR error: reason"}}
- date outside 6-month window from {today} → {{"flag": "Date outside 6-month window"}}
Return [] for flags if no issues.

Return ONLY valid JSON, no markdown fences, no explanation."""


# ── Model management ───────────────────────────────────────────────────────────

def _api_base() -> str:
    return LMSTUDIO_BASE_URL.rstrip("/").removesuffix("/v1")


def _fuzzy_match(model_id: str, loaded_ids: list[str]) -> bool:
    """Return True if model_id loosely matches any entry in loaded_ids."""
    key = re.sub(r"[-_/]", "", model_id.lower())
    for mid in loaded_ids:
        if key in re.sub(r"[-_/]", "", mid.lower()):
            return True
    return False


def _try_load_model(model_id: str) -> bool:
    """
    Check whether model_id is already loaded in LM Studio; if not, request
    it via the LM Studio REST API (/api/v0/models/load).
    Returns True when the model is confirmed available.
    """
    base = _api_base()

    try:
        req = urllib.request.Request(
            f"{base}/v1/models",
            headers={"Authorization": "Bearer lmstudio"},
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
            f"{base}/api/v0/models/load",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status == 200
    except Exception:
        return False


def list_available_models() -> list[str]:
    """Query LM Studio for all loaded models. Returns list of model IDs."""
    base = _api_base()
    try:
        req = urllib.request.Request(
            f"{base}/v1/models",
            headers={"Authorization": "Bearer lmstudio"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


def initialize_models() -> str:
    """
    Load the primary model at startup.
    Returns the active model ID.
    """
    global _active_model
    print(f"[models] Loading primary model ({_active_gemma_model}) …")
    if _try_load_model(_active_gemma_model):
        _active_model = _active_gemma_model
        print(f"[models] Primary: {_active_gemma_model}")
    else:
        _active_model = _active_gemma_model
        print(f"[models] Warning: could not confirm primary model {_active_gemma_model}")
    return _active_model


# ── Image encoding ─────────────────────────────────────────────────────────────

def encode_image(path: Path) -> tuple[str, str]:
    """
    Open, resize to ≤IMAGE_MAX_PX on longest side, return (base64_jpeg, 'image/jpeg').
    MPO (dual-camera JPEG) files are handled by extracting only the first frame.
    """
    raw = Image.open(path)
    if getattr(raw, "format", None) == "MPO":
        raw.seek(0)
    img = raw.convert("RGB")
    if max(img.size) > IMAGE_MAX_PX:
        ratio = IMAGE_MAX_PX / max(img.size)
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


# ── AI extraction ──────────────────────────────────────────────────────────────

def _is_low_confidence(data: Optional[dict]) -> bool:
    """True when the extraction is missing critical fields."""
    if data is None:
        return True
    if not data.get("amount"):
        return True
    if not (data.get("vendor") or "").strip():
        return True
    return False


def _strip_json(raw: str) -> str:
    """Strip markdown fences and extract the first JSON object/array."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    return match.group(0) if match else raw


def _extract_raw_ocr(client: OpenAI, image_path: Path, model_id: str) -> Optional[str]:
    """
    Stage 1: send image to olmOCR-2, return raw transcribed text only.
    No logic, no JSON — pure visual text extraction.
    """
    try:
        b64, mime = encode_image(image_path)
        response = client.chat.completions.create(
            model=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": OLMOCR_RAW_PROMPT},
                ],
            }],
            temperature=0.0,
            max_tokens=2048,
        )
        text = response.choices[0].message.content.strip()
        return text if text else None
    except Exception as exc:
        print(f"[ocr] Raw extraction failed for {image_path.name}: {exc}")
        return None


def _unified_distillation(
    client: OpenAI,
    ocr_text: str,
    *,
    _retry: bool = True,
) -> Optional[dict]:
    """Stage 2: send raw OCR text to primary model; returns JSON with fields + summary + flags."""
    today = date.today().isoformat()
    try:
        prompt = _UNIFIED_DISTILLATION_TEMPLATE.format(ocr_text=ocr_text, today=today)
    except KeyError as ke:
        print(f"[distill] Template format error — unescaped placeholder {ke}; falling back to raw OCR text")
        prompt = f"Extract receipt data as JSON from this text:\n\n{ocr_text}"
    system_msg = {
        "role": "system",
        "content": "You are a receipt data extractor. Respond with valid JSON only.",
    }
    user_msg = {"role": "user", "content": prompt}

    def _parse_response(raw: str) -> Optional[dict]:
        raw = _strip_json(raw)
        try:
            result = json.loads(raw)
            if "flags" not in result:
                result["flags"] = []
            # normalise "summary" field name to "ai_summary" used downstream
            if "summary" in result and "ai_summary" not in result:
                result["ai_summary"] = result.pop("summary")
            return result
        except json.JSONDecodeError:
            return None

    try:
        response = client.chat.completions.create(
            model=_active_gemma_model,
            messages=[system_msg, user_msg],
            temperature=0.0,
            max_tokens=768,
        )
        raw = response.choices[0].message.content.strip()
        result = _parse_response(raw)
        if result is not None:
            return result
        if _retry:
            print(f"[distill] JSON parse failed, retrying …  ({raw[:120]})")
            strict_msg = {
                "role": "user",
                "content": "Return ONLY the JSON object — no extra text, no markdown.",
            }
            r2 = client.chat.completions.create(
                model=_active_gemma_model,
                messages=[system_msg, user_msg, strict_msg],
                temperature=0.0,
                max_tokens=768,
            )
            result2 = _parse_response(r2.choices[0].message.content.strip())
            if result2 is not None:
                return result2
            print(f"[distill] Retry failed.")
        return None
    except Exception as exc:
        print(f"[distill] Exception: {exc}")
        return None


def _extract_with_model(
    client: OpenAI,
    image_path: Path,
    model_id: str,
    *,
    _retry: bool = True,
) -> Optional[dict]:
    """Gemma direct-vision fallback (used when olmOCR-2 is unavailable or low confidence)."""
    today = date.today().isoformat()
    prompt = _GEMMA_VISION_TEMPLATE.replace("{today}", today)

    def _parse_response(raw: str) -> Optional[dict]:
        raw = _strip_json(raw)
        try:
            result = json.loads(raw)
            if "flags" not in result:
                result["flags"] = []
            if "summary" in result and "ai_summary" not in result:
                result["ai_summary"] = result.pop("summary")
            return result
        except json.JSONDecodeError:
            return None

    try:
        b64, mime = encode_image(image_path)
        system_msg = {
            "role": "system",
            "content": "You are a receipt data extractor. Always respond with valid JSON only.",
        }
        user_msg = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text",      "text": prompt},
            ],
        }

        response = client.chat.completions.create(
            model=model_id,
            messages=[system_msg, user_msg],
            temperature=0.0,
            max_tokens=768,
        )
        result = _parse_response(response.choices[0].message.content.strip())
        if result is not None:
            return result

        if _retry:
            print(f"[extract] JSON parse failed for {image_path.name}, retrying …")
            strict_msg = {
                "role": "user",
                "content": "Your response was not valid JSON. Return ONLY the JSON object.",
            }
            retry_resp = client.chat.completions.create(
                model=model_id,
                messages=[system_msg, user_msg, strict_msg],
                temperature=0.0,
                max_tokens=768,
            )
            result2 = _parse_response(retry_resp.choices[0].message.content.strip())
            if result2 is not None:
                return result2
            print(f"[extract] Retry also failed for {image_path.name}")
        return None

    except Exception as exc:
        print(f"[extract] Exception for {image_path.name}: {exc}")
        return None


def _extract_receipt_with_status(
    client: OpenAI,
    image_path: Path,
    status_cb: Optional[Callable],
) -> Optional[dict]:
    """
    Two-stage pipeline with granular status callbacks for the Kanban board.
    Any unhandled exception returns None so the caller logs it as a failed receipt.
    """
    def _cb(status: str, data: Optional[dict] = None):
        try:
            if status_cb:
                status_cb(status, data)
        except Exception as cb_exc:
            print(f"[extract] Status callback error ({status}): {cb_exc}")

    try:
        if _active_secondary_model:
            _cb("ocr")
            ocr_text = _extract_raw_ocr(client, image_path, _active_secondary_model)
            if ocr_text:
                _cb("distilling")
                data = _unified_distillation(client, ocr_text)
                if not _is_low_confidence(data):
                    if data is not None:
                        data["_raw_ocr"] = ocr_text
                    return data
                print(f"[extract] Two-step low-confidence for {image_path.name}, "
                      f"falling back to primary direct vision")

        # Primary model analyzes the image directly
        _cb("distilling")
        return _extract_with_model(client, image_path, _active_gemma_model)

    except Exception as exc:
        print(f"[extract] Unhandled error for {image_path.name}: {exc}")
        _cb("failed")
        return None


def extract_receipt_data(client: OpenAI, image_path: Path) -> Optional[dict]:
    """Convenience wrapper — extract receipt data without status callbacks."""
    return _extract_receipt_with_status(client, image_path, status_cb=None)


# ── Category classification ────────────────────────────────────────────────────

def classify_category(data: dict) -> str:
    """Confirm AI category or fall back to vendor-keyword matching."""
    cat = (data.get("category") or "misc").lower().strip()
    if cat == "materials":
        return "mats"
    if cat in ("fuel", "mats", "misc"):
        return cat
    vendor = (data.get("vendor") or "").lower()
    if any(kw in vendor for kw in FUEL_VENDORS):
        return "fuel"
    if any(kw in vendor for kw in MATS_VENDORS):
        return "mats"
    return "misc"


# ── Duplicate detection ────────────────────────────────────────────────────────

def _detect_duplicates(results: list[dict]) -> None:
    """Flag receipts that share the same vendor + date + amount combination."""
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
    """Convert YYYY-MM-DD to MM-DD-YY for filenames. Returns sanitized fallback if unparseable."""
    try:
        d = datetime.strptime(raw_date, "%Y-%m-%d").date()
        return d.strftime("%m-%d-%y")
    except (ValueError, TypeError):
        return sanitize_filename_part(raw_date) or "unknown"


def rename_receipt_image(img_path: Path, data: dict, category: str) -> Path:
    """
    New naming convention: MM-DD-YY_VendorDescription.ext
    Examples: 12-30-24_Butchs_Grinders.jpg, 06-15-25_Shell.jpg
    Dashes for date parts, underscores between words.
    Collisions get a numeric suffix: …_2, _3, …
    """
    raw_date = (data.get("date") or "unknown").strip()
    date_str = _format_date_mmddyy(raw_date)

    vendor_str = sanitize_filename_part(
        data.get("vendor") or data.get("expense_description") or "receipt"
    )

    stem = f"{date_str}_{vendor_str}" if vendor_str else date_str
    ext  = img_path.suffix.lower()
    new_path = img_path.parent / f"{stem}{ext}"

    if new_path.exists() and new_path.resolve() != img_path.resolve():
        counter = 2
        while True:
            candidate = img_path.parent / f"{stem}_{counter}{ext}"
            if not candidate.exists():
                new_path = candidate
                break
            counter += 1

    if new_path.resolve() != img_path.resolve():
        img_path.rename(new_path)
    return new_path


# ── Date helpers ───────────────────────────────────────────────────────────────

def sort_key_for_receipt(data: dict) -> date:
    raw = (data.get("date") or "").strip()
    if not raw:
        return date.max
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        pass
    month_num = MONTH_MAP.get(raw.lower())
    if month_num:
        today = date.today()
        year  = today.year if month_num <= today.month else today.year - 1
        return date(year, month_num, 1)
    return date.max


def compute_expense_period(results: list[dict]) -> str:
    dates = [sort_key_for_receipt(r) for r in results if sort_key_for_receipt(r) != date.max]
    if not dates:
        return ""
    fmt = lambda d: d.strftime("%m/%d/%y")
    return f"{fmt(min(dates))} - {fmt(max(dates))}"


# ── Spreadsheet generation ─────────────────────────────────────────────────────

def generate_spreadsheet(
    results: list[dict],
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
    host_output_path: str = "",
) -> Optional[Path]:
    """
    Build the themed workbook from processed results and save to output_dir.
    Returns the output path, or None if no results.
    If host_output_path is set, image paths in the workbook are rewritten
    to use the host-side absolute path (for Docker deployments).
    """
    if not results:
        return None

    # Rewrite _image_path to host paths if configured
    resolved = []
    host_base = (host_output_path or HOST_OUTPUT_PATH).rstrip("/")
    if host_base:
        for r in results:
            r2 = dict(r)
            if r2.get("_image_path"):
                fname = Path(r2["_image_path"]).name
                r2["_image_path"] = str(Path(host_base) / fname)
            resolved.append(r2)
    else:
        resolved = list(results)

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
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_path = output_dir / f"Reimbursements_{timestamp}.xlsx"
    wb.save(output_path)
    return output_path


# ── Main pipeline ──────────────────────────────────────────────────────────────

def process_receipts_batch(
    template_path: Path,
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
) -> dict:
    """
    Full pipeline (2-stage: OCR → Unified Distillation):
      1. Gather images from receipts_folder (or Intake/ subfolder)
      2. For each image: olmOCR raw text, then Gemma unified distillation
         (extraction + AI summary + audit flags in a single LLM call)
      3. Sequential classify + rename
      4. Duplicate detection
      5. Optionally generate workbook (auto_generate=True for CLI, False for web UI)

    receipt_status_callback(idx, total, filename, status, data):
      Called at each stage — used by the web UI Kanban board.
      status: "queued" | "ocr" | "distilling" | "done" | "failed" | "retry"
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    def progress(cur: int, tot: int, fname: str):
        if progress_callback:
            progress_callback(cur, tot, fname)

    def receipt_status(idx: int, tot: int, fname: str, status: str, data: Optional[dict]):
        if receipt_status_callback:
            receipt_status_callback(idx, tot, fname, status, data)

    # ── Resolve intake folder ─────────────────────────────────────────────────
    if use_folder_structure:
        intake_dir   = receipts_folder / "Intake"
        proc_dir     = receipts_folder / "Processing"
        retry_dir    = receipts_folder / "Failed" / "Retry"
        completed_dir = output_dir / "Completed"
        for d in (intake_dir, proc_dir, retry_dir, completed_dir):
            d.mkdir(parents=True, exist_ok=True)
        scan_dir = intake_dir
    else:
        scan_dir = receipts_folder

    images = sorted(
        [p for p in scan_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )
    total = len(images)
    if total == 0:
        log("No receipt images found.")
        return {"processed": 0, "skipped": [], "total": 0,
                "output_path": None, "expense_period": "", "results": []}

    log(f"Found {total} receipt image(s).  Primary model: {_active_model}")
    client = OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lmstudio")

    results: list[dict] = []
    skipped: list[str]  = []

    # ── Parallel extraction (Stage 1 OCR + Stage 2 Unified Distillation) ────
    for i, img_path in enumerate(images, start=1):
        receipt_status(i, total, img_path.name, "queued", None)

    futures_map: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as executor:
        for i, img_path in enumerate(images, start=1):
            if cancel_event and cancel_event.is_set():
                log("Processing stopped by user (before submission).")
                break

            # Move to Processing/ if using folder structure
            if use_folder_structure:
                proc_path = proc_dir / img_path.name
                img_path.rename(proc_path)
                img_path = proc_path

            idx = i
            img = img_path  # capture for closure

            def make_status_cb(ridx: int, rname: str):
                def cb(status: str, data: Optional[dict] = None):
                    receipt_status(ridx, total, rname, status, data)
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

    # ── Sequential classify + rename ─────────────────────────────────────────
    raw_results.sort(key=lambda t: t[0])
    for ridx, img_path, data in raw_results:
        if cancel_event and cancel_event.is_set():
            log("Processing stopped by user.")
            break

        log(f"  [{ridx}/{total}] Analyzing: {img_path.name}")

        if data is None or _is_low_confidence(data):
            reason = "AI extraction failed" if data is None else "low confidence extraction"
            log(f"    SKIPPED — {reason}")
            skipped.append(img_path.name)
            receipt_status(ridx, total, img_path.name, "failed", None)
            if use_folder_structure:
                # Move to Failed/Retry/
                try:
                    img_path.rename(retry_dir / img_path.name)
                except Exception:
                    pass
            continue

        category = classify_category(data)
        data["_category"] = category
        data["_original_index"] = ridx

        # Job name/number always come from user input, never from the receipt
        if category == "fuel":
            data["job_name"] = None
            data["job_number"] = None
            data["expense_description"] = None
        else:
            data["job_name"] = job_name_default or None
            data["job_number"] = job_number_default or None

        # Promote first flag from unified distillation to the legacy _flag field
        flags_list = data.get("flags") or []
        if flags_list and not data.get("_flag"):
            data["_flag"] = flags_list[0].get("flag", "")

        # Rename and optionally move to Completed/
        dest_dir = completed_dir if use_folder_structure else img_path.parent
        if use_folder_structure:
            # Rename in-place in Processing/, then move to Completed/
            renamed = rename_receipt_image(img_path, data, category)
            final_path = dest_dir / renamed.name
            try:
                renamed.rename(final_path)
            except Exception:
                final_path = renamed
        else:
            final_path = rename_receipt_image(img_path, data, category)

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

    # ── Duplicate detection (code-based, across all receipts) ────────────────
    _detect_duplicates(results)

    expense_period = compute_expense_period(results)
    log(f"\nExpense period: {expense_period or '(no parseable dates)'}")

    output_path: Optional[Path] = None
    if auto_generate and not dry_run:
        output_path = generate_spreadsheet(results, output_dir, employee_name)
        if output_path:
            log(f"\nSaved: {output_path}")
    elif dry_run:
        log("\nDry run — workbook not saved.")

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
    parser = argparse.ArgumentParser(
        description="Process receipt images and generate a themed reimbursement spreadsheet."
    )
    parser.add_argument("spreadsheet", nargs="?", default="Reimbursement_sheet_1.xlsx")
    parser.add_argument("--receipts",           default=RECEIPTS_FOLDER)
    parser.add_argument("--output-dir",         default=OUTPUT_FOLDER)
    parser.add_argument("--employee",           default="Duane Hamilton")
    parser.add_argument("--job-name",           default="")
    parser.add_argument("--job-number",         default="")
    parser.add_argument("--dry-run",            action="store_true")
    parser.add_argument("--folder-structure",   action="store_true",
                        help="Use Intake/Processing/Failed/Retry/Output/Completed/ structure")
    args = parser.parse_args()

    receipts = Path(args.receipts)
    out_dir  = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not receipts.exists():
        print(f"ERROR: Receipts folder not found: {receipts}"); sys.exit(1)

    initialize_models()
    process_receipts_batch(
        template_path=Path(args.spreadsheet),
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
