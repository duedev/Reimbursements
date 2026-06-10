"""
spreadsheet_theme.py
Builds a fresh, professionally-themed reimbursement workbook from extracted receipt data.
All sections are written in a single pass — no row-insertion hacks needed.
"""
from __future__ import annotations

from datetime import datetime, date
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── Color palette ──────────────────────────────────────────────────────────────
COLOR_TITLE_BG    = "2C3E50"   # dark charcoal — title + TOTAL rows
COLOR_TITLE_FG    = "FFFFFF"
COLOR_SECTION_BG  = "D97706"   # amber — section banners
COLOR_SECTION_FG  = "FFFFFF"
COLOR_HEADER_BG   = "3B82F6"   # steel blue — column header rows
COLOR_HEADER_FG   = "FFFFFF"
COLOR_META_BG     = "EBF5FB"   # light blue — employee / period rows
COLOR_ROW_PLAIN   = "FFFFFF"   # white — alternating data rows
COLOR_ROW_ALT     = "F1F5F9"   # very light gray — alternating data rows
COLOR_SUBTOTAL_BG = "FEF9C3"   # light yellow — subtotal rows
COLOR_SUBTOTAL_FG = "1F2937"   # near-black
COLOR_TOTAL_BG    = "2C3E50"   # dark charcoal — grand total row
COLOR_TOTAL_FG    = "FFFFFF"
COLOR_NOTE_BG     = "FEF9C3"   # due-date note row

# ── Column positions (1-indexed) ───────────────────────────────────────────────
COL_RECEIPT_NO = 1   # A
COL_DATE       = 2   # B
COL_NAME       = 3   # C  (C+D merged in data rows)
COL_JOB_NUMBER = 5   # E  (also "Expense Description" in misc)
COL_AMOUNT     = 6   # F
COL_FILENAME   = 7   # G  — new column

COLUMN_WIDTHS = {
    "A": 9.5,
    "B": 13.0,
    "C": 33.0,
    "D": 22.5,
    "E": 28.5,
    "F": 17.5,
    "G": 36.0,
}

ACCT_FORMAT = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
DATE_FORMAT  = "m/d/yy"
LAST_COL     = 7   # G


# ── Style helpers ──────────────────────────────────────────────────────────────

def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _font(bold: bool = False, color: str = "000000", size: int = 11) -> Font:
    return Font(bold=bold, color=color, size=size, name="Calibri")


def _border(color: str = "CCCCCC") -> Border:
    side = Side(style="thin", color=color)
    return Border(left=side, right=side, top=side, bottom=side)


def _align(h: str = "center", v: str = "center", wrap: bool = True) -> Alignment:
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def _flood(ws, row: int, fill: PatternFill, font: Font = None,
           border: Border = None, align: Alignment = None,
           cols: range = None):
    """Apply styles to every cell in a row."""
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


def _write_col_headers(ws, row: int, headers: list[str]):
    hdr_fill = _fill(COLOR_HEADER_BG)
    hdr_font = _font(bold=True, color=COLOR_HEADER_FG, size=11)
    hdr_border = _border("2E75B6")

    # Merge C+D
    ws.merge_cells(f"C{row}:D{row}")
    _flood(ws, row, hdr_fill, hdr_font, hdr_border)

    for col_idx, text in enumerate(headers, start=1):
        if col_idx == 4:
            continue  # D is the merged tail — skip writing value
        cell = ws.cell(row=row, column=col_idx, value=text)
        cell.alignment = _align(h="center", wrap=True)
    ws.row_dimensions[row].height = 32


def _write_data_row(ws, row: int, receipt_no: int, data: dict,
                    category: str, fill_color: str):
    row_fill   = _fill(fill_color)
    row_font   = _font(size=11)
    row_border = _border()

    _flood(ws, row, row_fill, row_font, row_border)

    # Merge C+D
    ws.merge_cells(f"C{row}:D{row}")

    # A — Receipt No.
    cell_a = ws.cell(row=row, column=COL_RECEIPT_NO, value=receipt_no)
    cell_a.font = _font(bold=True, size=11)
    cell_a.alignment = _align(h="center")

    # B — Date
    raw_date = data.get("date")
    if raw_date and hasattr(raw_date, "strftime"):
        date_val = raw_date
    elif raw_date and isinstance(raw_date, str):
        try:
            date_val = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            date_val = raw_date  # leave as month-name string
    else:
        date_val = None

    cell_b = ws.cell(row=row, column=COL_DATE, value=date_val)
    if isinstance(date_val, (datetime, date)):
        cell_b.number_format = DATE_FORMAT
    cell_b.alignment = _align(h="center")

    # C — Name (category-dependent label)
    vendor   = (data.get("vendor") or "").strip()
    job_name = (data.get("job_name") or "").strip()

    if category == "fuel":
        name_val = job_name if job_name else vendor
    else:
        if job_name and job_name not in vendor:
            name_val = f"{vendor}/{job_name}" if vendor else job_name
        else:
            name_val = vendor

    cell_c = ws.cell(row=row, column=COL_NAME, value=name_val)
    cell_c.alignment = _align(h="left", wrap=True)

    # E — Job Number or Expense Description
    if category == "misc":
        e_val = data.get("expense_description") or data.get("job_number")
    else:
        e_val = data.get("job_number")
    cell_e = ws.cell(row=row, column=COL_JOB_NUMBER, value=e_val)
    cell_e.alignment = _align(h="center")

    # F — Amount
    amount = data.get("amount")
    cell_f = ws.cell(row=row, column=COL_AMOUNT)
    if amount is not None:
        cell_f.value = float(amount)
        cell_f.number_format = ACCT_FORMAT
    cell_f.alignment = _align(h="right")

    # G — Filename
    filename = data.get("_new_filename") or data.get("_file") or ""
    cell_g = ws.cell(row=row, column=COL_FILENAME, value=filename)
    cell_g.alignment = _align(h="left", wrap=False)

    ws.row_dimensions[row].height = 18


