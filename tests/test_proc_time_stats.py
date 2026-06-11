"""Tests for per-receipt processing-time stats."""
from unittest.mock import MagicMock

import server
import process_receipts


def _r(**kw):
    base = {"vendor": "Shell", "amount": 10.0, "category": "fuel"}
    base.update(kw)
    return base


def test_compute_stats_proc_times():
    stats = server._compute_stats([
        _r(_proc_seconds=10.0),
        _r(_proc_seconds=20.0),
        _r(),  # no timing recorded
    ])
    assert stats["proc_total_seconds"] == 30.0
    assert stats["proc_avg_seconds"] == 15.0


def test_compute_stats_proc_times_empty():
    stats = server._compute_stats([])
    assert stats["proc_total_seconds"] == 0.0
    assert stats["proc_avg_seconds"] == 0.0


def test_compute_stats_ignores_bad_proc_values():
    stats = server._compute_stats([_r(_proc_seconds="oops"), _r(_proc_seconds=None)])
    assert stats["proc_avg_seconds"] == 0.0


def test_extract_records_proc_seconds(tmp_path, monkeypatch):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")

    monkeypatch.setattr(process_receipts, "_active_ocr_model", "")
    monkeypatch.setattr(
        process_receipts, "_extract_with_model",
        lambda client, path, model: {"vendor": "Shell", "amount": 5.0},
    )

    data = process_receipts._extract_receipt_with_status(MagicMock(), img, None)
    assert data is not None
    assert data["_proc_seconds"] >= 0
    assert "_distill_seconds" in data or data["_proc_seconds"] == 0
