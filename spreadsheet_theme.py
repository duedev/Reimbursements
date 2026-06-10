"""
spreadsheet_theme.py
Builds a fresh, professionally-themed reimbursement workbook from extracted receipt data.
"""
from __future__ import annotations

import io
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── Color palette ──────────────────────────────────────────────────────────────
COLOR_TITLE_BG    = "2C3E50"   # dark charcoal — title row
COLOR_TITLE_FG    = "FFFFFF"
COLOR_SECTION_BG  = "475569"   # slate gray — section banners (replaces orange)
COLOR_SECTION_FG  = "FFFFFF"
COLOR_HEADER_BG   = "3B82F6"   # steel blue — column header rows
COLOR_HEADER_FG   = "FFFFFF"
COLOR_META_BG     = "EBF5FB"   # light blue — employee / period rows
COLOR_ROW_PLAIN   = "FFFFFF"
COLOR_ROW_ALT     = "F1F5F9"
COLOR_SUBTOTAL_BG = "FEF9C3"
COLOR_SUBTOTAL_FG = "1F2937"
COLOR_TOTAL_BG    = "2C3E50"
COLOR_TOTAL_FG    = "FFFFFF"
COLOR_NOTE_BG     = "FEF9C3"
COLOR_REVIEW_ROW  = "FFF9C4"   # soft yellow — row needs human review
COLOR_REVIEW_NOTE = "FFCCCC"   # light red   — review note cell (col H)

# ── Column positions (1-indexed) ───────────────────────────────────────────────
COL_RECEIPT_NO = 1   # A
COL_DATE       = 2   # B
COL_NAME       = 3   # C (C+D merged)
COL_JOB_NUMBER = 5   # E
COL_AMOUNT     = 6   # F
COL_FILENAME   = 7   # G
COL_REVIEW     = 8   # H — review notes (outside styled table, easy to delete)

COLUMN_WIDTHS = {
    "A": 9.5,
    "B": 13.0,
    "C": 33.0,
    "D": 22.5,
    "E": 28.5,
    "F": 17.5,
    "G": 36.0,
    "H": 28.0,   # review notes column
}

ACCT_FORMAT = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
DATE_FORMAT  = "m/d/yy"
LAST_COL     = 7   # G — main table ends here; H is the auxiliary review column


# ── Style helpers ──────────────────────────────────────────────────────────────

def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _font(bold: bool = False, color: str = "000000", size: int = 11,
          underline: str = "none") -> Font:
    return Font(bold=bold, color=color, size=size, name="Calibri", underline=underline)


def _border(color: str = "CCCCCC") -> Border:
    side = Side(style="thin", color=color)
    return Border(left=side, right=side, top=side, bottom=side)


def _align(h: str = "center", v: str = "center", wrap: bool = True) -> Alignment:
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def _flood(ws, row: int, fill: PatternFill, font: Font = None,
           border: Border = None, align: Alignment = None,
           cols: range = None):
    for col in (cols or range(1, LAST_COL + 1)):
        cell = ws.cell(row=row, column=col)
        cell.fill = fill
        if font:
            cell.font = font
        if border:
            cell.border = border
        if align:
            cell.alignment = align


# ── Row writers ────────────────────────────────────────────────────────────────

def _write_title(ws, row: int):
    ws.merge_cells(f"A{row}:G{row}")
    cell = ws.cell(row=row, column=1, value="Expense Reimbursement Form")
    cell.font = _font(bold=True, color=COLOR_TITLE_FG, size=16)
    cell.fill = _fill(COLOR_TITLE_BG)
    cell.alignment = _align(h="center")
    _flood(ws, row, _fill(COLOR_TITLE_BG), cols=range(2, LAST_COL + 1))
    ws.row_dimensions[row].height = 30


def _write_meta_field(ws, row: int, label: str, value: str,
                      label_col: int = 2, value_col: int = 4):
    meta_fill = _fill(COLOR_META_BG)
    _flood(ws, row, meta_fill)
    lbl = ws.cell(row=row, column=label_col, value=label)
    lbl.font = _font(bold=True, size=11)
    lbl.fill = meta_fill
    lbl.alignment = _align(h="right")
    end_col = value_col + 1
    ws.merge_cells(
        f"{get_column_letter(value_col)}{row}:{get_column_letter(end_col)}{row}"
    )
    val = ws.cell(row=row, column=value_col, value=value)
    val.font = _font(size=11)
    val.fill = meta_fill
    val.alignment = _align(h="left")
    ws.row_dimensions[row].height = 18