def _write_subtotal(ws, row: int, first_data: int, last_data: int):
    sub_fill   = _fill(COLOR_SUBTOTAL_BG)
    sub_font   = _font(bold=True, size=11, color=COLOR_SUBTOTAL_FG)
    sub_border = _border("D1D5DB")

    _flood(ws, row, sub_fill, sub_font, sub_border)

    ws.cell(row=row, column=COL_JOB_NUMBER).alignment = _align(h="right")
    ws.cell(row=row, column=COL_JOB_NUMBER).value = "Subtotal"

    # SUM over the data range (reversed range = 0 in Excel, safe for empty section)
    cell_f = ws.cell(row=row, column=COL_AMOUNT,
                     value=f"=SUM(F{first_data}:F{last_data})")
    cell_f.number_format = ACCT_FORMAT
    cell_f.alignment = _align(h="right")
    ws.row_dimensions[row].height = 20


def _write_total(ws, row: int, fuel_sub: int, mat_sub: int, misc_sub: int):
    tot_fill = _fill(COLOR_TOTAL_BG)
    tot_font = _font(bold=True, color=COLOR_TOTAL_FG, size=12)

    _flood(ws, row, tot_fill, tot_font)

    ws.merge_cells(f"A{row}:D{row}")
    note = ws.cell(row=row, column=1, value="**Please attach receipts.**")
    note.alignment = _align(h="left", wrap=False)

    lbl = ws.cell(row=row, column=COL_JOB_NUMBER, value="TOTAL")
    lbl.alignment = _align(h="right")

    cell_f = ws.cell(row=row, column=COL_AMOUNT,
                     value=f"=F{fuel_sub}+F{mat_sub}+F{misc_sub}")
    cell_f.number_format = ACCT_FORMAT
    cell_f.alignment = _align(h="right")
    ws.row_dimensions[row].height = 24


# ── Public API ─────────────────────────────────────────────────────────────────

def build_themed_workbook(
    sections: dict,
    expense_period: str = "",
    employee_name: str = "Duane Hamilton",
) -> Workbook:
    """
    Build a fresh themed workbook from receipt data.

    sections: {
        "fuel":      [data_dicts, ...],
        "materials": [data_dicts, ...],
        "misc":      [data_dicts, ...],
    }
    Each data_dict must have keys: date, vendor, amount, job_name, job_number,
    expense_description, _new_filename (or _file).

    Returns a Workbook ready to be saved.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    for col_letter, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width

    # ── Rows 1–4: form header ─────────────────────────────────────────────────
    _write_title(ws, 1)
    _write_meta_field(ws, 2, "Employee Name:", employee_name)
    _write_meta_field(ws, 3, "Expense Period:", expense_period)
    _write_note_row(ws, 4, "**Due Thursday by 12 p.m.**")

    # Row 5: thin spacer stripe
    _flood(ws, 5, _fill("CBD5E1"))
    ws.row_dimensions[5].height = 5

    current_row = 6
    subtotal_rows: dict[str, int] = {}

    SECTION_DEFS = [
        (
            "fuel",
            ["Receipt\nNo.", "Date", "Job Name", "", "Job Number", "Amount", "Filename"],
            "FUEL",
        ),
        (
            "materials",
            ["Receipt\nNo.", "Date", "Store / Job Name", "", "Job Number", "Amount", "Filename"],
            "MATERIALS",
        ),
        (
            "misc",
            ["Receipt\nNo.", "Date", "Store / Job Name", "", "Expense Description", "Amount", "Filename"],
            "MISCELLENEOUS",
        ),
    ]

    for category, col_headers, label in SECTION_DEFS:
        receipts = sections.get(category, [])

        _write_section_banner(ws, current_row, label)
        current_row += 1

        _write_col_headers(ws, current_row, col_headers)
        current_row += 1

        first_data_row = current_row

        for i, data in enumerate(receipts):
            fill_color = COLOR_ROW_PLAIN if i % 2 == 0 else COLOR_ROW_ALT
            _write_data_row(ws, current_row, i + 1, data, category, fill_color)
            current_row += 1

        last_data_row = current_row - 1  # may equal first_data_row - 1 if empty
        _write_subtotal(ws, current_row, first_data_row, last_data_row)
        subtotal_rows[category] = current_row
        current_row += 1

    # ── Grand total row ───────────────────────────────────────────────────────
    _write_total(
        ws, current_row,
        subtotal_rows["fuel"],
        subtotal_rows["materials"],
        subtotal_rows["misc"],
    )

    return wb
