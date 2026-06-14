"""Tests for the OCR-first extraction pipeline (engine + LLM fully mocked).

Flow under test: RapidOCR (primary) → LLM distillation (offline parser if the
LLM is down) → vision rescue only when OCR produced no usable text.
"""
from unittest.mock import MagicMock

import process_receipts as pr


GOOD = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}


def _setup(monkeypatch, tmp_path, *, local_ocr, distill, vision):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_local_ocr", MagicMock(return_value=local_ocr))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(side_effect=distill) if callable(distill)
                        else MagicMock(return_value=distill))
    monkeypatch.setattr(pr, "_extract_with_model",
                        MagicMock(side_effect=vision) if callable(vision)
                        else MagicMock(return_value=vision))
    return img


def test_local_ocr_text_distilled_by_llm(tmp_path, monkeypatch):
    """RapidOCR text is the primary input; the LLM structures it, no vision call."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr="SHELL\nTOTAL $45.20",
                 distill=lambda client, text: dict(GOOD),
                 vision=AssertionError("vision rescue should not run"))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["_ocr_engine"] == "rapidocr"
    assert data["_raw_ocr"] == "SHELL\nTOTAL $45.20"
    assert pr._unified_distillation.call_args[0][1] == "SHELL\nTOTAL $45.20"
    pr._extract_with_model.assert_not_called()


def test_no_ocr_text_falls_back_to_vision(tmp_path, monkeypatch):
    """When RapidOCR finds nothing, a vision model reads the image directly."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr=None,
                 distill=AssertionError("distill should not run without OCR text"),
                 vision=lambda client, path, model: dict(GOOD))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["vendor"] == "Shell"
    assert "_ocr_engine" not in data           # vision path doesn't tag an OCR engine
    pr._extract_with_model.assert_called_once()
    pr._unified_distillation.assert_not_called()


def test_llm_unavailable_uses_offline_parser(tmp_path, monkeypatch):
    """RapidOCR text + offline parser carries the receipt when the LLM is down."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr="SHELL\nUNLEADED\nTOTAL $45.20\n05/01/2026",
                 distill=None,                          # LLM distillation unreachable
                 vision=AssertionError("vision rescue should not run"))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["_local_parse"] is True
    assert data["_ocr_engine"] == "rapidocr"
    assert data["amount"] == 45.20
    pr._extract_with_model.assert_not_called()


def test_low_confidence_distill_falls_to_vision(tmp_path, monkeypatch):
    """OCR text neither the LLM nor the offline parser can use → vision rescue."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr="garbled text with no total",
                 distill=lambda client, text: {"vendor": "X"},  # sparse → low confidence
                 vision=lambda client, path, model: dict(GOOD))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["vendor"] == "Shell"
    pr._extract_with_model.assert_called_once()
