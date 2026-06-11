"""Tests for the conservative receipt auto-crop."""
import base64

from PIL import Image

from process_receipts import autocrop_receipt, autocrop_image_file, encode_image


def _receipt_on_background(size=(1000, 1000), box=(200, 150, 800, 900),
                           bg=(255, 255, 255), fg=(60, 60, 60)):
    img = Image.new("RGB", size, bg)
    img.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), fg), box[:2])
    return img


def test_crops_centered_receipt_with_margin():
    img = _receipt_on_background()
    out = autocrop_receipt(img)
    assert out.size != img.size
    # Content box is 600x750; margin is 2% of original dims (20px each side)
    assert 600 <= out.width <= 660
    assert 750 <= out.height <= 810


def test_full_frame_receipt_unchanged():
    # Content fills the frame — trimming would be negligible (>95% kept)
    img = _receipt_on_background(box=(5, 5, 995, 995))
    assert autocrop_receipt(img).size == img.size


def test_tiny_content_guard_unchanged():
    # Crop would keep <40% of the area — too aggressive, skip
    img = _receipt_on_background(box=(450, 450, 550, 550))
    assert autocrop_receipt(img).size == img.size


def test_solid_color_unchanged():
    img = Image.new("RGB", (800, 800), (250, 250, 250))
    assert autocrop_receipt(img).size == img.size


def test_autocrop_image_file_in_place(tmp_path):
    p = tmp_path / "r.jpg"
    _receipt_on_background().save(p, format="JPEG", quality=92)
    before = p.stat().st_size
    assert autocrop_image_file(p) is True
    with Image.open(p) as img:
        assert img.size != (1000, 1000)
    assert p.stat().st_size < before


def test_encode_image_round_trip(tmp_path):
    p = tmp_path / "r.jpg"
    _receipt_on_background().save(p, format="JPEG", quality=92)
    b64, mime = encode_image(p)
    assert mime == "image/jpeg"
    raw = base64.b64decode(b64)
    assert raw[:2] == b"\xff\xd8"  # JPEG magic bytes
