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
from PIL import Image

from spreadsheet_theme import build_themed_workbook

# ── Configuration ──────────────────────────────────────────────────────────────
LMSTUDIO_BASE_URL    = os.getenv("LMSTUDIO_BASE_URL",    "http://127.0.0.1:1234/v1")
OLMOCR_MODEL_ID      = os.getenv("OLMOCR_MODEL_ID",      "allenai/olmOCR-2-7B")
GEMMA_SMALL_MODEL_ID = os.getenv("GEMMA_SMALL_MODEL_ID", "google/gemma-4-12b-qat")
GEMMA_LARGE_MODEL_ID = os.getenv("GEMMA_LARGE_MODEL_ID", "google/gemma-4-26b-a4b-qat")
GEMMA_MODEL_ID       = os.getenv("GEMMA_MODEL_ID",       GEMMA_SMALL_MODEL_ID)
MAX_PARALLEL_REQUESTS = int(os.getenv("MAX_PARALLEL_REQUESTS", "0"))  # 0 = no cap (ThreadPoolExecutor default)
RECEIPTS_FOLDER      = os.getenv("RECEIPTS_FOLDER", "receipts")
OUTPUT_FOLDER        = os.getenv("OUTPUT_FOLDER",   "output")
HOST_OUTPUT_PATH     = os.getenv("HOST_OUTPUT_PATH", "")
IMAGE_EXTENSIONS     = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
PDF_EXTENSIONS       = {".pdf"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS
IMAGE_MAX_PX         = 1568

# Runtime state — both selectable from the UI
# _active_ocr_model:    empty string = skip dedicated OCR step, use distill model directly
# _active_distill_model: model used for unified extraction + audit
_active_ocr_model:    str = ""           # no dedicated OCR model by default
_active_distill_model: str = GEMMA_MODEL_ID

# Thinking-mode flags per role (can be toggled via model swap endpoints)
_ocr_thinking:    bool = False   # off by default — smallest/fastest model for OCR
_distill_thinking: bool = True   # on by default — bigger model benefits from CoT

FUEL_VENDORS = {
    "shell", "chevron", "arco", "mobil", "exxon", "bp", "76", "valero",
    "marathon", "speedway", "sunoco", "citgo", "texaco", "pilot", "loves",
    "love's", "casey", "kwik trip", "wawa", "quiktrip", "circle k", "ampm",
    "gas station", "fuel station", "petro", "petroleum", "flying j",
    "bucees", "buc-ee", "racetrac", "racetrack", "cenex", "sinclair",
    "murphy", "murphy usa", "tom thumb", "stripes", "kwik fill", "sunoco",
    "kum & go", "sheetz", "thorntons", "mapco", "gulf", "hess",
    "conoco", "phillips 66", "pdq", "getgo", "flash foods", "moto mart",
    "pantry", "road ranger", "git n go", "corner store",
}

FUEL_KEYWORDS = {
    "gas", "gasoline", "diesel", "petrol", "fuel", "pump", "gallon",
    "gallons", "unleaded", "regular", "premium", "e85", "fill-up",
    "fill up", "fueling", "service station", "gas pump", "octane",
    "auto fuel", "motor fuel",
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


def initialize_models() -> str:
    """Check LM Studio connectivity and report available models. Does not auto-select."""
    global _active_distill_model
    available = list_available_models()
    if available:
        print(f"[models] {len(available)} model(s) available: {available}")
        if not _active_distill_model or _active_distill_model not in available:
            gemma = next((m for m in available if "gemma" in m.lower()), None)
            if gemma:
                _active_distill_model = gemma
    else:
        print("[models] LM Studio not reachable or no models loaded")

    print(f"[models] OCR model: {_active_ocr_model or '(none — using distill model for vision)'}")
    print(f"[models] Distill model: {_active_distill_model}")
    return _active_distill_model


# ── Image encoding ─────────────────────────────────────────────────────────────

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
    img.save(buf, format="JPEG", quality=92)
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
        for i, page in enumerate(doc):
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            suffix = f"_p{i + 1}" if len(doc) > 1 else ""
            img_path = dest_dir / f"{pdf_path.stem}{suffix}.jpg"
            pix.save(str(img_path))
            out.append(img_path)
        doc.close()
    except Exception as exc:
        print(f"[pdf] Failed to convert {pdf_path.name}: {exc}")
    return out


# ── AI extraction ──────────────────────────────────────────────────────────────

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
    flags = data.get("flags") or []
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
    """Stage 1: dedicated OCR model extracts raw text only."""
    try:
        b64, mime = encode_image(image_path)
        thinking_body = (
            {"thinking": {"type": "enabled", "budget_tokens": 4096}}
            if _ocr_thinking
            else {"thinking": {"type": "disabled"}}
        )
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
            if "flags" not in result:
                result["flags"] = []
            # normalise "summary" field name to "ai_summary" used downstream
            if "summary" in result and "ai_summary" not in result:
                result["ai_summary"] = result.pop("summary")
            return result
        except json.JSONDecodeError:
            return None

    thinking_body = (
        {"thinking": {"type": "enabled", "budget_tokens": 8192}}
        if _distill_thinking
        else {"thinking": {"type": "disabled"}}
    )
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
            if "flags" not in result:
                result["flags"] = []
            if "summary" in result and "ai_summary" not in result:
                result["ai_summary"] = result.pop("summary")
            return result
        except json.JSONDecodeError:
            return None

    thinking_body = (
        {"thinking": {"type": "enabled", "budget_tokens": 8192}}
        if _distill_thinking
        else {"thinking": {"type": "disabled"}}
    )
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
) -> Optional[dict]:
    """
    2-stage pipeline with Kanban status callbacks.
    If _active_ocr_model is set and differs from _active_distill_model:
      Stage 1: OCR model extracts raw text
      Stage 2: distillation model returns structured data + summary + flags
    Otherwise:
      Single stage: distillation model analyzes the image directly.
    """
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
        return data

    try:
        if _active_ocr_model and _active_ocr_model != _active_distill_model:
            _cb("ocr", model=_active_ocr_model)
            t_ocr = time.perf_counter()
            ocr_text = _extract_raw_ocr(client, image_path, _active_ocr_model)
            ocr_seconds = time.perf_counter() - t_ocr
            if ocr_text:
                _cb("distilling", model=_active_distill_model)
                t_distill = time.perf_counter()
                data = _unified_distillation(client, ocr_text)
                distill_seconds = time.perf_counter() - t_distill
                if not _is_low_confidence(data):
                    if data is not None:
                        data["_raw_ocr"] = ocr_text
                    return _finish(data, ocr_seconds, distill_seconds)
                print(f"[extract] Two-step low-confidence for {image_path.name}, "
                      "falling back to direct vision")

        _cb("distilling", model=_active_distill_model)
        t_distill = time.perf_counter()
        data = _extract_with_model(client, image_path, _active_distill_model)
        return _finish(data, distill_seconds=time.perf_counter() - t_distill)
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


def extract_candidate_totals(text: str) -> list[float]:
    """Money values found on total-like lines of raw receipt text.

    Falls back to every money value in the text when no line mentions a
    total keyword (some receipts only print the bare number).
    """
    if not text:
        return []

    def _vals(s: str) -> list[float]:
        out = []
        for m in _MONEY_RE.finditer(s):
            try:
                out.append(round(float(m.group(1).replace(",", "")), 2))
            except ValueError:
                pass
        return out

    keyword_vals: list[float] = []
    for line in text.splitlines():
        if _TOTAL_KEYWORD_RE.search(line):
            keyword_vals.extend(_vals(line))
    if keyword_vals:
        return sorted(set(keyword_vals))
    return sorted(set(_vals(text)))


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
    combined = f"{vendor} {summary} {expense}"

    # Fuel: vendor-name match is a strong signal (score +3), keyword in any field (+1)
    fuel_score = sum(3 for kw in FUEL_VENDORS if kw in vendor)
    fuel_score += sum(1 for kw in FUEL_KEYWORDS if kw in combined)

    if fuel_score >= 3:
        return "fuel"
    if model_cat == "fuel" and fuel_score >= 1:
        return "fuel"

    if any(kw in vendor for kw in MATS_VENDORS):
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

def generate_spreadsheet(
    results: list[dict],
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
    host_output_path: str = "",
) -> Optional[Path]:
    if not results:
        return None

    # Use _image_path as-is — images live in the temp/staged folder where they
    # were written during processing. Host path remapping belongs in UI only.
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
    safe_name   = re.sub(r'[^\w\s-]', '', employee_name or '').strip().replace(' ', '_') or 'Employee'
    datestamp   = datetime.now().strftime("%Y-%m-%d")
    output_path = output_dir / f"Reimbursements_{safe_name}_{datestamp}.xlsx"
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
    client = openai_client if openai_client is not None else OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lmstudio")

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

        flags_list = data.get("flags") or []
        if flags_list and not data.get("_flag"):
            data["_flag"] = flags_list[0].get("flag", "")

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
    parser.add_argument("spreadsheet", nargs="?", default="Reimbursement_sheet_1.xlsx")
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
