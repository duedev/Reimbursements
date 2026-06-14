"""Tests for local-OCR (RapidOCR) fallback selection logic (engine fully mocked)."""
from unittest.mock import MagicMock

import process_receipts as pr


GOOD = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}


def _setup(monkeypatch, tmp_path, *, lm_ocr, local_ocr, distill, vision):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "ocr-model")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_raw_ocr", MagicMock(return_value=lm_ocr))
    monkeypatch.setattr(pr, "_extract_local_ocr", MagicMock(return_value=local_ocr))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(side_effect=distill))
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock(side_effect=vision))
    return img


def test_local_ocr_text_reaches_distillation_when_lm_ocr_fails(tmp_path, monkeypatch):
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr=None, local_ocr="SHELL\nTOTAL $45.20",
                 distill=lambda client, text: dict(GOOD),
                 vision=AssertionError("direct vision should not run"))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["_ocr_engine"] == "rapidocr"
    assert data["_raw_ocr"] == "SHELL\nTOTAL $45.20"
    pr._unified_distillation.assert_called_once()
    assert pr._unified_distillation.call_args[0][1] == "SHELL\nTOTAL $45.20"
    pr._extract_with_model.assert_not_called()


def test_local_ocr_failure_falls_back_to_direct_vision(tmp_path, monkeypatch):
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr=None, local_ocr=None,
                 distill=AssertionError("distill should not run without OCR text"),
                 vision=lambda client, path, model: dict(GOOD))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["vendor"] == "Shell"
    assert "_ocr_engine" not in data
    pr._extract_with_model.assert_called_once()


def test_lm_ocr_success_skips_local_ocr(tmp_path, monkeypatch):
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr="LM TEXT", local_ocr="LOCAL TEXT",
                 distill=lambda client, text: dict(GOOD),
                 vision=AssertionError("direct vision should not run"))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    pr._extract_local_ocr.assert_not_called()
    assert pr._unified_distillation.call_args[0][1] == "LM TEXT"


def test_local_ocr_last_resort_when_direct_vision_fails(tmp_path, monkeypatch):
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr=None, local_ocr="LOCAL TEXT",
                 distill=lambda client, text: dict(GOOD),
                 vision=lambda client, path, model: None)
    # No dedicated OCR model configured → straight to vision, then local-OCR rescue
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["_ocr_engine"] == "rapidocr"
    pr._extract_with_model.assert_called_once()
