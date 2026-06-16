"""Tests for the spend-over-time calendar-span calculation.

Regression: the dashboard caption used to report the *count of distinct dated
days* as the duration ("over 173 days"), ignoring multi-year gaps. The span
must be the true inclusive calendar distance between the first and last receipt
using the full Y/M/D date.
"""
import server


def _r(date_str, amount=10.0):
    return {"vendor": "Shell", "amount": amount, "category": "fuel", "date": date_str}


def test_span_is_calendar_distance_not_day_count():
    # Two receipts, one per year-end — only 2 distinct dated days, but the
    # calendar span is just over a year.
    stats = server._compute_stats([_r("2024-01-01"), _r("2025-01-01")])
    assert stats["timeline_days"] == 2          # distinct dated days
    assert stats["timeline_span_days"] == 367   # 2024 is a leap year, inclusive


def test_span_spans_multiple_years():
    stats = server._compute_stats([_r("2023-06-16"), _r("2026-06-16")])
    # 3 calendar years (2024 leap) inclusive.
    assert stats["timeline_span_days"] == 1097


def test_span_single_day_is_one():
    stats = server._compute_stats([_r("2026-06-16"), _r("2026-06-16", 5.0)])
    assert stats["timeline_span_days"] == 1


def test_span_ignores_undated_receipts():
    stats = server._compute_stats([_r("2026-01-01"), _r("2026-01-31"), _r("")])
    assert stats["timeline_span_days"] == 31


def test_span_empty():
    stats = server._compute_stats([])
    assert stats["timeline_span_days"] == 0
