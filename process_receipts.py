#!/usr/bin/env python3
"""
process_receipts.py
Core receipt-processing logic.  Can be used as a CLI or imported by the GUI.

Public API:
  initialize_models()          — load olmOCR-2 at startup, fall back to Gemma
  process_receipts_batch(...)  — full pipeline, returns a summary dict
  extract_receipt_data(...)    — send one image to LM Studio, get structured dict
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
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
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
OLMOCR_MODEL_ID   = os.getenv("OLMOCR_MODEL_ID",   "allenai/olmOCR-2-7B")
GEMMA_MODEL_ID    = os.getenv("GEMMA_MODEL_ID",     "google/gemma-4-12b-qat")
RECEIPTS_FOLDER   = "receipts"
IMAGE_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
IMAGE_MAX_PX      = 1568   # Qwen2-VL / olmOCR-2 hard limit per side

# Runtime state — updated by initialize_models()
_active_model: str = GEMMA_MODEL_ID

FUEL_VENDORS = {
    "shell", "chevron", "arco", "mobil", "exxon", "bp", "76", "valero",
    "marathon", "speedway", "sunoco", "citgo", "texaco", "pilot", "loves",
    "casey", "kwik trip", "wawa", "quiktrip", "circle k", "ampm",
    "gas station", "fuel station", "petro",
}
MATERIALS_VENDORS = {
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

# olmOCR-2 (Qwen2-VL fine-tune): terse, temp=0, higher max_tokens
OLMOCR_EXTRACTION_PROMPT = (
    'Extract receipt data as JSON with these exact keys: '
    '{"date":"YYYY-MM-DD","vendor":"store name","amount":0.00,'
    '"category":"fuel|materials|misc","job_name":null,"job_number":null,'
    '"expense_description":"brief description"}. '
    'Use the transaction TOTAL for amount. Return ONLY valid JSON, no markdown.'
)

# Gemma: explicitly disable chain-of-thought / thinking steps to save time
GEMMA_EXTRACTION_PROMPT = (
    "No reasoning steps. Respond immediately with JSON only.\n\n"
    "Analyze this receipt image and return ONLY this JSON object:\n\n"
    '{\n'
    '  "date": "YYYY-MM-DD (or month name if no day visible)",\n'
    '  "vendor": "store or vendor name",\n'
    '  "amount": 0.00,\n'
    '  "category": "fuel | materials | misc",\n'
    '  "job_name": null,\n'
    '  "job_number": null,\n'
    '  "expense_description": "brief description"\n'
    '}\n\n'
    'Category rules — fuel: gas stations; materials: hardware/supply stores; misc: everything else.\n'
    'Amount: use TOTAL or GRAND TOTAL. Return ONLY valid JSON, no markdown fences.'
)


# ── Date parsing ───────────────────────────────────────────────────────────────

def parse_date(raw: str) -> Optional[date]:
    """
    Try multiple common date formats; return a date or None.
    Handles YYYY-MM-DD, MM/DD/YY, MM/DD/YYYY, and month-name-only strings.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
                "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass

    # Non-zero-padded: split on / or - and infer order
    for sep in ('/', '-'):
        parts = raw.split(sep)
        if len(parts) == 3:
            try:
                a, b, c = int(parts[0]), int(parts[1]), int(parts[2])
                if a > 1000:                      # YYYY-MM-DD
                    return date(a, b, c)
                if c > 1000:                      # MM/DD/YYYY
                    return date(c, a, b)
                if c < 100:                       # MM/DD/YY
                    yr = 2000 + c if c < 50 else 1900 + c
                    return date(yr, a, b)
            except (ValueError, TypeError):
                pass

    month_num = MONTH_MAP.get(raw.lower())
    if month_num:
        today = date.today()
        year  = today.year if month_num <= today.month else today.year - 1
        return date(year, month_num, 1)

    return None


# ── Model management ───────────────────────────────────────────────────────────

def _api_base() -> str:
    return LMSTUDIO_BASE_URL.rstrip("/").removesuffix("/v1")


def _fuzzy_match(model_id: str, loaded_ids: list[str]) -> bool:
    key = re.sub(r"[-_/]", "", model_id.lower())
    return any(key in re.sub(r"[-_/]", "", mid.lower()) for mid in loaded_ids)


