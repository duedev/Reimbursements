"""Tests for auto-rotate (text-upright orientation), fully local — no model.

Two tiers:
  • autorotate_image_file: bake a photo's EXIF Orientation into the pixels.
  • _ocr_lines_best_orientation: when the upright OCR read is weak, try the three
    90° rotations and keep whichever RapidOCR reads best (scored by
    _ocr_orientation_score), rewriting the file in place.
"""
from PIL import Image

import process_receipts as pr


# ── EXIF tier: autorotate_image_file ───────────────────────────────────────────

def test_autorotate_bakes_in_exif(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "AUTOROTATE_ENABLED", True)
    p = tmp_path / "r.jpg"
    img = Image.new("RGB", (100, 50), "white")
    exif = img.getexif()
    exif[0x0112] = 6                       # Orientation 6 → rotate 90° to display upright
    img.save(p, format="JPEG", exif=exif)

    assert pr.autorotate_image_file(p) is True
    with Image.open(p) as out:
        assert out.size == (50, 100)       # pixels rotated upright (100x50 → 50x100)
        assert pr._exif_orientation(out) in (0, 1)   # tag cleared so it won't double-apply


def test_autorotate_noop_when_already_upright(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "AUTOROTATE_ENABLED", True)
    p = tmp_path / "r.jpg"
    Image.new("RGB", (40, 20), "white").save(p)    # no orientation tag
    assert pr.autorotate_image_file(p) is False


def test_autorotate_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "AUTOROTATE_ENABLED", False)
    p = tmp_path / "r.jpg"
    img = Image.new("RGB", (10, 10), "white")
    exif = img.getexif(); exif[0x0112] = 6
    img.save(p, format="JPEG", exif=exif)
    assert pr.autorotate_image_file(p) is False


# ── OCR scoring ────────────────────────────────────────────────────────────────

def test_ocr_orientation_score_ranks_more_clear_text_higher():
    strong = [{"text": "HELLO WORLD 123", "score": 0.95}]
    weak   = [{"text": "x", "score": 0.30}]
    assert pr._ocr_orientation_score(strong) > pr._ocr_orientation_score(weak)
    assert pr._ocr_orientation_score([]) == 0.0


# ── OCR-guided tier: _ocr_lines_best_orientation ───────────────────────────────

def test_ocr_lines_best_orientation_rotates_to_best(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "AUTOROTATE_ENABLED", True)
    monkeypatch.setattr(pr, "ORIENT_BY_OCR", True)
    p = tmp_path / "r.png"
    Image.new("RGB", (200, 400), "white").save(p)  # real image so size > 0

    # Upright reads as noise; only the 180° candidate reads as clean text.
    def fake_lines(path):
        s = str(path)
        if ".orient_180" in s:
            return ([{"text": "TOTAL 45.20 SHELL RECEIPT", "score": 0.95}], 200, 400)
        if ".orient_" in s:
            return ([{"text": "x", "score": 0.2}], 400, 200)
        return ([{"text": "??", "score": 0.2}], 200, 400)   # weak upright read

    monkeypatch.setattr(pr, "_extract_local_ocr_lines", fake_lines)
    rows, w, h, note = pr._ocr_lines_best_orientation(p)
    assert "180" in note
    assert rows and rows[0]["text"].startswith("TOTAL 45.20")


def test_ocr_lines_best_orientation_keeps_upright_when_strong(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "AUTOROTATE_ENABLED", True)
    monkeypatch.setattr(pr, "ORIENT_BY_OCR", True)
    p = tmp_path / "r.png"
    Image.new("RGB", (200, 400), "white").save(p)

    # A strong upright read should short-circuit (no rotation search, no note).
    strong = [{"text": "SHELL UNLEADED TOTAL 45.20 06/14/2026 THANK YOU", "score": 0.95}]
    monkeypatch.setattr(pr, "_extract_local_ocr_lines", lambda path: (strong, 200, 400))
    rows, w, h, note = pr._ocr_lines_best_orientation(p)
    assert note == ""
    assert rows is strong
