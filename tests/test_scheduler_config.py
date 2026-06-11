"""Tests for scheduled-export configuration parsing and next-run math."""
from datetime import datetime

import pytest

from scheduler import ScheduleError, next_run, parse_schedule


def _cfg(**kw):
    base = {"enabled": True, "time": "17:00", "days": "thu",
            "dropbox_token": "", "email": False}
    base.update(kw)
    return parse_schedule(base)


def test_parse_valid():
    cfg = _cfg(time="08:30", days="mon,fri")
    assert (cfg.hour, cfg.minute) == (8, 30)
    assert cfg.days == {"mon", "fri"}
    assert cfg.enabled is True


def test_parse_day_aliases():
    assert _cfg(days="daily").days == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    assert _cfg(days="weekdays").days == {"mon", "tue", "wed", "thu", "fri"}


def test_parse_full_day_names_trimmed():
    assert _cfg(days="Thursday, Monday").days == {"thu", "mon"}


def test_parse_invalid_time():
    with pytest.raises(ScheduleError):
        _cfg(time="25:99")
    with pytest.raises(ScheduleError):
        _cfg(time="noonish")


def test_parse_invalid_days():
    with pytest.raises(ScheduleError):
        _cfg(days="caturday")
    with pytest.raises(ScheduleError):
        _cfg(days="")


def test_next_run_same_week():
    # Wed 2026-06-10 → next Thursday 17:00 is the 11th
    now = datetime(2026, 6, 10, 12, 0)
    assert next_run(_cfg(), now) == datetime(2026, 6, 11, 17, 0)


def test_next_run_same_day_before_time():
    now = datetime(2026, 6, 11, 9, 0)  # Thursday morning
    assert next_run(_cfg(), now) == datetime(2026, 6, 11, 17, 0)


def test_next_run_same_day_after_time_rolls_a_week():
    now = datetime(2026, 6, 11, 18, 0)  # Thursday evening, past 17:00
    assert next_run(_cfg(), now) == datetime(2026, 6, 18, 17, 0)


def test_next_run_disabled():
    now = datetime(2026, 6, 10, 12, 0)
    assert next_run(_cfg(enabled=False), now) is None


def test_run_export_no_results(tmp_path):
    from scheduler import run_export
    report = run_export(_cfg(), [], "Jane", export_dir=tmp_path)
    assert report["ok"] is False


def test_run_export_writes_workbook(tmp_path):
    from scheduler import run_export
    results = [{"vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
                "category": "fuel", "_category": "fuel", "_file": "a.jpg"}]
    report = run_export(_cfg(email=False), results, "Jane Doe", export_dir=tmp_path)
    assert report["ok"] is True
    assert report["delivered"] == ["folder"]
    assert (tmp_path / report["filename"]).exists()
