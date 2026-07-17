"""PDF text-layer fast path — digitally generated PDF pages (e.g. the bundled
Chevron/Texaco Rewards export: one text receipt per page) carry an exact text
layer, so pdf_to_images writes a .pdftext sidecar next to each page JPEG and
the pipeline distills that text directly instead of OCR-ing the render."""
from unittest.mock import MagicMock

import fitz
import pytest
from fastapi.testclient import TestClient

import process_receipts as pr
import server


GOOD = {"vendor": "Chevron", "amount": 45.20, "date": "2026-05-01", "flags": []}

RECEIPT_TEXT = (
    "CHEVRON #12345\n123 MAIN ST\nANYTOWN CA\n"
    "05/01/2026 08:15\nUNLEADED  12.5G @ 3.599\nTOTAL $45.20\nTHANK YOU"
)


def _text_pdf(path, pages=2):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{RECEIPT_TEXT}\nPAGE {i + 1}", fontsize=9)
    doc.save(str(path))
    doc.close()


def _image_pdf(path):
    """A 'scanned' PDF: one raster image, no text layer."""
    from PIL import Image
    import io
    doc = fitz.open()
    page = doc.new_page()
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (200, 200, 200)).save(buf, format="JPEG")
    page.insert_image(fitz.Rect(36, 36, 400, 700), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


# ── pdf_to_images: sidecar creation ──────────────────────────────────────────────

def test_text_pdf_pages_get_sidecars(tmp_path):
    pdf = tmp_path / "chevron-receipts_2025-05-09_to_2026-06-24.pdf"
    _text_pdf(pdf, pages=2)
    pages = pr.pdf_to_images(pdf, tmp_path / "out")
    assert len(pages) == 2
    for p in pages:
        side = pr.pdf_text_sidecar(p)
        assert p.exists() and side.exists()
        assert "TOTAL $45.20" in side.read_text()


def test_scanned_pdf_gets_no_sidecar(tmp_path):
    pdf = tmp_path / "scan.pdf"
    _image_pdf(pdf)
    pages = pr.pdf_to_images(pdf, tmp_path / "out")
    assert len(pages) == 1
    assert not pr.pdf_text_sidecar(pages[0]).exists()


def test_short_text_gets_no_sidecar(tmp_path):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "hi", fontsize=9)   # < PDF_TEXT_MIN_CHARS
    pdf = tmp_path / "short.pdf"
    doc.save(str(pdf)); doc.close()
    pages = pr.pdf_to_images(pdf, tmp_path / "out")
    assert pages and not pr.pdf_text_sidecar(pages[0]).exists()


# ── Pipeline: sidecar consumption ────────────────────────────────────────────────

def _img_with_sidecar(tmp_path):
    img = tmp_path / "r_p1.jpg"
    img.write_bytes(b"fake")
    pr.pdf_text_sidecar(img).write_text(RECEIPT_TEXT, encoding="utf-8")
    return img


def test_sidecar_skips_ocr_and_distills_text(tmp_path, monkeypatch):
    img = _img_with_sidecar(tmp_path)
    ocr = MagicMock(side_effect=AssertionError("OCR must not run on a text-layer page"))
    monkeypatch.setattr(pr, "_ocr_lines_best_orientation", ocr)
    monkeypatch.setattr(pr, "_extract_local_ocr", ocr)
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    distill = MagicMock(return_value=dict(GOOD))
    monkeypatch.setattr(pr, "_unified_distillation", distill)

    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)

    assert data is not None and data["vendor"] == "Chevron"
    assert data["_ocr_engine"] == "pdf-text"
    by_step = {s["step"]: s for s in steps}
    assert by_step["pdf_text"]["ok"] is True
    assert "local_ocr" not in by_step             # OCR never ran
    # The distiller received the exact PDF text.
    assert "TOTAL $45.20" in distill.call_args[0][1]
    # One-shot: the sidecar is consumed (won't re-trigger or be orphaned).
    assert not pr.pdf_text_sidecar(img).exists()


def test_sidecar_with_llm_down_uses_offline_parser_not_ocr(tmp_path, monkeypatch):
    """With the LLM unavailable the text layer still avoids OCR: _distill_text
    falls back to the offline regex parser ON THE EXACT PDF TEXT."""
    img = _img_with_sidecar(tmp_path)
    ocr = MagicMock(side_effect=AssertionError("OCR must not run on a text-layer page"))
    monkeypatch.setattr(pr, "_ocr_lines_best_orientation", ocr)
    monkeypatch.setattr(pr, "_extract_local_ocr", ocr)
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=None))

    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    assert data["amount"] == 45.20                # parsed from the exact PDF text
    by_step = {s["step"]: s for s in steps}
    assert by_step["pdf_text"]["ok"] is True
    assert by_step["local_parse"]["ok"] is True   # offline fallback, no OCR
    assert "local_ocr" not in by_step
    assert not pr.pdf_text_sidecar(img).exists()


def test_no_sidecar_is_untouched_path(tmp_path, monkeypatch):
    """A plain image without a sidecar behaves exactly as before."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_local_ocr",
                        MagicMock(return_value="SHELL\nTOTAL $45.20"))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(return_value=dict(GOOD)))
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    assert "pdf_text" not in {s["step"] for s in steps}


# ── Dashboard stats: live allowances ─────────────────────────────────────────────

def test_stats_include_live_allowances(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    server._results.clear()
    server._results.append({"vendor": "Shell", "date": "2026-05-01", "amount": 10.0,
                            "category": "fuel", "_category": "fuel", "_file": "a.jpg"})
    try:
        c = TestClient(server.app)
        c.post("/settings/per-diem", json={"enabled": True, "rate": 50, "days": 3})
        c.post("/settings/phone-service", json={"enabled": True, "months": ["2026-07"]})
        s = c.get("/stats").json()
        assert s["per_diem_total"] == 150.0 and s["per_diem_days"] == 3
        assert s["phone_total"] == 63.0 and s["phone_months"] == 1
        assert s["total_reimbursement"] == round(s["total"] + 213.0, 2)
        # Off → zeros (tiles hide).
        c.post("/settings/per-diem", json={"enabled": False, "rate": 50, "days": 3})
        c.post("/settings/phone-service", json={"enabled": False, "months": []})
        s = c.get("/stats").json()
        assert s["per_diem_total"] == 0.0 and s["phone_total"] == 0.0
        assert s["total_reimbursement"] == s["total"]
    finally:
        server._results.clear()
