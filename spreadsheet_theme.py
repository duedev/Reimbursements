"""
spreadsheet_theme.py
Builds a fresh, professionally-themed reimbursement workbook from extracted receipt data.
"""
from __future__ import annotations

from datetime import datetime, date
from pathlib import Path
from typing import Optional
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ── Color palette ──────────────────────────────────────────────────────────────
COLOR_TITLE_BG    = "2C3E50"
COLOR_TITLE_FG    = "FFFFFF"
COLOR_SECTION_BG  = "1E40AF"
COLOR_SECTION_FG  = "FFFFFF"
COLOR_HEADER_BG   = "3B82F6"
COLOR_HEADER_FG   = "FFFFFF"
COLOR_META_BG     = "EBF5FB"
COLOR_ROW_PLAIN   = "FFFFFF"
COLOR_ROW_ALT     = "F1F5F9"
COLOR_SUBTOTAL_BG = "FEF9C3"
COLOR_SUBTOTAL_FG = "1F2937"
COLOR_TOTAL_BG    = "2C3E50"
COLOR_TOTAL_FG    = "FFFFFF"
COLOR_NOTE_BG     = "FEF9C3"
COLOR_FLAG_BG     = "FEE2E2"
COLOR_RECEIPT_SEP = "B0B8C1"

# ── Column positions (1-indexed) ───────────────────────────────────────────────
# A  B  C+D  E                  F       G         H        I
# #  Date  Name  Job#/Desc  Amount  Filename  Summary  Review Notes
COL_RECEIPT_NO = 1   # A
COL_DATE       = 2   # B
COL_NAME       = 3   # C  (C+D merged)
COL_JOB_NUMBER = 5   # E
COL_AMOUNT     = 6   # F
COL_FILENAME   = 7   # G  ← Filename
COL_SUMMARY    = 8   # H  ← Summary
COL_FLAG       = 9   # I  ← Review Notes

COLUMN_WIDTHS = {
    "A": 9.5,
    "B": 13.0,
    "C": 33.0,
    "D": 22.5,
    "E": 28.5,
    "F": 17.5,
    "G": 36.0,   # Filename
    "H": 48.0,   # Summary — wide for a sentence
}

ACCT_FORMAT = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
DATE_FORMAT  = "m/d/yy"
LAST_COL     = 9   # I — all row bands extend to Review Notes column


# ── Style helpers ──────────────────────────────────────────────────────────────

def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _font(bold: bool = False, color: str = "000000", size: int = 11) -> Font:
    return Font(bold=bold, color=color, size=size, name="Calibri")


def _border(color: str = "CCCCCC") -> Border:
    thin = Side(style="thin", color=color)
    return Border(left=thin, right=thin, top=thin, bottom=thin)


def _receipt_sep_border() -> Border:
    """Medium bottom border as a visual separator between receipts."""
    thin   = Side(style="thin",   color="CCCCCC")
    medium = Side(style="medium", color=COLOR_RECEIPT_SEP)
    return Border(left=thin, right=thin, top=thin, bottom=medium)


def _align(h: str = "center", v: str = "center", wrap: bool = True) -> Alignment:
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def _flood(ws, row: int, fill: PatternFill, font: Font = None,
           border: Border = None, align: Alignment = None,
           cols: range = None):
    for col in (cols or range(1, LAST_COL + 1)):
        cell = ws.cell(row=row, column=col)
        cell.fill = fill
        if font:   cell.font   = font
        if border: cell.border = border
        if align:  cell.alignment = align


# ── Row writers ────────────────────────────────────────────────────────────────

def _write_title(ws, row: int):
    _flood(ws, row, _fill(COLOR_TITLE_BG), cols=range(1, LAST_COL + 1))
    ws.merge_cells(f"A{row}:I{row}")
    cell = ws.cell(row=row, column=1, value="Expense Reimbursement Form")
    cell.font      = _font(bold=True, color=COLOR_TITLE_FG, size=16)
    cell.fill      = _fill(COLOR_TITLE_BG)
    cell.alignment = _align(h="center")
    ws.row_dimensions[row].height = 30


def _write_meta_field(ws, row: int, label: str, value: str,
                      label_col: int = 2, value_col: int = 4):
    meta_fill = _fill(COLOR_META_BG)
    _flood(ws, row, meta_fill)
    lbl = ws.cell(row=row, column=label_col, value=label)
    lbl.font = _font(bold=True, size=11); lbl.fill = meta_fill; lbl.alignment = _align(h="right")
    end_col = value_col + 1
    ws.merge_cells(f"{get_column_letter(value_col)}{row}:{get_column_letter(end_col)}{row}")
    val = ws.cell(row=row, column=value_col, value=value)
    val.font = _font(size=11); val.fill = meta_fill; val.alignment = _align(h="left")
    ws.row_dimensions[row].height = 18


