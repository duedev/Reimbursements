#!/usr/bin/env python3
"""
process_receipts.py
Core receipt-processing logic.  Can be used as a CLI or imported by the GUI.

Public API:
  process_receipts_batch(...)  — full pipeline, returns a summary dict
  extract_receipt_data(...)    — send one image to LM Studio, get structured dict
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from collections import defaultdict
from copy import copy
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openai import OpenAI

from spreadsheet_theme import build_themed_workbook

# ── Configuration ──────────────────────────────────────────────────────────────
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
MODEL_ID = "google/gemma-4-12b-qat"
RECEIPTS_FOLDER = "receipts"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}

# Column positions (shared with spreadsheet_theme)
COL_RECEIPT_NO = 1
COL_DATE       = 2
COL_NAME       = 3
COL_JOB_NUMBER = 5
COL_AMOUNT     = 6
COL_FILENAME   = 7

FUEL_LABEL      = "FUEL"
MATERIALS_LABEL = "MATERIALS"
MISC_LABEL      = "MISCELLENEOUS"

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

# Month-name → month-number (includes common OCR / human typos)
MONTH_MAP: dict[str, int] = {
    "january": 1,   "february": 2,  "march": 3,
    "april": 4,     "may": 5,       "june": 6,
    "july": 7,      "august": 8,    "september": 9,
    "october": 10,  "november": 11, "december": 12,
    # observed typos in real data
    "jaunary": 1, "feburary": 2, "jan": 1, "feb": 2, "mar": 3,
    "apr": 4,     "jun": 6,     "jul": 7, "aug": 8, "sep": 9,
    "oct": 10,    "nov": 11,    "dec": 12,
}


# ── Image encoding ─────────────────────────────────────────────────────────────

def encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, mime_type)."""
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".gif": "image/gif",
        ".bmp": "image/bmp",  ".webp": "image/webp",
        ".tiff": "image/tiff", ".tif": "image/tiff",
    }
    mime = mime_map.get(path.suffix.lower(), "image/jpeg")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return data, mime


# ── AI extraction ──────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a receipt data extractor. Analyze this receipt image and return ONLY a JSON object:

{
  "date": "YYYY-MM-DD or month name if no specific date (e.g. 'January')",
  "vendor": "store or vendor name",
  "amount": 0.00,
  "category": "fuel | materials | misc",
  "job_name": "job name or project if visible, else null",
  "job_number": "job/project number if visible, else null",
  "expense_description": "brief description (e.g. 'Gasoline', 'Building Materials', 'Cell Phone', 'Hotel Stay')"
}

Category rules:
- "fuel": gas stations, fuel purchases (Shell, Chevron, Arco, Mobil, 76, etc.)
- "materials": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies
- "misc": everything else (phone bills, hotel, meals, WiFi, restaurants, coffee, etc.)

For amount: use the TOTAL or GRAND TOTAL. Return as a number only.
For date: use the transaction date. Month name only if no day is visible.
Return ONLY valid JSON, no markdown fences, no explanation."""


def extract_receipt_data(client: OpenAI, image_path: Path) -> Optional[dict]:
    """Send receipt image to LM Studio; return extracted data dict or None."""
    try:
        b64, mime = encode_image(image_path)
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=512,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return None
    except Exception:
        return None


def classify_category(data: dict) -> str:
    """Confirm AI category or fall back to vendor-keyword matching."""
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
    """Lowercase, spaces→underscores, strip specials, cap length."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:40]


def rename_receipt_image(img_path: Path, data: dict, category: str) -> Path:
    """
    Rename receipt image to {category}_{date}_{description}.{ext}.
    Returns the new Path.
    """
    raw_date = (data.get("date") or "unknown").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw_date):
        date_str = raw_date
    else:
        date_str = sanitize_filename_part(raw_date)

    desc = (data.get("expense_description") or data.get("vendor") or "receipt").strip()
    desc_str = sanitize_filename_part(desc)
    cat_str = category  # "fuel", "materials", or "misc"

    stem = f"{cat_str}_{date_str}_{desc_str}"
    ext = img_path.suffix.lower()
    new_path = img_path.parent / f"{stem}{ext}"

    # Collision handling (also handles the no-op case when name already matches)
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


# ── Date sorting ───────────────────────────────────────────────────────────────

def sort_key_for_receipt(data: dict) -> date:
    """
    Return a date for sorting (ascending).
    Month-name-only dates → 1st of that month.
    Dates with month > current month are assumed to be last year
    (handles Dec–Jan submission periods).
    Unparseable → date.max (sorts to end).
    """
    raw = (data.get("date") or "").strip()
    if not raw:
        return date.max

    # ISO format
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        pass

    # Month name
    month_num = MONTH_MAP.get(raw.lower())
    if month_num:
        today = date.today()
        year = today.year if month_num <= today.month else today.year - 1
        return date(year, month_num, 1)

    return date.max


