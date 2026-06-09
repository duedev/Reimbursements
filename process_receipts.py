#!/usr/bin/env python3
"""
Receipt Processor
Reads receipt images from a folder, extracts data via LM Studio vision AI,
and inserts rows into the reimbursement Excel spreadsheet.
"""

import os
import sys
import json
import base64
import argparse
import re
from pathlib import Path
from datetime import datetime, date
from copy import copy
from typing import Optional

import openpyxl
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
from openpyxl.utils import get_column_letter
from openai import OpenAI

# ── Configuration ─────────────────────────────────────────────────────────────
LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
MODEL_ID = "google/gemma-4-12b-qat"
RECEIPTS_FOLDER = "receipts"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}

# Column positions (1-indexed: A=1, B=2 ...)
COL_RECEIPT_NO = 1  # A
COL_DATE = 2         # B
COL_NAME = 3         # C (merged C+D)
COL_JOB_NUMBER = 5  # E  (also "Expense Description" in misc)
COL_AMOUNT = 6       # F

# Section identifiers as they appear in the spreadsheet
FUEL_LABEL = "FUEL"
MATERIALS_LABEL = "MATERIALS"
MISC_LABEL = "MISCELLENEOUS"

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


# ── AI Extraction ─────────────────────────────────────────────────────────────

def encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, mime_type) for an image file."""
    ext = path.suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".bmp": "image/bmp", ".webp": "image/webp",
        ".tiff": "image/tiff", ".tif": "image/tiff",
    }
    mime = mime_map.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return data, mime


EXTRACTION_PROMPT = """You are a receipt data extractor. Analyze this receipt image and return ONLY a JSON object with these fields:

{
  "date": "YYYY-MM-DD or month name if no specific date (e.g. 'January')",
  "vendor": "store or vendor name",
  "amount": 0.00,
  "category": "fuel | materials | misc",
  "job_name": "job name or project if visible, else null",
  "job_number": "job/project number if visible, else null",
  "expense_description": "brief description of the expense (e.g. 'Gasoline', 'Building Materials', 'Cell Phone', 'Hotel Stay', 'Per Diem')"
}

Category rules:
- "fuel": gas stations, fuel purchases (Shell, Chevron, Arco, Mobil, 76, etc.)
- "materials": Home Depot, Lowes, hardware stores, blueprint/plan prints, building supplies
- "misc": everything else (phone bills, hotel, meals, Costco, Starbucks, any restaurant, WiFi, etc.)

