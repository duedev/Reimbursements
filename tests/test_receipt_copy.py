"""Filable receipt-copy rendering for emailed (text/HTML) e-receipts.

The office requires the actual receipt document, not just the parsed fields, so an
emailed receipt (which has no photo) is rendered to a JPEG that flows into the
report. Faithful render via wkhtmltoimage when present; pure-Python PIL fallback
otherwise — tested here against the fallback (no binary in CI).
"""
from pathlib import Path

from PIL import Image

import process_receipts as pr


def _is_jpeg(p: Path) -> bool:
    with Image.open(p) as im:
        return im.format == "JPEG" and im.width > 0 and im.height > 0


def test_text_to_jpeg_writes_image(tmp_path):
    out = tmp_path / "r.receipt.jpg"
    res = pr._text_to_jpeg("Chevron\nTotal: $52.10\n06/22/2026", out)
    assert res == out and out.exists() and _is_jpeg(out)


def test_text_to_jpeg_handles_empty_body(tmp_path):
    out = tmp_path / "blank.receipt.jpg"
    res = pr._text_to_jpeg("", out)
    assert res is not None and out.exists() and _is_jpeg(out)


def test_render_receipt_copy_txt_uses_text_fallback(tmp_path):
    src = tmp_path / "msg.txt"
    src.write_text("Shell\nGallons 10.0\nTotal $40.00")
    steps = []
    out = pr.render_receipt_copy(src, src.read_text(), steps)
    assert out is not None and Path(out).exists() and _is_jpeg(Path(out))
    assert any(s.get("step") == "receipt_copy" and s.get("ok") for s in steps)


def test_render_receipt_copy_html_falls_back_without_binary(tmp_path):
    # No wkhtmltoimage in CI → faithful render returns None → text fallback kicks in.
    src = tmp_path / "msg.html"
    src.write_text("<body><h2>Chevron</h2><p>Total: $52.10</p></body>")
    out = pr.render_receipt_copy(src, "Chevron Total: $52.10", None)
    assert out is not None and Path(out).exists() and _is_jpeg(Path(out))


def test_html_render_none_without_binary(tmp_path):
    src = tmp_path / "x.html"
    src.write_text("<p>hi</p>")
    assert pr._render_html_to_jpeg(src, tmp_path / "x.jpg") is None


def test_render_receipt_copy_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "RENDER_RECEIPT_COPY", False)
    src = tmp_path / "msg.txt"
    src.write_text("anything")
    assert pr.render_receipt_copy(src, "anything", None) is None


def test_text_source_extraction_sets_render_path(tmp_path):
    # End-to-end: an emailed HTML receipt distils AND produces a filable JPEG copy.
    p = tmp_path / "chevron.html"
    p.write_text("<body><h2>Chevron</h2><p>Total: $52.10</p><p>06/22/2026</p></body>")
    data = pr._extract_receipt_with_status(None, p, None, [])
    assert data is not None and data.get("_text_source") is True
    rp = data.get("_render_path")
    assert rp and Path(rp).exists() and _is_jpeg(Path(rp))
