"""Tests for the offline rule-based OCR fallback used when LM Studio is down."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import process_receipts as pr


# ── Date finder ──────────────────────────────────────────────────────────────

def test_find_date_iso():
    assert pr._find_date_in_text("Txn 2026-05-01 ok") == "2026-05-01"


def test_find_date_us_slash_and_two_digit_year():
    assert pr._find_date_in_text("05/01/2026") == "2026-05-01"
    assert pr._find_date_in_text("5-1-26") == "2026-05-01"


def test_find_date_month_name_both_orders():
    assert pr._find_date_in_text("May 1, 2026") == "2026-05-01"
    assert pr._find_date_in_text("1 May 2026") == "2026-05-01"


def test_find_date_none():
    assert pr._find_date_in_text("no date present here") == ""


# ── Local rule-based distillation ─────────────────────────────────────────────

def test_local_parse_extracts_core_fields():
    txt = "SHELL\n123 Main St\nUNLEADED\nTOTAL $45.20\n05/01/2026"
    out = pr._local_distill_from_ocr(txt)
    assert out["vendor"] == "SHELL"
    assert out["amount"] == 45.20
    assert out["category"] == "fuel"
    assert out["date"] == "2026-05-01"
    assert out["_local_parse"] is True
    assert out["flags"] and "LM Studio unavailable" in out["flags"][0]["flag"]


def test_local_parse_requires_amount_and_vendor():
    assert pr._local_distill_from_ocr("") is None
    assert pr._local_distill_from_ocr("SHELL\nno numbers here") is None       # no amount
    assert pr._local_distill_from_ocr("\n\n$%^&\n") is None                   # no usable vendor


def test_local_parse_category_mats():
    out = pr._local_distill_from_ocr("HOME DEPOT\nLUMBER\nTOTAL $88.00")
    assert out["category"] == "mats"


def test_local_parse_picks_grand_total_not_tendered_cash():
    txt = ("DINER\nSUBTOTAL $43.75\nTAX $1.45\nTOTAL $45.20\n"
           "AMOUNT TENDERED $60.00\nCHANGE $14.80")
    out = pr._local_distill_from_ocr(txt)
    assert out["amount"] == 45.20
    assert out["category"] == "misc"


# ── End-to-end fallback when LM Studio is unreachable ─────────────────────────

def test_paddle_fallback_when_lm_fully_down(monkeypatch, tmp_path):
    """No OCR model, vision unreachable, distillation unreachable: PaddleOCR text
    must still yield a result via the local parser instead of dropping to failed."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill")
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock(return_value=None))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=None))
    monkeypatch.setattr(pr, "_extract_paddle_ocr",
                        MagicMock(return_value="SHELL\nUNLEADED\nTOTAL $45.20\n05/01/2026"))

    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["_ocr_engine"] == "paddleocr"
    assert data["_local_parse"] is True
    assert data["amount"] == 45.20


def test_lm_distillation_preferred_over_local_parse(monkeypatch, tmp_path):
    """When LM distillation succeeds, the local parser must not override it."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    good = {"vendor": "Chevron", "amount": 30.0, "date": "2026-05-02", "flags": []}
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill")
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock(return_value=None))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=dict(good)))
    monkeypatch.setattr(pr, "_extract_paddle_ocr", MagicMock(return_value="CHEVRON\nTOTAL $30.00"))

    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["vendor"] == "Chevron"
    assert "_local_parse" not in data


def test_distilled_amount_reconciled_against_ocr_text(monkeypatch, tmp_path):
    """A model amount that appears nowhere in the OCR text is replaced by the
    receipt's printed grand total and flagged for review."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    hallucinated = {"vendor": "Chevron", "amount": 39.0, "date": "2026-05-02", "flags": []}
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill")
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock(return_value=None))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=dict(hallucinated)))
    monkeypatch.setattr(pr, "_extract_paddle_ocr",
                        MagicMock(return_value="CHEVRON\nSUBTOTAL $28.50\nTAX $1.50\nTOTAL $30.00"))

    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["amount"] == 30.00
    assert any("corrected" in (f.get("flag") or "").lower() for f in data["flags"])


# ── Config consolidation ──────────────────────────────────────────────────────

@pytest.mark.no_path_isolation   # asserts the real import-time default, not a tmp redirect
def test_single_config_source_of_truth():
    """server and watch_mode must reference the one CONFIG_FILE from process_receipts."""
    import server
    import watch_mode
    assert server.CONFIG_FILE == pr.CONFIG_FILE
    assert watch_mode.CONFIG_FILE == pr.CONFIG_FILE
    assert pr.CONFIG_FILE == Path(pr.OUTPUT_FOLDER) / pr.APP_CONFIG_FILENAME
