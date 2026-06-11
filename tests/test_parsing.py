"""Tests for JSON stripping, filename sanitization, date parsing, and model matching."""
from datetime import date

from process_receipts import (
    _format_date_mmddyy,
    _fuzzy_match,
    _strip_json,
    compute_expense_period,
    sanitize_filename_part,
    sort_key_for_receipt,
)


# ── _strip_json ────────────────────────────────────────────────────────────────

def test_strip_json_plain():
    assert _strip_json('{"a": 1}') == '{"a": 1}'


def test_strip_json_markdown_fence():
    raw = '```json\n{"a": 1}\n```'
    assert _strip_json(raw) == '{"a": 1}'


def test_strip_json_surrounding_prose():
    raw = 'Here is the result:\n{"vendor": "Shell"}\nHope that helps!'
    assert _strip_json(raw) == '{"vendor": "Shell"}'


def test_strip_json_nested_braces():
    raw = 'x {"flags": [{"flag": "ok"}]} y'
    assert _strip_json(raw) == '{"flags": [{"flag": "ok"}]}'


# ── sanitize_filename_part ─────────────────────────────────────────────────────

def test_sanitize_basic():
    assert sanitize_filename_part("Home Depot") == "home_depot"


def test_sanitize_strips_punctuation():
    assert sanitize_filename_part("Love's #42!") == "loves_42"


def test_sanitize_collapses_separators():
    assert sanitize_filename_part("a -- b   c") == "a_b_c"


def test_sanitize_truncates_to_40():
    assert len(sanitize_filename_part("x" * 100)) == 40


def test_sanitize_empty():
    assert sanitize_filename_part("") == ""


# ── _format_date_mmddyy ────────────────────────────────────────────────────────

def test_format_date_standard():
    assert _format_date_mmddyy("2024-12-30") == "12-30-24"


def test_format_date_unpadded():
    assert _format_date_mmddyy("2024-1-5") == "01-05-24"


def test_format_date_invalid_calendar_date_falls_back():
    assert _format_date_mmddyy("2024-13-45") == "2024_13_45"


def test_format_date_empty():
    assert _format_date_mmddyy("") == "unknown"


def test_format_date_garbage():
    assert _format_date_mmddyy("???") == "unknown"


# ── sort_key_for_receipt / compute_expense_period ──────────────────────────────

def test_sort_key_iso_date():
    assert sort_key_for_receipt({"date": "2026-05-01"}) == date(2026, 5, 1)


def test_sort_key_unpadded():
    assert sort_key_for_receipt({"date": "2026-5-1"}) == date(2026, 5, 1)


def test_sort_key_missing_date_sorts_last():
    assert sort_key_for_receipt({}) == date.max


def test_sort_key_month_name():
    key = sort_key_for_receipt({"date": "march"})
    assert key.month == 3 and key.day == 1


def test_expense_period_spans_min_to_max():
    results = [{"date": "2026-05-10"}, {"date": "2026-04-01"}, {"date": "2026-05-02"}]
    assert compute_expense_period(results) == "04/01/26 - 05/10/26"


def test_expense_period_empty_without_dates():
    assert compute_expense_period([{"date": ""}, {}]) == ""


# ── _fuzzy_match ───────────────────────────────────────────────────────────────

def test_fuzzy_match_ignores_separators_and_case():
    assert _fuzzy_match("google/gemma-4-12b-qat", ["Google_Gemma-4-12B-QAT-GGUF"])


def test_fuzzy_match_no_match():
    assert not _fuzzy_match("allenai/olmOCR-2-7B", ["google/gemma-4-12b-qat"])
