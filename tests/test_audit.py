"""Tests for the rules-based OCR amount cross-check."""
from process_receipts import (
    audit_amount,
    extract_best_total,
    extract_candidate_totals,
    reconcile_amount,
)

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


# ── Grand-total selection ──────────────────────────────────────────────────────

def test_best_total_prefers_total_over_subtotal_and_tender():
    txt = ("SUBTOTAL $43.75\nTAX $1.45\nTOTAL $45.20\n"
           "AMOUNT TENDERED $60.00\nCHANGE $14.80")
    assert extract_best_total(txt) == 45.20


def test_best_total_prefers_grand_total_line():
    txt = "SUBTOTAL 10.00\nTOTAL TAX 0.80\nGRAND TOTAL 12.50"
    assert extract_best_total(txt) == 12.50


def test_best_total_ignores_zero_balance_due():
    txt = "TOTAL 154.37\nBALANCE DUE 0.00"
    assert extract_best_total(txt) == 154.37


def test_best_total_none_without_total_lines():
    assert extract_best_total("coffee 4.50\nmuffin 3.25") is None
    assert extract_best_total("") is None


# ── Amount reconciliation (hallucination / subtotal correction) ───────────────

def test_reconcile_keeps_amount_printed_on_receipt():
    data = {"amount": 45.20}
    assert reconcile_amount(data, RECEIPT_TEXT) is None
    assert data["amount"] == 45.20


def test_reconcile_replaces_hallucinated_amount():
    data = {"amount": 54.20}   # transposed digits — appears nowhere in the text
    note = reconcile_amount(data, RECEIPT_TEXT)
    assert note is not None and "$45.20" in note
    assert data["amount"] == 45.20


def test_reconcile_corrects_subtotal_copy():
    data = {"amount": 43.75}   # model copied the pre-tax SUBTOTAL
    note = reconcile_amount(data, RECEIPT_TEXT)
    assert note is not None and "subtotal" in note.lower()
    assert data["amount"] == 45.20


def test_reconcile_fills_missing_amount_from_total():
    data = {"amount": 0}
    note = reconcile_amount(data, RECEIPT_TEXT)
    assert note is not None
    assert data["amount"] == 45.20


def test_reconcile_leaves_amount_when_no_total_line():
    data = {"amount": 9.99}
    assert reconcile_amount(data, "coffee 4.50\nmuffin 3.25") is None
    assert data["amount"] == 9.99