def _write_note_row(ws, row: int, text: str):
    note_fill = _fill(COLOR_NOTE_BG)
    _flood(ws, row, note_fill)
    ws.merge_cells(f"D{row}:G{row}")
    cell = ws.cell(row=row, column=4, value=text)
    cell.font = _font(bold=True, size=10, color="92400E")
    cell.fill = note_fill
    cell.alignment = _align(h="center")
    ws.row_dimensions[row].height = 18


def _write_section_banner(ws, row: int, label: str):
    ws.merge_cells(f"A{row}:G{row}")
    cell = ws.cell(row=row, column=1, value=f"  {label}")
    cell.font = _font(bold=True, color=COLOR_SECTION_FG, size=13)
    cell.fill = _fill(COLOR_SECTION_BG)
    cell.alignment = _align(h="left", wrap=False)
    _flood(ws, row, _fill(COLOR_SECTION_BG), cols=range(2, LAST_COL + 1))
    ws.row_dimensions[row].height = 24


def _write_col_headers(ws, row: int, headers: list[str], has_review: bool = False):
    hdr_fill   = _fill(COLOR_HEADER_BG)
    hdr_font   = _font(bold=True, color=COLOR_HEADER_FG, size=11)
    hdr_border = _border("2E75B6")
    ws.merge_cells(f"C{row}:D{row}")
    _flood(ws, row, hdr_fill, hdr_font, hdr_border)
    for col_idx, text in enumerate(headers, start=1):
        if col_idx == 4:
            continue
        cell = ws.cell(row=row, column=col_idx, value=text)
        cell.alignment = _align(h="center", wrap=True)
    if has_review:
        cell_h = ws.cell(row=row, column=COL_REVIEW, value="⚠ Review Notes")
        cell_h.fill   = _fill("FFAAAA")
        cell_h.font   = _font(bold=True, color="880000", size=10)
        cell_h.border = _border("FF9999")
        cell_h.alignment = _align(h="center")
    ws.row_dimensions[row].height = 32


def _write_data_row(ws, row: int, receipt_no: int, data: dict,
                    category: str, fill_color: str,
                    receipt_refs: dict[str, str] | None = None):
    needs_review = data.get("_needs_review", False)
    actual_fill  = COLOR_REVIEW_ROW if needs_review else fill_color
    row_fill     = _fill(actual_fill)
    row_font     = _font(size=11)
    row_border   = _border()

    _flood(ws, row, row_fill, row_font, row_border)
    ws.merge_cells(f"C{row}:D{row}")

    # A — Receipt No.
    cell_a = ws.cell(row=row, column=COL_RECEIPT_NO, value=receipt_no)
    cell_a.font      = _font(bold=True, size=11)
    cell_a.alignment = _align(h="center")

    # B — Date
    raw_date = data.get("date")
    date_val = None
    if raw_date and isinstance(raw_date, str):
        try:
            date_val = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            date_val = raw_date
    elif raw_date and hasattr(raw_date, "strftime"):
        date_val = raw_date
    cell_b = ws.cell(row=row, column=COL_DATE, value=date_val)
    if isinstance(date_val, (datetime, date)):
        cell_b.number_format = DATE_FORMAT
    cell_b.alignment = _align(h="center")

    # C — Name
    vendor   = (data.get("vendor") or "").strip()
    job_name = (data.get("job_name") or "").strip()
    if category == "fuel":
        name_val = job_name if job_name else vendor
    else:
        name_val = f"{vendor}/{job_name}" if (job_name and job_name not in vendor) else vendor
    cell_c = ws.cell(row=row, column=COL_NAME, value=name_val)
    cell_c.alignment = _align(h="left", wrap=True)

    # E — Job Number / Expense Description
    e_val = (data.get("expense_description") or data.get("job_number")) \
            if category == "misc" else data.get("job_number")
    ws.cell(row=row, column=COL_JOB_NUMBER, value=e_val).alignment = _align(h="center")

    # F — Amount
    amount = data.get("amount")
    cell_f = ws.cell(row=row, column=COL_AMOUNT)
    if amount:
        cell_f.value        = float(amount)
        cell_f.number_format = ACCT_FORMAT
    elif needs_review:
        cell_f.value = None   # leave blank rather than show $0.00 for missing amounts
    cell_f.alignment = _align(h="right")

    # G — Filename (hyperlink to Receipts sheet if available)
    filename = data.get("_new_filename") or data.get("_file") or ""
    cell_g   = ws.cell(row=row, column=COL_FILENAME, value=filename)
    ref      = (receipt_refs or {}).get(filename)
    if ref:
        cell_g.hyperlink  = f"#{ref}"
        cell_g.font       = _font(color="0563C1", underline="single", size=11)
    cell_g.alignment = _align(h="left", wrap=False)

    # H — Review note (only written when review needed; outside main table)
    if needs_review:
        missing  = data.get("_missing_fields", "")
        cell_h   = ws.cell(row=row, column=COL_REVIEW,
                           value=f"Missing: {missing} — delete this cell after review")
        cell_h.fill      = _fill(COLOR_REVIEW_NOTE)
        cell_h.font      = _font(bold=True, color="880000", size=10)
        cell_h.border    = _border("FF9999")
        cell_h.alignment = _align(h="left", wrap=True)

    ws.row_dimensions[row].height = 18