def _write_note_row(ws, row: int, text: str):
    note_fill = _fill(COLOR_NOTE_BG)
    _flood(ws, row, note_fill)
    ws.merge_cells(f"D{row}:I{row}")
    cell = ws.cell(row=row, column=4, value=text)
    cell.font      = _font(bold=True, size=10, color="92400E")
    cell.fill      = note_fill
    cell.alignment = _align(h="center")
    ws.row_dimensions[row].height = 18


def _write_section_banner(ws, row: int, label: str):
    _flood(ws, row, _fill(COLOR_SECTION_BG), cols=range(1, LAST_COL + 1))
    ws.merge_cells(f"A{row}:I{row}")
    cell = ws.cell(row=row, column=1, value=f"  {label}")
    cell.font      = _font(bold=True, color=COLOR_SECTION_FG, size=13)
    cell.fill      = _fill(COLOR_SECTION_BG)
    cell.alignment = _align(h="left", wrap=False)
    ws.row_dimensions[row].height = 24


def _write_col_headers(ws, row: int, headers: list[str], show_flags: bool = False):
    hdr_fill   = _fill(COLOR_HEADER_BG)
    hdr_font   = _font(bold=True, color=COLOR_HEADER_FG, size=11)
    hdr_border = _border("2E75B6")

    _flood(ws, row, hdr_fill, hdr_font, hdr_border)
    ws.merge_cells(f"C{row}:D{row}")

    for col_idx, text in enumerate(headers, start=1):
        if col_idx == 4:
            continue  # D is merged tail
        cell = ws.cell(row=row, column=col_idx, value=text)
        cell.alignment = _align(h="center", wrap=True)

    if show_flags:
        cell_i = ws.cell(row=row, column=COL_FLAG, value="Review Notes")
        cell_i.font = hdr_font; cell_i.fill = hdr_fill
        cell_i.border = hdr_border; cell_i.alignment = _align(h="center", wrap=True)

    ws.row_dimensions[row].height = 32


def _write_data_row(ws, row: int, receipt_no: int, data: dict,
                    category: str, fill_color: str, show_flags: bool = False,
                    hyperlink_target: Optional[str] = None):
    row_fill   = _fill(fill_color)
    row_border = _receipt_sep_border()

    _flood(ws, row, row_fill, _font(size=11), row_border)
    ws.merge_cells(f"C{row}:D{row}")

    # A — Receipt No.
    cell_a = ws.cell(row=row, column=COL_RECEIPT_NO, value=receipt_no)
    cell_a.font = _font(bold=True, size=11); cell_a.alignment = _align(h="center")

    # B — Date
    raw_date = data.get("date")
    if raw_date and hasattr(raw_date, "strftime"):
        date_val = raw_date
    elif raw_date and isinstance(raw_date, str):
        try:
            date_val = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            date_val = raw_date
    else:
        date_val = None
    cell_b = ws.cell(row=row, column=COL_DATE, value=date_val)
    if isinstance(date_val, (datetime, date)):
        cell_b.number_format = DATE_FORMAT
    cell_b.alignment = _align(h="center")

    # C — Name
    vendor   = (data.get("vendor")   or "").strip()
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

    # G — Filename (with optional hyperlink to image sheet)
    filename = data.get("_new_filename") or data.get("_file") or ""
    cell_g = ws.cell(row=row, column=COL_FILENAME, value=filename)
    if hyperlink_target and filename:
        cell_g.hyperlink = hyperlink_target
        cell_g.font = Font(bold=False, color="1155CC", underline="single",
                           name="Calibri", size=11)
    cell_g.alignment = _align(h="left", wrap=False)

    # H — Summary
    summary = (data.get("summary") or data.get("ai_summary") or "").strip()
    cell_h = ws.cell(row=row, column=COL_SUMMARY, value=summary or None)
    cell_h.font      = _font(size=10, color="4B5563")
    cell_h.alignment = _align(h="left", wrap=True)

    # I — Review Notes (only when flags column active)
    if show_flags:
        flag_text = data.get("_flag") or ""
        cell_i = ws.cell(row=row, column=COL_FLAG, value=flag_text or None)
        if flag_text:
            cell_i.fill = _fill(COLOR_FLAG_BG); cell_i.font = _font(size=10, color="991B1B")
        else:
            cell_i.fill = _fill(fill_color)
        cell_i.alignment = _align(h="left", wrap=True)
        cell_i.border    = _receipt_sep_border()

    ws.row_dimensions[row].height = 20


