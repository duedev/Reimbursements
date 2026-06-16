"""Tests for the conservative receipt auto-crop."""
import base64

from PIL import Image

from process_receipts import (
    autocrop_analyze, autocrop_receipt, autocrop_image_file, encode_image,
    _autocrop_params,
)


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
    # Content fills the frame — after margin the bbox equals the full image.
    img = _receipt_on_background(box=(5, 5, 995, 995))
    assert autocrop_receipt(img).size == img.size


def test_tiny_content_crops():
    # Gate removed: a tiny receipt on a large background is now cropped,
    # no matter how small the kept_ratio.
    img = _receipt_on_background(box=(450, 450, 550, 550))
    out = autocrop_receipt(img)
    assert out.size != img.size


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


# ── autocrop_analyze: diagnostics that drive both the pipeline and the test UI ──

def test_analyze_reports_crop_for_bordered_receipt():
    info = autocrop_analyze(_receipt_on_background())
    assert info["would_crop"] is True
    assert info["bbox"] is not None
    assert 0.40 <= info["kept_ratio"] <= 0.95
    assert info["reason"]


def test_analyze_skips_full_frame_with_reason():
    # Full-frame content: after adding the safety margin the bbox collapses to the
    # whole image, so would_crop is False (nothing to trim).
    info = autocrop_analyze(_receipt_on_background(box=(5, 5, 995, 995)))
    assert info["would_crop"] is False
    assert "no meaningful border" in info["reason"]


def test_analyze_crops_tiny_content():
    # Accept/reject gate removed: tiny content on a large background is always
    # cropped — the kept_ratio just tells the caller how much was trimmed.
    info = autocrop_analyze(_receipt_on_background(box=(450, 450, 550, 550)))
    assert info["would_crop"] is True
    assert info["kept_ratio"] < 0.30


def test_analyze_skips_too_small_image():
    info = autocrop_analyze(Image.new("RGB", (40, 40), (255, 255, 255)))
    assert info["would_crop"] is False
    assert "too small" in info["reason"]


def test_analyze_and_receipt_agree():
    # The thin apply step must crop exactly when analyze says it would.
    img = _receipt_on_background()
    info = autocrop_analyze(img)
    out = autocrop_receipt(img)
    assert (out.size != img.size) == info["would_crop"]


# ── Aggressiveness dial ────────────────────────────────────────────────────────

def test_autocrop_params_monotonic_in_aggressiveness():
    lo, hi = _autocrop_params(0), _autocrop_params(100)
    assert hi["min_ratio"] < lo["min_ratio"]   # accepts tighter crops
    assert hi["margin"]    < lo["margin"]       # trims closer
    assert hi["threshold"] > lo["threshold"]    # ignores fainter gradients
    assert hi["max_ratio"] >= lo["max_ratio"]   # fires on smaller borders


def test_autocrop_params_clamped():
    assert _autocrop_params(-50) == _autocrop_params(0)
    assert _autocrop_params(999) == _autocrop_params(100)


def test_higher_aggressiveness_crops_tighter():
    # Accept/reject gate removed: both aggressiveness levels now crop when there is
    # a detectable border.  Confirm that higher aggressiveness produces a smaller
    # kept_ratio (i.e. tighter crop), proving the dial still has an effect.
    img = _receipt_on_background(box=(250, 200, 750, 800))
    lo = autocrop_analyze(img, aggressiveness=0)
    hi = autocrop_analyze(img, aggressiveness=100)
    assert lo["would_crop"] is True
    assert hi["would_crop"] is True
    assert hi["kept_ratio"] <= lo["kept_ratio"]