def _write_subtotal(ws, row: int, first_data: int, last_data: int):
    sub_fill   = _fill(COLOR_SUBTOTAL_BG)
    sub_font   = _font(bold=True, size=11, color=COLOR_SUBTOTAL_FG)
    sub_border = _border("D1D5DB")
    _flood(ws, row, sub_fill, sub_font, sub_border)
    ws.cell(row=row, column=COL_JOB_NUMBER).alignment = _align(h="right")
    ws.cell(row=row, column=COL_JOB_NUMBER).value = "Subtotal"
    cell_f = ws.cell(row=row, column=COL_AMOUNT)
    # Guard: when empty category last_data < first_data, writing a SUM would
    # create a circular reference.  Write 0 (a literal) instead.
    if last_data >= first_data:
        cell_f.value = f"=SUM(F{first_data}:F{last_data})"
    else:
        cell_f.value = 0
    cell_f.number_format = ACCT_FORMAT
    cell_f.alignment     = _align(h="right")
    ws.row_dimensions[row].height = 20


def _write_total(ws, row: int, fuel_sub: int, mat_sub: int, misc_sub: int):
    tot_fill = _fill(COLOR_TOTAL_BG)
    tot_font = _font(bold=True, color=COLOR_TOTAL_FG, size=12)
    _flood(ws, row, tot_fill, tot_font)
    ws.merge_cells(f"A{row}:D{row}")
    ws.cell(row=row, column=1, value="**Please attach receipts.**").alignment = \
        _align(h="left", wrap=False)
    ws.cell(row=row, column=COL_JOB_NUMBER, value="TOTAL").alignment = _align(h="right")
    cell_f = ws.cell(row=row, column=COL_AMOUNT,
                     value=f"=F{fuel_sub}+F{mat_sub}+F{misc_sub}")
    cell_f.number_format = ACCT_FORMAT
    cell_f.alignment     = _align(h="right")
    ws.row_dimensions[row].height = 24


# ── Receipts image sheet ───────────────────────────────────────────────────────

