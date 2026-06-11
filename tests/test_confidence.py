"""Tests for extraction confidence scoring and failure detection."""
from process_receipts import (
    _compute_confidence,
    _get_fail_reason,
    _has_ocr_flag,
    _is_low_confidence,
)


def test_none_is_low_confidence():
    assert _is_low_confidence(None)


def test_missing_amount_is_low_confidence():
    assert _is_low_confidence({"vendor": "Shell", "amount": 0})


def test_missing_vendor_is_low_confidence():
    assert _is_low_confidence({"vendor": "  ", "amount": 12.50})


def test_complete_data_is_confident():
    assert not _is_low_confidence({"vendor": "Shell", "amount": 12.50})


def test_full_data_scores_100():
    data = {"vendor": "Shell", "amount": 45.0, "date": "2026-05-01",
            "category": "fuel", "flags": []}
    score, missing = _compute_confidence(data)
    assert score == 100
    assert missing == ""


def test_no_data_scores_0():
    score, missing = _compute_confidence(None)
    assert score == 0
    assert missing == "no data extracted"


def test_missing_fields_deduct_points():
    data = {"vendor": "", "amount": 0, "date": "", "category": "", "flags": []}
    score, missing = _compute_confidence(data)
    assert score == 100 - 35 - 35 - 15 - 5
    assert "vendor" in missing and "amount" in missing
    assert "date" in missing and "category" in missing


def test_each_flag_deducts_five():
    data = {"vendor": "Shell", "amount": 45.0, "date": "2026-05-01",
            "category": "fuel", "flags": [{"flag": "a"}, {"flag": "b"}]}
    score, _ = _compute_confidence(data)
    assert score == 90


def test_score_never_negative():
    data = {"flags": [{"flag": str(i)} for i in range(30)]}
    score, _ = _compute_confidence(data)
    assert score == 0


def test_ocr_flag_detection():
    assert _has_ocr_flag({"flags": [{"flag": "OCR error: garbled date"}]})
    assert not _has_ocr_flag({"flags": [{"flag": "Amount exceeds threshold"}]})
    assert not _has_ocr_flag({"flags": []})
    assert not _has_ocr_flag(None)


def test_fail_reason_messages():
    assert _get_fail_reason(None) == "Model returned no data"
    assert "vendor" in _get_fail_reason({"amount": 5.0})
    full = {"vendor": "Shell", "amount": 45.0, "date": "2026-05-01",
            "category": "fuel", "flags": []}
    assert _get_fail_reason(full) == "Low-confidence extraction"
