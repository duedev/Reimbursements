"""Tests for the rules-based OCR amount cross-check."""
from process_receipts import audit_amount, extract_candidate_totals

RECEIPT_TEXT = """\
SHELL OIL 57444
123 MAIN ST

UNLEADED   12.503 GAL
PRICE/GAL  $3.499

SUBTOTAL   $43.75
TAX         $1.45
TOTAL      $45.20

THANK YOU
"""


def test_extract_totals_prefers_keyword_lines():
    vals = extract_candidate_totals(RECEIPT_TEXT)
    assert 45.20 in vals
    assert 43.75 in vals          # SUBTOTAL contains "total"
    assert 3.499 not in vals      # price/gal line has no total keyword


def test_extract_totals_falls_back_to_all_money():
    vals = extract_candidate_totals("coffee 4.50\nmuffin 3.25")
    assert vals == [3.25, 4.50]


def test_extract_totals_handles_thousands_separators():
    vals = extract_candidate_totals("TOTAL $1,234.56")
    assert vals == [1234.56]


def test_extract_totals_empty_text():
    assert extract_candidate_totals("") == []
    assert extract_candidate_totals("no numbers here") == []


def test_audit_passes_matching_amount():
    data = {"amount": 45.20}
    assert audit_amount(data, RECEIPT_TEXT) is None
    assert data["_amount_verified"] is True


def test_audit_flags_mismatched_amount():
    data = {"amount": 54.20}   # transposed digits — classic hallucination
    flag = audit_amount(data, RECEIPT_TEXT)
    assert flag is not None
    assert "$54.20" in flag and "$45.20" in flag
    assert data["_amount_verified"] is False


def test_audit_skips_without_text_or_amount():
    assert audit_amount({"amount": 45.20}, "") is None
    assert audit_amount({"amount": 0}, RECEIPT_TEXT) is None
    assert audit_amount(None, RECEIPT_TEXT) is None


def test_audit_skips_when_no_candidates():
    data = {"amount": 12.00}
    assert audit_amount(data, "ILLEGIBLE RECEIPT") is None
    assert "_amount_verified" not in data


def test_audit_tolerates_float_noise():
    data = {"amount": 45.2000001}
    assert audit_amount(data, RECEIPT_TEXT) is None
    assert data["_amount_verified"] is True