def _write_subtotal(ws, row: int, first_data: int, last_data: int):
    sub_fill   = _fill(COLOR_SUBTOTAL_BG)
    sub_font   = _font(bold=True, size=11, color=COLOR_SUBTOTAL_FG)
    sub_border = _border("D1D5DB")
    _flood(ws, row, sub_fill, sub_font, sub_border)
    ws.cell(row=row, column=COL_JOB_NUMBER).value     = "Subtotal"
    ws.cell(row=row, column=COL_JOB_NUMBER).alignment = _align(h="right")
    cell_f = ws.cell(row=row, column=COL_AMOUNT)
    cell_f.value = f"=SUM(F{first_data}:F{last_data})" if last_data >= first_data else 0
    cell_f.number_format = ACCT_FORMAT; cell_f.alignment = _align(h="right")
    ws.row_dimensions[row].height = 20


def _write_total(ws, row: int, fuel_sub: int, mat_sub: int, misc_sub: int):
    _flood(ws, row, _fill(COLOR_TOTAL_BG), _font(bold=True, color=COLOR_TOTAL_FG, size=12))
    ws.merge_cells(f"A{row}:D{row}")
    ws.cell(row=row, column=1, value="**Please attach receipts.**").alignment = _align(h="left", wrap=False)
    ws.cell(row=row, column=COL_JOB_NUMBER, value="TOTAL").alignment = _align(h="right")
    cell_f = ws.cell(row=row, column=COL_AMOUNT, value=f"=F{fuel_sub}+F{mat_sub}+F{misc_sub}")
    cell_f.number_format = ACCT_FORMAT; cell_f.alignment = _align(h="right")
    ws.row_dimensions[row].height = 24


# ── Image sheet builder ────────────────────────────────────────────────────────

_IMG_ROW_HEIGHT_PT = 14
_IMG_MAX_W_PX      = 720
_IMG_MAX_H_PX      = 480
_IMG_ROWS          = 27


def _build_image_sheet(wb: Workbook, sheet_name: str, receipts: list[dict]) -> list[str]:
    """Build a per-category image sheet. Returns anchor cell refs for hyperlinks."""
    ws = wb.create_sheet(title=sheet_name)
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 38

    current_row = 1
    anchors: list[str] = []

    ws.merge_cells(f"A{current_row}:E{current_row}")
    title_cell = ws.cell(row=current_row, column=1, value=f"{sheet_name} — Receipt Images")
    title_cell.font      = _font(bold=True, color=COLOR_TITLE_FG, size=14)
    title_cell.fill      = _fill(COLOR_TITLE_BG)
    title_cell.alignment = _align(h="center")
    ws.row_dimensions[current_row].height = 28
    current_row += 1

    if not receipts:
        ws.cell(row=current_row, column=1, value="No receipts in this category.")
        return anchors

    hdr_fill = _fill(COLOR_HEADER_BG); hdr_font = _font(bold=True, color=COLOR_HEADER_FG, size=10)
    for col_idx, hdr in enumerate(["#", "Date", "Vendor / Name", "Amount", "Filename"], 1):
        cell = ws.cell(row=current_row, column=col_idx, value=hdr)
        cell.font = hdr_font; cell.fill = hdr_fill
        cell.alignment = _align(h="center"); cell.border = _border("2E75B6")
    ws.row_dimensions[current_row].height = 18
    current_row += 1

    for i, data in enumerate(receipts):
        img_path_str = data.get("_image_path", "")
        date_val     = data.get("date") or ""
        vendor       = data.get("vendor") or data.get("job_name") or ""
        amount       = data.get("amount") or 0
        filename     = data.get("_new_filename") or data.get("_file") or ""

        anchors.append(f"A{current_row}")
        row_fill   = _fill(COLOR_ROW_PLAIN if i % 2 == 0 else COLOR_ROW_ALT)
        row_border = _receipt_sep_border()
        for col_idx, val in enumerate([i + 1, date_val, vendor, f"${float(amount):.2f}", filename], 1):
            cell = ws.cell(row=current_row, column=col_idx, value=val)
            cell.font      = _font(bold=(col_idx == 1), size=10)
            cell.fill      = row_fill; cell.border = row_border
            cell.alignment = _align(h="center" if col_idx in (1, 4) else "left", wrap=False)
        ws.row_dimensions[current_row].height = 16
        current_row += 1

        if img_path_str and Path(img_path_str).exists():
            try:
                from io import BytesIO
                from openpyxl.drawing.image import Image as XLImage
                from PIL import Image as PILImage

                with PILImage.open(img_path_str) as pil_img:
                    orig_w, orig_h = pil_img.size
                    if getattr(pil_img, "format", None) == "MPO":
                        buf = BytesIO()
                        pil_img.convert("RGB").save(buf, "JPEG", quality=92)
                        buf.seek(0); img_source = buf
                    else:
                        img_source = img_path_str

                scale = min(_IMG_MAX_W_PX / orig_w, _IMG_MAX_H_PX / orig_h, 1.0)
                img_w, img_h = int(orig_w * scale), int(orig_h * scale)
                rows_needed  = max(int(img_h * 0.75 / _IMG_ROW_HEIGHT_PT) + 2, _IMG_ROWS)

                xl_img = XLImage(img_source)
                xl_img.width = img_w; xl_img.height = img_h
                ws.add_image(xl_img, f"A{current_row}")
                for r in range(current_row, current_row + rows_needed):
                    ws.row_dimensions[r].height = _IMG_ROW_HEIGHT_PT
                current_row += rows_needed
            except Exception as exc:
                ws.cell(row=current_row, column=1, value=f"[Image error: {exc}]").font = _font(size=9, color="991B1B")
                ws.row_dimensions[current_row].height = 14
                current_row += 1
        else:
            ws.cell(row=current_row, column=1, value=f"[Image not available: {filename}]").font = _font(size=9, color="6B7280")
            ws.row_dimensions[current_row].height = 14
            current_row += 1

        ws.row_dimensions[current_row].height = 8
        current_row += 1

    return anchors


