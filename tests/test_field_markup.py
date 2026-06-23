"""Tests for rules-based on-image field localization (no LLM).

locate_field_boxes() maps the final vendor/date/amount values back to the
RapidOCR line that produced each, returning normalized [x, y, w, h] boxes the UI
draws over the receipt image. These exercise the matching/priority logic and the
normalization helper with synthetic line-boxes (no real OCR engine needed).
"""
import process_receipts as pr


def _row(text, x0, y0, x1, y1, score=0.95):
    """A RapidOCR-style line box (axis-aligned 4-point polygon)."""
    return {"text": text, "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "score": score}


# ── _poly_to_norm_rect ─────────────────────────────────────────────────────────

def test_poly_to_norm_rect_normalizes():
    rect = pr._poly_to_norm_rect([[10, 20], [110, 20], [110, 40], [10, 40]], 200, 400)
    assert rect == [0.05, 0.05, 0.5, 0.05]


def test_poly_to_norm_rect_rejects_bad_size():
    assert pr._poly_to_norm_rect([[0, 0], [1, 1]], 0, 0) is None


# ── locate_field_boxes ─────────────────────────────────────────────────────────

def test_locate_field_boxes_basic():
    W, H = 200, 400
    rows = [
        _row("SHELL", 20, 10, 120, 40),
        _row("123 MAIN ST", 10, 50, 180, 70),
        _row("06/14/2026", 10, 90, 90, 110),
        _row("SUBTOTAL 40.00", 10, 300, 190, 320),
        _row("TOTAL 45.20", 10, 330, 190, 350),
    ]
    data = {"vendor": "Shell", "amount": 45.20, "date": "2026-06-14"}
    boxes = pr.locate_field_boxes(rows, W, H, data)

    assert set(boxes) == {"vendor", "date", "amount"}
    # every coordinate is normalized 0..1
    for v in boxes.values():
        assert all(0.0 <= c <= 1.0 for c in v)
    # amount lands on the TOTAL line (y≈330), not the subtotal
    assert abs(boxes["amount"][1] - 330 / H) < 0.01
    # vendor lands on the SHELL header line (y≈10)
    assert abs(boxes["vendor"][1] - 10 / H) < 0.02
    # date lands on the date line (y≈90)
    assert abs(boxes["date"][1] - 90 / H) < 0.02


def test_locate_amount_prefers_total_over_subtotal_when_value_shared():
    W, H = 200, 400
    rows = [
        _row("SUBTOTAL 45.20", 10, 300, 190, 320),
        _row("TOTAL 45.20", 10, 340, 190, 360),
    ]
    boxes = pr.locate_field_boxes(rows, W, H, {"amount": 45.20})
    assert "amount" in boxes
    assert abs(boxes["amount"][1] - 340 / H) < 0.01  # the TOTAL line wins


def test_locate_field_boxes_omits_unmatched_vendor():
    W, H = 200, 400
    rows = [_row("RANDOM HEADER", 10, 10, 100, 30), _row("TOTAL 9.99", 10, 300, 100, 320)]
    data = {"vendor": "Costco Wholesale", "amount": 9.99}
    boxes = pr.locate_field_boxes(rows, W, H, data)
    assert "vendor" not in boxes      # DB-only vendor not printed → no box
    assert "amount" in boxes


def test_locate_field_boxes_no_geometry_returns_empty():
    rows = [{"text": "TOTAL 5.00", "box": None, "score": 0.9}]
    assert pr.locate_field_boxes(rows, 100, 100, {"amount": 5.00}) == {}


def test_locate_vendor_box_via_match_src_after_canonicalization():
    # After canonicalization the displayed vendor is the canonical brand, which is
    # NOT printed verbatim (the line is the slogan). The box still maps via the
    # alias that actually matched (_vendor_match_src).
    W, H = 200, 400
    rows = [
        _row("HOW DOERS GET MORE DONE", 10, 12, 190, 32),
        _row("TOTAL 88.42", 10, 330, 190, 350),
    ]
    data = {"vendor": "The Home Depot", "amount": 88.42,
            "_vendor_match_src": "how doers get more done"}
    boxes = pr.locate_field_boxes(rows, W, H, data)
    assert "vendor" in boxes and "amount" in boxes
    assert abs(boxes["vendor"][1] - 12 / H) < 0.02   # lands on the slogan line


def test_locate_vendor_box_unchanged_without_match_src():
    # No _vendor_match_src and the canonical name isn't on any line → no vendor box
    # (additive fallback is inert when the key is absent — no behaviour change).
    W, H = 200, 400
    rows = [_row("RANDOM HEADER", 10, 10, 100, 30), _row("TOTAL 9.99", 10, 300, 100, 320)]
    boxes = pr.locate_field_boxes(rows, W, H, {"vendor": "The Home Depot", "amount": 9.99})
    assert "vendor" not in boxes and "amount" in boxes


def test_locate_field_boxes_empty_inputs():
    assert pr.locate_field_boxes([], 100, 100, {"amount": 5.0}) == {}
    assert pr.locate_field_boxes([_row("TOTAL 5.00", 0, 0, 10, 10)], 100, 100, None) == {}
