"""The per-category image sheet must render the receipt picture ABOVE its data.

Regression guard for the layout change: a Summary hyperlink should land on the
receipt header with the image immediately below (in view), and the metadata row
now sits beneath the image rather than above it.
"""
from openpyxl import Workbook
from openpyxl.utils.cell import coordinate_to_tuple
from PIL import Image as PILImage

import spreadsheet_theme


def _image_row_1idx(img):
    """1-indexed top row of an embedded image (anchor may be a str or marker)."""
    anc = img.anchor
    if isinstance(anc, str):
        return coordinate_to_tuple(anc)[0]
    return anc._from.row + 1


def _make_png(path):
    PILImage.new("RGB", (48, 72), (210, 210, 210)).save(path, "PNG")


def test_image_is_above_metadata_row(tmp_path):
    png = tmp_path / "r.png"
    _make_png(png)
    receipts = [{
        "vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
        "_category": "fuel", "_image_path": str(png), "_new_filename": "r.png",
    }]
    wb = Workbook()
    anchors = spreadsheet_theme._build_image_sheet(wb, "Fuel", receipts, "fuel")
    ws = wb["Fuel"]

    assert ws._images, "an image should be embedded"
    image_row = _image_row_1idx(ws._images[0])              # 1-indexed
    header_row = int(anchors[0][1:])                        # e.g. "A3" → 3
    data_rows = [c.row for row in ws.iter_rows() for c in row if c.value == "Shell"]
    assert data_rows, "metadata row with the vendor should exist"
    data_row = min(data_rows)

    # Header (hyperlink target) is above the image, image is above the data.
    assert header_row < image_row < data_row