def compute_expense_period(results: list[dict]) -> str:
    """
    Return "MM/DD/YY - MM/DD/YY" spanning all parseable receipt dates.
    Returns empty string if no dates are parseable.
    """
    dates = [sort_key_for_receipt(r) for r in results if sort_key_for_receipt(r) != date.max]
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
    progress_callback: Optional[Callable] = None,   # (current, total, filename)
    log_callback:      Optional[Callable] = None,   # (message_str)
) -> dict:
    """
    Full pipeline:
      1. Gather images
      2. Extract + classify + rename each image
      3. Sort by date per category
      4. Build themed workbook
      5. Save as Reimbursements_{YYYY-MM-DD}_{HHMMSS}.xlsx

    Returns:
      {processed, skipped, total, output_path (Path or None), expense_period}
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    def progress(cur: int, tot: int, fname: str):
        if progress_callback:
            progress_callback(cur, tot, fname)

    # ── 1. Gather images ───────────────────────────────────────────────────────
    images = sorted(
        [p for p in receipts_folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )
    total = len(images)
    if total == 0:
        log("No receipt images found in receipts folder.")
        return {"processed": 0, "skipped": [], "total": 0,
                "output_path": None, "expense_period": ""}

    log(f"Found {total} receipt image(s).")
    client = OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="lmstudio")

    # ── 2. Extract, classify, rename ──────────────────────────────────────────
    results: list[dict] = []
    skipped: list[str]  = []

    for i, img_path in enumerate(images, start=1):
        progress(i, total, img_path.name)
        log(f"  [{i}/{total}] Analyzing: {img_path.name}")

        data = extract_receipt_data(client, img_path)
        if data is None:
            log(f"    SKIPPED — AI extraction failed")
            skipped.append(img_path.name)
            continue

        category = classify_category(data)
        data["_category"] = category

        # Inject defaults when receipt doesn't show job info
        if not data.get("job_name") and job_name_default:
            data["job_name"] = job_name_default
        if not data.get("job_number") and job_number_default:
            data["job_number"] = job_number_default

        # Rename the image file
        new_path = rename_receipt_image(img_path, data, category)
        data["_new_filename"] = new_path.name
        data["_file"] = new_path.name

        vendor  = data.get("vendor", "?")
        amount  = data.get("amount", 0) or 0
        new_fn  = new_path.name
        log(f"    [{category.upper():9}] {vendor} — ${amount:.2f}  →  {new_fn}")

        results.append(data)

    if not results:
        log("No receipts were successfully processed.")
        return {"processed": 0, "skipped": skipped, "total": total,
                "output_path": None, "expense_period": ""}

    # ── 3. Sort by date within each category ──────────────────────────────────
    by_category: dict[str, list] = defaultdict(list)
    for r in results:
        by_category[r["_category"]].append(r)

    for cat_list in by_category.values():
        cat_list.sort(key=sort_key_for_receipt)

    # ── 4. Compute expense period ──────────────────────────────────────────────
    expense_period = compute_expense_period(results)
    log(f"\nExpense period: {expense_period or '(no parseable dates)'}")

    # ── 5. Build & save workbook ───────────────────────────────────────────────
    output_path: Optional[Path] = None

    if not dry_run:
        wb = build_themed_workbook(
            sections=dict(by_category),
            expense_period=expense_period,
            employee_name=employee_name,
        )
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output_path = output_dir / f"Reimbursements_{timestamp}.xlsx"
        wb.save(output_path)
        log(f"\nSaved: {output_path}")
    else:
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
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Process receipt images and generate a themed reimbursement spreadsheet."
    )
    parser.add_argument(
        "spreadsheet",
        nargs="?",
        default="Reimbursement_sheet_1.xlsx",
        help="Template spreadsheet (used as reference; not modified)",
    )
    parser.add_argument("--receipts", default=RECEIPTS_FOLDER,
                        help="Folder containing receipt images")
    parser.add_argument("--output-dir", default=None,
                        help="Output folder (default: same as template)")
    parser.add_argument("--employee", default="Duane Hamilton",
                        help="Employee name for the form header")
    parser.add_argument("--job-name", default="",
                        help="Default job name when not visible on receipt")
    parser.add_argument("--job-number", default="",
                        help="Default job number when not visible on receipt")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and rename only; do not save spreadsheet")
    args = parser.parse_args()

    template  = Path(args.spreadsheet)
    receipts  = Path(args.receipts)
    out_dir   = Path(args.output_dir) if args.output_dir else template.parent

    if not template.exists():
        print(f"ERROR: Template not found: {template}")
        sys.exit(1)
    if not receipts.exists():
        print(f"ERROR: Receipts folder not found: {receipts}")
        sys.exit(1)

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
