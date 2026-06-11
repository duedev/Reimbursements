"""Tests for duplicate receipt detection."""
from process_receipts import _detect_duplicates


def _r(vendor, date, amount, flag=None):
    out = {"vendor": vendor, "date": date, "amount": amount}
    if flag:
        out["_flag"] = flag
    return out


def test_exact_duplicates_flagged_both_ways():
    results = [_r("Shell", "2026-05-01", 45.20), _r("Shell", "2026-05-01", 45.20)]
    _detect_duplicates(results)
    assert results[0]["_flag"] == "Potential duplicate entry"
    assert "Duplicate of receipt #1" in results[1]["_flag"]


def test_vendor_match_is_case_insensitive():
    results = [_r("SHELL ", "2026-05-01", 45.20), _r("shell", "2026-05-01", 45.20)]
    _detect_duplicates(results)
    assert results[1].get("_flag")


def test_different_amounts_not_duplicates():
    results = [_r("Shell", "2026-05-01", 45.20), _r("Shell", "2026-05-01", 45.21)]
    _detect_duplicates(results)
    assert not results[0].get("_flag")
    assert not results[1].get("_flag")


def test_zero_amount_receipts_ignored():
    results = [_r("Unknown", "", 0), _r("Unknown", "", 0)]
    _detect_duplicates(results)
    assert not results[0].get("_flag")
    assert not results[1].get("_flag")


def test_existing_flags_not_overwritten():
    results = [
        _r("Shell", "2026-05-01", 45.20, flag="Amount exceeds $200 fuel threshold"),
        _r("Shell", "2026-05-01", 45.20),
    ]
    _detect_duplicates(results)
    assert results[0]["_flag"] == "Amount exceeds $200 fuel threshold"
    assert "Duplicate" in results[1]["_flag"]


def test_three_way_duplicate_references_first_occurrence():
    results = [
        _r("Chevron", "2026-04-15", 60.00),
        _r("Chevron", "2026-04-15", 60.00),
        _r("Chevron", "2026-04-15", 60.00),
    ]
    _detect_duplicates(results)
    assert "Duplicate of receipt #1" in results[1]["_flag"]
    assert "Duplicate of receipt #1" in results[2]["_flag"]


def test_string_amount_coerced():
    results = [_r("Shell", "2026-05-01", "45.20"), _r("Shell", "2026-05-01", 45.2)]
    _detect_duplicates(results)
    assert results[1].get("_flag")
