"""Tests for stored-image JPEG compression."""
import os

from PIL import Image

import process_receipts as pr


def test_compress_shrinks_and_converts_png(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(pr, "JPEG_QUALITY", 85)
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


def test_compress_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", False)
    p = tmp_path / "r.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(p, "JPEG", quality=95)
    out = pr.compress_image_file(p)
    assert out == p and p.exists()


def test_lower_quality_yields_smaller_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(pr, "STORE_MAX_PX", 4000)
    noise = Image.frombytes("RGB", (400, 400), os.urandom(400 * 400 * 3))
    low = tmp_path / "low.jpg"
    high = tmp_path / "high.jpg"
    noise.save(low, "JPEG", quality=95)
    noise.save(high, "JPEG", quality=95)

    monkeypatch.setattr(pr, "JPEG_QUALITY", 40)
    pr.compress_image_file(low)
    monkeypatch.setattr(pr, "JPEG_QUALITY", 90)
    pr.compress_image_file(high)

    assert low.stat().st_size < high.stat().st_size


def test_compress_failure_returns_original_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    p = tmp_path / "not_an_image.jpg"
    p.write_bytes(b"definitely not an image")
    assert pr.compress_image_file(p) == p       # graceful no-op on bad input
