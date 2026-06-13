"""Tests for the pre-OCR black-&-white (grayscale) pre-pass."""
from PIL import Image

import process_receipts as pr
from process_receipts import grayscale_image_file


def _color_receipt(size=(400, 600)):
    img = Image.new("RGB", size, (200, 180, 160))
    img.paste(Image.new("RGB", (120, 120), (10, 90, 200)), (40, 40))  # a colour block
    return img


def test_converts_to_grayscale_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "GRAYSCALE_ENABLED", True)
    p = tmp_path / "r.png"
    _color_receipt().save(p, format="PNG")
    assert grayscale_image_file(p) is True
    with Image.open(p) as img:
        assert img.mode == "L"           # single channel
        assert img.size == (400, 600)    # dimensions untouched
        assert p.suffix == ".png"        # suffix preserved → downstream still finds it


def test_preserves_jpeg_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "GRAYSCALE_ENABLED", True)
    p = tmp_path / "r.jpg"
    _color_receipt().save(p, format="JPEG", quality=92)
    assert grayscale_image_file(p) is True
    assert p.suffix == ".jpg"
    with Image.open(p) as img:
        assert img.mode == "L"


def test_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "GRAYSCALE_ENABLED", False)
    p = tmp_path / "r.png"
    _color_receipt().save(p, format="PNG")
    assert grayscale_image_file(p) is False
    with Image.open(p) as img:
        assert img.mode == "RGB"         # left exactly as-is


def test_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "GRAYSCALE_ENABLED", True)
    p = tmp_path / "r.png"
    _color_receipt().save(p, format="PNG")
    assert grayscale_image_file(p) is True
    assert grayscale_image_file(p) is True   # a second pass is still safe
    with Image.open(p) as img:
        assert img.mode == "L"


def test_missing_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "GRAYSCALE_ENABLED", True)
    assert grayscale_image_file(tmp_path / "nope.png") is False   # best-effort, never raises