def _try_load_model(model_id: str) -> bool:
    base = _api_base()
    try:
        req = urllib.request.Request(
            f"{base}/v1/models",
            headers={"Authorization": "Bearer lmstudio"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            loaded = [m["id"] for m in json.loads(resp.read()).get("data", [])]
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


def initialize_models() -> str:
    """Try olmOCR-2; fall back to Gemma. Returns the active model ID."""
    global _active_model
    print(f"[models] Checking for olmOCR-2 ({OLMOCR_MODEL_ID}) …")
    if _try_load_model(OLMOCR_MODEL_ID):
        _active_model = OLMOCR_MODEL_ID
        print(f"[models] Primary: olmOCR-2  ({OLMOCR_MODEL_ID})")
    else:
        _active_model = GEMMA_MODEL_ID
        print(f"[models] olmOCR-2 not available — using Gemma ({GEMMA_MODEL_ID})")
    return _active_model


# ── Image encoding ─────────────────────────────────────────────────────────────

def encode_image(path: Path) -> tuple[str, str]:
    """Resize to ≤IMAGE_MAX_PX on longest side and return (base64_jpeg, mime)."""
    img = Image.open(path).convert("RGB")
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

def _check_review_needed(data: Optional[dict]) -> list[str]:
    """Return list of field names that are missing and need human review."""
    if data is None:
        return ["all fields"]
    missing = []
    if not data.get("amount"):
        missing.append("amount")
    if not (data.get("vendor") or "").strip():
        missing.append("vendor")
    if not (data.get("date") or "").strip():
        missing.append("date")
    return missing


def _is_low_confidence(data: Optional[dict]) -> bool:
    return bool(_check_review_needed(data))


def _extract_with_model(client: OpenAI, image_path: Path, model_id: str) -> Optional[dict]:
    try:
        b64, mime    = encode_image(image_path)
        is_olmocr    = re.sub(r"[-_/]", "", OLMOCR_MODEL_ID.split("/")[-1].lower()) in \
                       re.sub(r"[-_/]", "", model_id.lower())
        prompt       = OLMOCR_EXTRACTION_PROMPT if is_olmocr else GEMMA_EXTRACTION_PROMPT
        max_tokens   = 2048 if is_olmocr else 512

        response = client.chat.completions.create(
            model=model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text",      "text": prompt},
                ],
            }],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        return json.loads(raw)
    except (json.JSONDecodeError, Exception):
        return None


def extract_receipt_data(client: OpenAI, image_path: Path) -> Optional[dict]:
    """Use olmOCR-2 when loaded; auto-retry with Gemma on low-confidence result."""
    data = _extract_with_model(client, image_path, _active_model)
    if _is_low_confidence(data) and _active_model != GEMMA_MODEL_ID:
        data = _extract_with_model(client, image_path, GEMMA_MODEL_ID)
    return data


# ── Category classification ────────────────────────────────────────────────────

def classify_category(data: dict) -> str:
    cat = (data.get("category") or "misc").lower().strip()
    if cat in ("fuel", "materials", "misc"):
        return cat
    vendor = (data.get("vendor") or "").lower()
    if any(kw in vendor for kw in FUEL_VENDORS):
        return "fuel"
    if any(kw in vendor for kw in MATERIALS_VENDORS):
        return "materials"
    return "misc"


# ── Photo renaming ─────────────────────────────────────────────────────────────

def sanitize_filename_part(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:40]


def rename_receipt_image(img_path: Path, data: dict, category: str) -> Path:
    raw_date = (data.get("date") or "unknown").strip()
    date_str = raw_date if re.match(r"\d{4}-\d{2}-\d{2}", raw_date) \
               else sanitize_filename_part(raw_date)

    if category == "fuel":
        # Drop description for fuel — category tag is self-explanatory
        stem = f"fuel_{date_str}"
    else:
        desc_str = sanitize_filename_part(
            data.get("expense_description") or data.get("vendor") or "receipt"
        )
        stem = f"{category}_{date_str}_{desc_str}"

    ext      = img_path.suffix.lower()
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
    d = parse_date(data.get("date") or "")
    return d if d else date.max


def compute_expense_period(results: list[dict]) -> str:
    dates = [parse_date(r.get("date") or "") for r in results]
    dates = [d for d in dates if d and d != date.max]
    if not dates:
        return ""
    fmt = lambda d: d.strftime("%m/%d/%y")
    return f"{fmt(min(dates))} - {fmt(max(dates))}"


# ── Main pipeline ──────────────────────────────────────────────────────────────