def _build_receipts_sheet(wb: Workbook, sections: dict) -> dict[str, str]:
    """
    Create a 'Receipts' sheet with embedded receipt images.
    Returns {filename: hyperlink_target} for linking from Sheet1.
    """
    from PIL import Image as PILImage
    from openpyxl.drawing.image import Image as XLImage

    ws = wb.create_sheet("Receipts")
    ws.column_dimensions["A"].width = 80
    ws.sheet_view.showGridLines = False

    refs: dict[str, str] = {}
    current_row = 1
    MAX_W, MAX_H = 600, 800   # max display pixels per image

    order = ["fuel", "materials", "misc"]
    for category in order:
        for data in sections.get(category, []):
            img_path = data.get("_image_path", "")
            fname    = data.get("_new_filename") or data.get("_file") or ""
            if not img_path or not Path(img_path).exists():
                refs[fname] = f"Receipts!A{current_row}"
                continue

            # Label row
            label = ws.cell(row=current_row, column=1, value=fname)
            label.font      = Font(bold=True, size=10, name="Calibri")
            label.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[current_row].height = 16
            refs[fname] = f"Receipts!A{current_row}"
            current_row += 1

            # Embed image
            try:
                pil = PILImage.open(img_path).convert("RGB")
                ratio    = min(MAX_W / pil.width, MAX_H / pil.height, 1.0)
                disp_w   = int(pil.width  * ratio)
                disp_h   = int(pil.height * ratio)

                # Save a resized copy to a buffer so we embed a small version
                buf = io.BytesIO()
                pil.resize((disp_w, disp_h), PILImage.LANCZOS).save(buf, format="JPEG", quality=85)
                buf.seek(0)

                xl_img        = XLImage(buf)
                xl_img.width  = disp_w
                xl_img.height = disp_h
                xl_img.anchor = f"A{current_row}"
                ws.add_image(xl_img)

                rows_taken = max(1, disp_h // 15) + 1
                ws.row_dimensions[current_row].height = disp_h * 0.75
                current_row += rows_taken
            except Exception:
                current_row += 1

            current_row += 2   # spacer between receipts

    return refs


# ── Public API ─────────────────────────────────────────────────────────────────

def build_themed_workbook(
    sections: dict,
    expense_period: str = "",
    employee_name: str = "Duane Hamilton",
) -> Workbook:
    """
    Build a fresh themed workbook from receipt data.

    sections: {"fuel": [...], "materials": [...], "misc": [...]}
    Each dict must have: date, vendor, amount, job_name, job_number,
    expense_description, _new_filename/_file, _image_path, _needs_review,
    _missing_fields.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    # Build Receipts sheet first to get hyperlink targets
    receipt_refs = _build_receipts_sheet(wb, sections)

    for col_letter, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width

    # ── Rows 1–5: form header ─────────────────────────────────────────────────
    _write_title(ws, 1)
    _write_meta_field(ws, 2, "Name:", employee_name)          # was "Employee Name:"
    _write_meta_field(ws, 3, "Expense Period:", expense_period)
    _write_note_row(ws, 4, "**Due Thursday by 12 p.m.**")
    _flood(ws, 5, _fill("CBD5E1"))
    ws.row_dimensions[5].height = 5

    current_row = 6
    subtotal_rows: dict[str, int] = {}

    SECTION_DEFS = [
        ("fuel",      ["Receipt\nNo.", "Date", "Job Name",       "", "Job Number",          "Amount", "Filename"], "FUEL"),
        ("materials", ["Receipt\nNo.", "Date", "Store / Job Name", "", "Job Number",         "Amount", "Filename"], "MATERIALS"),
        ("misc",      ["Receipt\nNo.", "Date", "Store / Job Name", "", "Expense Description", "Amount", "Filename"], "MISCELLENEOUS"),
    ]

    for category, col_headers, label in SECTION_DEFS:
        receipts   = sections.get(category, [])
        has_review = any(r.get("_needs_review") for r in receipts)

        _write_section_banner(ws, current_row, label)
        current_row += 1

        _write_col_headers(ws, current_row, col_headers, has_review=has_review)
        current_row += 1

        first_data_row = current_row

        for i, data in enumerate(receipts):
            fill_color = COLOR_ROW_PLAIN if i % 2 == 0 else COLOR_ROW_ALT
            _write_data_row(ws, current_row, i + 1, data, category, fill_color,
                            receipt_refs=receipt_refs)
            current_row += 1

        last_data_row = current_row - 1

        # Always insert a blank buffer row so the SUM formula can never
        # accidentally include the subtotal cell itself (empty-category guard).
        current_row += 1   # buffer row — left unstyled

        _write_subtotal(ws, current_row, first_data_row, last_data_row)
        subtotal_rows[category] = current_row
        current_row += 1

    _write_total(
        ws, current_row,
        subtotal_rows["fuel"],
        subtotal_rows["materials"],
        subtotal_rows["misc"],
    )

    return wb
