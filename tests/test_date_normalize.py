"""Robust, US-first date normalization (normalize_date).

The app standardizes on the US MM/DD/YYYY convention so the LLM no longer has to
guess day/month order. These lock that behaviour in.
"""
import pytest

from process_receipts import normalize_date, _normalize_year


@pytest.mark.parametrize("raw,expected", [
    # The headline case from the task — all three separators, 2-digit year.
    ("08-15-24", "2024-08-15"),
    ("08/15/24", "2024-08-15"),
    ("08.15.24", "2024-08-15"),
    # 4-digit year, US month/day.
    ("08/15/2024", "2024-08-15"),
    ("8/5/2024", "2024-08-05"),
    # Single-digit month/day.
    ("1/5/24", "2024-01-05"),
    # Already ISO (year-first) is trusted as written.
    ("2024-08-15", "2024-08-15"),
    ("2024/08/15", "2024-08-15"),
    ("2024.08.15", "2024-08-15"),
    # Month-name forms.
    ("May 1, 2024", "2024-05-01"),
    ("1 May 2024", "2024-05-01"),
    ("Aug 15 24", "2024-08-15"),
    # Embedded in surrounding OCR text.
    ("Date: 08/15/24", "2024-08-15"),
    ("TRANSACTION 12/31/2023 14:05", "2023-12-31"),
])
def test_normalize_date_us_first(raw, expected):
    assert normalize_date(raw) == expected


def test_us_order_not_day_first():
    # 08/15 can only be month=08/day=15 (15 is not a valid month), and we must
    # never silently swap — confirm month-first is honoured.
    assert normalize_date("08/15/24") == "2024-08-15"
    # A date valid under BOTH orders must resolve as US month/day.
    assert normalize_date("03/04/24") == "2024-03-04"   # March 4th, not April 3rd


def test_two_digit_year_defaults_to_2000s():
    assert normalize_date("12/31/99") == "2099-12-31"   # 20xx, not 1999
    assert normalize_date("01/01/00") == "2000-01-01"
    assert _normalize_year(24) == 2024
    assert _normalize_year(99) == 2099
    assert _normalize_year(2024) == 2024                # 4-digit passes through


@pytest.mark.parametrize("raw", ["", None, "not a date", "garbled",
                                 "13/40/24", "99/99/99"])
def test_normalize_date_unparseable_returns_blank(raw):
    assert normalize_date(raw) == ""


def test_normalize_date_in_llm_record():
    # The shared parser canonicalises the model's date field in place.
    from process_receipts import _parse_llm_record
    rec = _parse_llm_record('{"vendor": "Shell", "amount": 40, "date": "08/15/24"}')
    assert rec["date"] == "2024-08-15"


def test_unparseable_model_date_is_kept_not_dropped():
    from process_receipts import _parse_llm_record
    rec = _parse_llm_record('{"vendor": "Shell", "date": "sometime last week"}')
    assert rec["date"] == "sometime last week"          # preserved, not blanked
