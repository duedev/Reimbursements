"""Tests for zip-archive intake (extract → import members → clean up)."""
import io
import zipfile

from PIL import Image

import process_receipts as pr
from process_receipts import extract_archive


def _png_bytes(color=(120, 120, 120), size=(60, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_extracts_images_and_pdfs_skips_junk(tmp_path):
    z = tmp_path / "batch.zip"
    _make_zip(z, {
        "a.png":      _png_bytes(),
        "sub/b.jpg":  _png_bytes((10, 20, 30)),
        "notes.txt":  b"ignore me",
        "fake.pdf":   b"%PDF-1.4 minimal",
        ".DS_Store":  b"junk",
    })
    dest = tmp_path / "out"
    members = extract_archive(z, dest)

    assert sorted(p.name for p in members) == ["a.png", "b.jpg", "fake.pdf"]
    assert all(p.exists() for p in members)
    # Members are flattened into dest — the 'sub/' nesting is dropped.
    assert all(p.parent == dest for p in members)


def test_zip_slip_is_neutralised(tmp_path):
    z = tmp_path / "evil.zip"
    _make_zip(z, {"../../escape.png": _png_bytes()})
    dest = tmp_path / "out"
    members = extract_archive(z, dest)

    assert len(members) == 1
    assert members[0].parent == dest          # written inside dest, by basename
    assert members[0].name == "escape.png"
    assert not (tmp_path.parent / "escape.png").exists()   # never escaped


def test_name_collisions_are_disambiguated(tmp_path):
    z = tmp_path / "dupes.zip"
    _make_zip(z, {"a/r.png": _png_bytes((1, 2, 3)), "b/r.png": _png_bytes((4, 5, 6))})
    members = extract_archive(z, tmp_path / "out")

    assert len(members) == 2
    assert len({p.name for p in members}) == 2   # both kept under distinct names


def test_file_cap_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ARCHIVE_MAX_FILES", 2)
    z = tmp_path / "many.zip"
    _make_zip(z, {f"img{i}.png": _png_bytes() for i in range(5)})
    members = extract_archive(z, tmp_path / "out")
    assert len(members) == 2


def test_byte_cap_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ARCHIVE_MAX_BYTES", 100)   # smaller than one decompressed PNG
    z = tmp_path / "big.zip"
    _make_zip(z, {"img.png": _png_bytes(size=(400, 400))})
    members = extract_archive(z, tmp_path / "out")
    assert members == []                                 # nothing fit under the cap
    # the partial file must not be left behind
    assert not any((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_bad_zip_returns_empty(tmp_path):
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip at all")
    assert extract_archive(bad, tmp_path / "out") == []