# ── Public API ─────────────────────────────────────────────────────────────────

def build_themed_workbook(
    sections: dict,
    expense_period: str = "",
    employee_name: str = "Duane Hamilton",
) -> Workbook:
    """
    Build a themed workbook from receipt data.

    sections: {"fuel": [...], "mats": [...], "misc": [...]}

    Column layout (A–I):
      A: Receipt#  B: Date  C+D: Name  E: Job#/Desc  F: Amount
      G: Filename  H: Summary  I: Review Notes

    Row layout (no spacer row 5):
      1: Title  2: Employee  3: Period  4: Note  5+: Sections
    """
    has_flags = any(d.get("_flag") for receipts in sections.values() for d in receipts)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    for col_letter, width in COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width
    ws.column_dimensions["I"].width = 40.0

    # Build image sheets first (tabs named by full category name)
    IMAGE_SHEET_DEFS = [
        ("fuel",  "Fuel"),
        ("mats",  "Materials"),
        ("misc",  "Miscellaneous"),
    ]
    image_links: dict[tuple, str] = {}
    for category, sheet_name in IMAGE_SHEET_DEFS:
        receipts = sections.get(category, [])
        anchors  = _build_image_sheet(wb, sheet_name, receipts)
        safe     = sheet_name.replace("'", "''")
        for i, cell_ref in enumerate(anchors):
            image_links[(category, i)] = f"#'{safe}'!{cell_ref}"

    # Summary sheet — rows 1-4 header, row 5+ sections (no spacer row)
    _write_title(ws, 1)
    _write_meta_field(ws, 2, "Employee:", employee_name)
    _write_meta_field(ws, 3, "Expense Period:", expense_period)
    _write_note_row(ws, 4, "**Due Thursday by 12 p.m.**")

    current_row = 5
    subtotal_rows: dict[str, int] = {}

    SECTION_DEFS = [
        ("fuel",  ["Receipt\nNo.", "Date", "Job Name",         "", "Job Number",           "Amount", "Filename", "Summary"], "Fuel"),
        ("mats",  ["Receipt\nNo.", "Date", "Store / Job Name", "", "Job Number",           "Amount", "Filename", "Summary"], "Materials"),
        ("misc",  ["Receipt\nNo.", "Date", "Store / Job Name", "", "Expense Description",  "Amount", "Filename", "Summary"], "Miscellaneous"),
    ]

    for category, col_headers, label in SECTION_DEFS:
        receipts = sections.get(category, [])
        _write_section_banner(ws, current_row, label); current_row += 1
        _write_col_headers(ws, current_row, col_headers, show_flags=has_flags); current_row += 1
        first_data_row = current_row

        for i, data in enumerate(receipts):
            _write_data_row(
                ws, current_row, i + 1, data, category,
                COLOR_ROW_PLAIN if i % 2 == 0 else COLOR_ROW_ALT,
                show_flags=has_flags,
                hyperlink_target=image_links.get((category, i)),
            )
            current_row += 1

        last_data_row = current_row - 1
        _write_subtotal(ws, current_row, first_data_row, last_data_row)
        subtotal_rows[category] = current_row
        current_row += 1

    _write_total(ws, current_row,
                 subtotal_rows["fuel"], subtotal_rows["mats"], subtotal_rows["misc"])

    return wb