For amount: use the TOTAL or GRAND TOTAL on the receipt. Return as a number, no currency symbols.
For date: extract the transaction date from the receipt. If only a month is visible, return the month name.
Return ONLY valid JSON, no markdown, no explanation."""


def extract_receipt_data(client: OpenAI, image_path: Path) -> Optional[dict]:
    """Send receipt image to LM Studio and return extracted data dict."""
    print(f"  Analyzing: {image_path.name}")
    try:
        b64, mime = encode_image(image_path)
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{b64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": EXTRACTION_PROMPT,
                        },
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=512,
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown code fences if model wrapped it
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        data = json.loads(raw)
        print(f"    -> {data.get('category','?').upper()} | {data.get('vendor','?')} | ${data.get('amount', 0):.2f} | {data.get('date','?')}")
        return data

    except json.JSONDecodeError as e:
        print(f"    ERROR: Could not parse AI response as JSON: {e}")
        print(f"    Raw response: {raw[:300]}")
        return None
    except Exception as e:
        print(f"    ERROR: {e}")
        return None


def classify_category(data: dict) -> str:
    """Confirm or override AI category using vendor name as fallback."""
    category = (data.get("category") or "misc").lower().strip()
    if category in ("fuel", "materials", "misc"):
        return category

    vendor = (data.get("vendor") or "").lower()
    if any(kw in vendor for kw in FUEL_VENDORS):
        return "fuel"
    if any(kw in vendor for kw in MATERIALS_VENDORS):
        return "materials"
    return "misc"


# ── Spreadsheet Manipulation ──────────────────────────────────────────────────

def find_section(ws, label: str) -> Optional[int]:
    """Return the row number where the section label appears (e.g. 'FUEL')."""
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.strip().upper() == label.upper():
                return cell.row
    return None


def find_section_bounds(ws, label: str) -> tuple[int, int, int]:
    """
    Return (header_row, first_data_row, subtotal_row) for a section.
    header_row  = column-names row (Receipt No., Date, ...)
    first_data  = first numbered data row
    subtotal    = the Subtotal row
    """
    section_row = find_section(ws, label)
    if section_row is None:
        raise ValueError(f"Section '{label}' not found in spreadsheet")

    # Header is the next non-empty row after the section label
    header_row = section_row + 1
    first_data_row = header_row + 1

    # Subtotal row: first row after first_data_row that has 'Subtotal' in col E
    subtotal_row = None
    for r in range(first_data_row, ws.max_row + 1):
        cell_e = ws.cell(row=r, column=COL_JOB_NUMBER)
        if isinstance(cell_e.value, str) and "subtotal" in cell_e.value.lower():
            subtotal_row = r
            break

    if subtotal_row is None:
        raise ValueError(f"Could not find Subtotal row for section '{label}'")

    return header_row, first_data_row, subtotal_row


def count_filled_data_rows(ws, first_data_row: int, subtotal_row: int) -> int:
    """Count rows between first_data_row and subtotal_row that have actual data (date or amount filled)."""
    count = 0
    for r in range(first_data_row, subtotal_row):
        date_val = ws.cell(row=r, column=COL_DATE).value
        amount_val = ws.cell(row=r, column=COL_AMOUNT).value
        if date_val is not None or amount_val is not None:
            count += 1
    return count


def find_next_empty_row(ws, first_data_row: int, subtotal_row: int) -> Optional[int]:
    """Return the first row in the data range with no date AND no amount."""
    for r in range(first_data_row, subtotal_row):
        date_val = ws.cell(row=r, column=COL_DATE).value
        amount_val = ws.cell(row=r, column=COL_AMOUNT).value
        if date_val is None and amount_val is None:
            return r
    return None  # section is full


def copy_row_style(ws, src_row: int, dst_row: int):
    """Copy cell styles from src_row to dst_row."""
    for col in range(1, ws.max_column + 1):
        src = ws.cell(row=src_row, column=col)
        dst = ws.cell(row=dst_row, column=col)
        if src.has_style:
            dst.font = copy(src.font)
            dst.fill = copy(src.fill)
            dst.border = copy(src.border)
            dst.alignment = copy(src.alignment)
            dst.number_format = src.number_format


def expand_section(ws, subtotal_row: int, first_data_row: int):
    """
    Insert one new row just above subtotal_row, copy style from row above,
    assign the next receipt number, and update the SUM formula.
    Returns the new row number that was inserted.
    """
    # The row just before subtotal is the last data row
    last_data_row = subtotal_row - 1
    new_row = subtotal_row  # we insert at this position, pushing subtotal down

    ws.insert_rows(new_row)

    # After insert, subtotal is now at subtotal_row + 1
    new_subtotal_row = subtotal_row + 1

    # Copy style from the row above
    copy_row_style(ws, last_data_row, new_row)

    # Set receipt number (last receipt_no + 1)
    prev_receipt_no = ws.cell(row=last_data_row, column=COL_RECEIPT_NO).value
    new_receipt_no = (int(prev_receipt_no) + 1) if isinstance(prev_receipt_no, (int, float)) else None
    ws.cell(row=new_row, column=COL_RECEIPT_NO).value = new_receipt_no

    # Clear data cells (date, name, job_no, amount) in the new row
    for col in [COL_DATE, COL_NAME, COL_JOB_NUMBER, COL_AMOUNT]:
        ws.cell(row=new_row, column=col).value = None

    # Update SUM formula to cover the new row
    last_data = new_subtotal_row - 1
    ws.cell(row=new_subtotal_row, column=COL_AMOUNT).value = (
        f"=SUM(F{first_data_row}:F{last_data})"
    )

    return new_row, new_subtotal_row


def update_total_formula(ws):
    """Rebuild the TOTAL formula in the last row to reference current Subtotal rows."""
    fuel_sub = None
    mat_sub = None
    misc_sub = None

    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and "subtotal" in cell.value.lower():
                # find which section this belongs to
                r = cell.row
                # scan upward for section label
                for check_r in range(r - 1, 0, -1):
                    for check_cell in ws[check_r]:
                        val = check_cell.value
                        if isinstance(val, str):
                            if val.strip().upper() == FUEL_LABEL:
                                fuel_sub = r
                            elif val.strip().upper() == MATERIALS_LABEL:
                                mat_sub = r
                            elif val.strip().upper() == MISC_LABEL:
                                misc_sub = r
                    if any(v is not None for v in [fuel_sub, mat_sub, misc_sub]):
                        break

    # Find TOTAL row
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and "total" in cell.value.lower() and "sub" not in cell.value.lower():
                total_cell = ws.cell(row=cell.row, column=COL_AMOUNT)
                refs = []
                if fuel_sub:
                    refs.append(f"F{fuel_sub}")
                if mat_sub:
                    refs.append(f"F{mat_sub}")
                if misc_sub:
                    refs.append(f"F{misc_sub}")
                if refs:
                    total_cell.value = "=" + "+".join(refs)
                return


def write_receipt_row(ws, row: int, receipt_no: int, data: dict, category: str):
    """Write extracted receipt data into the given row."""
    ws.cell(row=row, column=COL_RECEIPT_NO).value = receipt_no

    # Parse date
    raw_date = data.get("date")
    parsed_date = None
    if raw_date:
        # Try ISO format first
        try:
            parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            pass
        if parsed_date is None:
            # Try month name only — keep as string
            parsed_date = raw_date  # leave as text (e.g. "January")
    ws.cell(row=row, column=COL_DATE).value = parsed_date

    vendor = data.get("vendor") or ""
    job_name = data.get("job_name") or ""

    if category == "fuel":
        # Col C: Job Name (project name)
        ws.cell(row=row, column=COL_NAME).value = job_name if job_name else vendor
        # Col E: Job Number
        ws.cell(row=row, column=COL_JOB_NUMBER).value = data.get("job_number")
    elif category == "materials":
        # Col C: Store / Job Name
        name_val = vendor
        if job_name:
            name_val = f"{vendor}/{job_name}" if job_name not in vendor else vendor
        ws.cell(row=row, column=COL_NAME).value = name_val
        ws.cell(row=row, column=COL_JOB_NUMBER).value = data.get("job_number")
    else:  # misc
        # Col C: Store / Job Name
        name_val = vendor
        if job_name:
            name_val = f"{vendor}/{job_name}" if job_name not in vendor else vendor
        ws.cell(row=row, column=COL_NAME).value = name_val
        # Col E: Expense Description
        ws.cell(row=row, column=COL_JOB_NUMBER).value = data.get("expense_description") or data.get("job_number")

    # Amount
    amount = data.get("amount")
    if amount is not None:
        ws.cell(row=row, column=COL_AMOUNT).value = float(amount)


def insert_receipt(ws, data: dict, category: str,
                   section_bounds: dict) -> tuple[int, int]:
    """
    Find or create an empty row in the appropriate section and write the data.
    Returns (receipt_no, row_written).
    Updates section_bounds in-place when rows are inserted.
    """
    label_map = {
        "fuel": FUEL_LABEL,
        "materials": MATERIALS_LABEL,
        "misc": MISC_LABEL,
    }
    label = label_map[category]
    header_row, first_data_row, subtotal_row = section_bounds[category]

    # Count existing filled rows to get next receipt number
    receipt_no = count_filled_data_rows(ws, first_data_row, subtotal_row) + 1

    # Find next empty slot
    target_row = find_next_empty_row(ws, first_data_row, subtotal_row)

    if target_row is None:
        # Section is full — expand it
        print(f"    Section {label} is full, expanding...")
        target_row, new_subtotal_row = expand_section(ws, subtotal_row, first_data_row)
        # Update bounds for this section
        section_bounds[category] = (header_row, first_data_row, new_subtotal_row)
        # All sections below this one shift down by 1 — update their bounds
        shift_below(section_bounds, category, 1)

    write_receipt_row(ws, target_row, receipt_no, data, category)
    return receipt_no, target_row


def shift_below(section_bounds: dict, changed: str, delta: int):
    """Shift row numbers for sections that come after the changed section."""
    order = ["fuel", "materials", "misc"]
    idx = order.index(changed)
    for sec in order[idx + 1:]:
        h, f, s = section_bounds[sec]
        section_bounds[sec] = (h + delta, f + delta, s + delta)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Process receipt images and populate reimbursement spreadsheet."
    )
    parser.add_argument(
        "spreadsheet",
        nargs="?",
        default="Reimbursement_sheet_1.xlsx",
        help="Path to the reimbursement Excel file (default: Reimbursement_sheet_1.xlsx)",
    )
    parser.add_argument(
        "--receipts",
        default=RECEIPTS_FOLDER,
        help=f"Folder containing receipt images (default: {RECEIPTS_FOLDER})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path (default: overwrites input file)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract data but don't modify the spreadsheet",
    )
    args = parser.parse_args()

    spreadsheet_path = Path(args.spreadsheet)
    receipts_folder = Path(args.receipts)
    output_path = Path(args.output) if args.output else spreadsheet_path

    if not spreadsheet_path.exists():
        print(f"ERROR: Spreadsheet not found: {spreadsheet_path}")
        sys.exit(1)

    if not receipts_folder.exists():
        print(f"ERROR: Receipts folder not found: {receipts_folder}")
        print(f"  Create it and drop your receipt images inside: {receipts_folder}/")
        sys.exit(1)

    # Gather images
    images = sorted(
        [p for p in receipts_folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS],
        key=lambda p: p.name,
    )
    if not images:
        print(f"No receipt images found in '{receipts_folder}/'")
        print(f"  Supported formats: {', '.join(IMAGE_EXTENSIONS)}")
        sys.exit(0)

    print(f"Found {len(images)} receipt image(s) to process")
    print()

    # Connect to LM Studio
    client = OpenAI(base_url=LMSTUDIO_BASE_URL, api_key="")

    # Load spreadsheet
    wb = load_workbook(spreadsheet_path)
    ws = wb.active

    # Map section bounds once
    section_bounds = {}
    for cat, label in [("fuel", FUEL_LABEL), ("materials", MATERIALS_LABEL), ("misc", MISC_LABEL)]:
        h, f, s = find_section_bounds(ws, label)
        section_bounds[cat] = (h, f, s)
        print(f"Section {label}: header=row{h}, data=rows{f}-{s-1}, subtotal=row{s}")
    print()

    # Process each image
    results = []
    skipped = []

    for img_path in images:
        print(f"Processing: {img_path.name}")
        data = extract_receipt_data(client, img_path)
        if data is None:
            skipped.append(img_path.name)
            continue

        category = classify_category(data)
        data["_category"] = category
        data["_file"] = img_path.name
        results.append(data)

        if not args.dry_run:
            receipt_no, row = insert_receipt(ws, data, category, section_bounds)
            print(f"    Inserted as receipt #{receipt_no} in {category.upper()} section (row {row})")
        print()

    if args.dry_run:
        print("─── DRY RUN — no changes written ───")
        for r in results:
            print(f"  [{r['_category'].upper():10}] {r['_file']}: {r.get('vendor','?')} | "
                  f"${r.get('amount',0):.2f} | {r.get('date','?')}")
    else:
        if results:
            update_total_formula(ws)
            wb.save(output_path)
            print(f"Saved: {output_path}")
        else:
            print("No receipts were successfully processed.")

    if skipped:
        print(f"\nSkipped ({len(skipped)} files — AI extraction failed):")
        for name in skipped:
            print(f"  - {name}")

    print(f"\nDone. Processed {len(results)}/{len(images)} receipts.")


if __name__ == "__main__":
    main()
