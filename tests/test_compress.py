"""Stored-image JPEG compression — the export-time re-encode with the built-in
quality judgment (_auto_jpeg_quality, no user dial) and the corruption-safe
temp-write → full-decode-verify → swap flow."""
import os

from PIL import Image

import process_receipts as pr


def test_compress_shrinks_and_converts_png(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(pr, "STORE_MAX_PX", 2000)
    p = tmp_path / "r.png"
    Image.new("RGB", (3000, 2400), (120, 180, 90)).save(p, format="PNG")
    before = p.stat().st_size

    out = pr.compress_image_file(p)

    assert out.suffix == ".jpg"
    assert out.exists() and not p.exists()      # PNG replaced by JPEG
    assert out.stat().st_size < before
    with Image.open(out) as im:
        assert max(im.size) <= 2000             # downscaled to the cap
    # No temp file left behind.
    assert not list(tmp_path.glob("*.compress-tmp.jpg"))


def test_compress_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", False)
    p = tmp_path / "r.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG", quality=95)
    out = pr.compress_image_file(p)
    assert out == p and p.exists()


def test_auto_quality_scales_with_frame_size():
    small = Image.new("RGB", (800, 600))        # 0.48 MP
    mid   = Image.new("RGB", (1600, 1200))      # 1.92 MP
    big   = Image.new("RGB", (2000, 2000))      # 4.0 MP
    qs = [pr._auto_jpeg_quality(i) for i in (small, mid, big)]
    assert qs == sorted(qs, reverse=True)       # bigger frame → lower quality
    assert all(70 <= q <= 90 for q in qs)       # sane document range


def test_compress_never_grows_an_optimized_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(pr, "STORE_MAX_PX", 4000)
    # A tiny, already heavily-compressed JPEG: re-encoding at a higher automatic
    # quality would only make it BIGGER — the original must be kept byte-for-byte.
    p = tmp_path / "small.jpg"
    Image.new("RGB", (300, 200), (200, 200, 200)).save(p, "JPEG", quality=30, optimize=True)
    before = p.read_bytes()
    out = pr.compress_image_file(p)
    assert out == p and p.read_bytes() == before


def test_compress_failure_returns_original_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    p = tmp_path / "not_an_image.jpg"
    p.write_bytes(b"definitely not an image")
    assert pr.compress_image_file(p) == p       # graceful no-op on bad input
    assert p.read_bytes() == b"definitely not an image"


def test_corrupt_output_keeps_original(tmp_path, monkeypatch):
    """The check-after: if the freshly written JPEG can't be fully decoded, the
    original file must be left untouched (no swap, no data loss)."""
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(pr, "STORE_MAX_PX", 2000)
    p = tmp_path / "r.png"
    Image.new("RGB", (1200, 900), (50, 60, 70)).save(p, format="PNG")
    before = p.read_bytes()
    monkeypatch.setattr(pr, "_image_intact", lambda _path: False)   # simulate corruption
    out = pr.compress_image_file(p)
    assert out == p and p.exists()
    assert p.read_bytes() == before             # original byte-for-byte intact
    assert not (tmp_path / "r.jpg").exists()    # corrupt result never swapped in
    assert not list(tmp_path.glob("*.compress-tmp.jpg"))


def test_image_intact_detects_truncation(tmp_path):
    good = tmp_path / "good.jpg"
    Image.new("RGB", (400, 300), (1, 2, 3)).save(good, "JPEG", quality=85)
    assert pr._image_intact(good) is True
    # Truncate the file mid-stream — header parses, pixel data doesn't.
    data = good.read_bytes()
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(data[: len(data) // 2])
    assert pr._image_intact(bad) is False


def test_autocrop_is_gone():
    """The auto-crop feature was removed outright — no lingering entry points."""
    for name in ("autocrop_receipt", "autocrop_image_file", "autocrop_analyze",
                 "AUTOCROP_ENABLED", "AUTOCROP_AGGRESSIVENESS", "JPEG_QUALITY"):
        assert not hasattr(pr, name)