def process_receipts_batch(
    template_path: Path,
    receipts_folder: Path,
    output_dir: Path,
    employee_name: str = "Duane Hamilton",
    job_name_default: str = "",
    job_number_default: str = "",
    dry_run: bool = False,
    rename_in_place: bool = False,
    progress_callback: Optional[Callable] = None,
    log_callback:      Optional[Callable] = None,
    cancel_event:      Optional[threading.Event] = None,
) -> dict:
    """
    Full pipeline: gather → extract → classify → rename → sort → build xlsx.
    Set rename_in_place=True when working on the user's local folder directly
    (folder mode) so renames stay in the source folder.
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    def progress(cur: int, tot: int, fname: str):
        if progress_callback:
            progress_callback(cur, tot, fname)

    images = sorted(
        [p for p in receipts_folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )
    total = len(images)
    if total == 0:
        log("No receipt images found.")
        return {"processed": 0, "skipped": [], "total": 0,
                "output_path": None, "expense_period": ""}

    log(f"Found {total} receipt image(s).  Active model: {_active_model}")
    client = OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lmstudio")

    results: list[dict] = []
    skipped: list[str]  = []

    for i, img_path in enumerate(images, start=1):
        if cancel_event and cancel_event.is_set():
            log("Processing stopped by user.")
            break

        progress(i, total, img_path.name)
        log(f"  [{i}/{total}] Analyzing: {img_path.name}")

        data = extract_receipt_data(client, img_path)
        if data is None:
            log("    SKIPPED — AI extraction failed completely")
            skipped.append(img_path.name)
            continue

        # Normalize date to ISO format so spreadsheet always gets YYYY-MM-DD
        if data.get("date"):
            parsed = parse_date(data["date"])
            if parsed:
                data["date"] = parsed.strftime("%Y-%m-%d")

        category = classify_category(data)
        data["_category"] = category

        if not data.get("job_name") and job_name_default:
            data["job_name"] = job_name_default
        if not data.get("job_number") and job_number_default:
            data["job_number"] = job_number_default

        # Flag rows that need human review
        missing = _check_review_needed(data)
        data["_needs_review"]  = bool(missing)
        data["_missing_fields"] = ", ".join(missing)

        new_path = rename_receipt_image(img_path, data, category)
        data["_new_filename"] = new_path.name
        data["_file"]         = new_path.name
        data["_image_path"]   = str(new_path)   # used by spreadsheet for image embedding

        flag = "  ⚠ REVIEW (missing: " + data["_missing_fields"] + ")" if missing else ""
        log(f"    [{category.upper():9}] {data.get('vendor','?')} — ${data.get('amount') or 0:.2f}  →  {new_path.name}{flag}")
        results.append(data)

    if not results:
        log("No receipts were successfully processed.")
        return {"processed": 0, "skipped": skipped, "total": total,
                "output_path": None, "expense_period": ""}

    by_category: dict[str, list] = defaultdict(list)
    for r in results:
        by_category[r["_category"]].append(r)
    for cat_list in by_category.values():
        cat_list.sort(key=sort_key_for_receipt)

    expense_period = compute_expense_period(results)
    log(f"\nExpense period: {expense_period or '(no parseable dates)'}")

    review_count = sum(1 for r in results if r.get("_needs_review"))
    if review_count:
        log(f"⚠  {review_count} receipt(s) flagged for review — highlighted in spreadsheet.")

    output_path: Optional[Path] = None
    if not dry_run:
        wb = build_themed_workbook(
            sections=dict(by_category),
            expense_period=expense_period,
            employee_name=employee_name,
        )
        timestamp   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = output_dir / f"Reimbursements_{timestamp}.xlsx"
        wb.save(output_path)
        log(f"\nSaved: {output_path}")
    else:
        log("\nDry run — workbook not saved.")

    processed = len(results)
    log(f"Done. {processed}/{total} processed"
        + (f", {len(skipped)} skipped" if skipped else "")
        + (f", {review_count} need review." if review_count else "."))

    return {
        "processed":      processed,
        "skipped":        skipped,
        "total":          total,
        "output_path":    output_path,
        "expense_period": expense_period,
        "review_count":   review_count,
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Process receipt images and generate a themed reimbursement spreadsheet."
    )
    parser.add_argument("spreadsheet", nargs="?", default="Reimbursement_sheet_1.xlsx")
    parser.add_argument("--receipts",    default=RECEIPTS_FOLDER)
    parser.add_argument("--output-dir",  default=None)
    parser.add_argument("--employee",    default="Duane Hamilton")
    parser.add_argument("--job-name",    default="")
    parser.add_argument("--job-number",  default="")
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    template = Path(args.spreadsheet)
    receipts = Path(args.receipts)
    out_dir  = Path(args.output_dir) if args.output_dir else template.parent

    if not template.exists():
        print(f"ERROR: Template not found: {template}"); sys.exit(1)
    if not receipts.exists():
        print(f"ERROR: Receipts folder not found: {receipts}"); sys.exit(1)

    initialize_models()
    process_receipts_batch(
        template_path=template,
        receipts_folder=receipts,
        output_dir=out_dir,
        employee_name=args.employee,
        job_name_default=args.job_name,
        job_number_default=args.job_number,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
