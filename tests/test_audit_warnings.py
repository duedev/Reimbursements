"""Optional, user-configured spending/date warnings (default OFF)."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import process_receipts as pr
import server


# ── audit_warning_flags (pure) ─────────────────────────────────────────────────

@pytest.fixture()
def limits(monkeypatch):
    # Start from a clean, all-off baseline and auto-restore afterwards.
    monkeypatch.setattr(pr, "AMOUNT_LIMITS", {"fuel": None, "mats": None, "misc": None})
    monkeypatch.setattr(pr, "MAX_RECEIPT_AGE_DAYS", None)
    return pr


def test_no_warnings_by_default(limits):
    data = {"amount": 9999, "date": (date.today() - timedelta(days=9999)).isoformat()}
    assert pr.audit_warning_flags(data, "fuel") == []


def test_amount_limit_flags_over_only(limits):
    limits.AMOUNT_LIMITS["fuel"] = 200
    assert pr.audit_warning_flags({"amount": 250}, "fuel")           # over → flagged
    assert pr.audit_warning_flags({"amount": 150}, "fuel") == []     # under → clean
    # A limit on another category doesn't affect this one.
    assert pr.audit_warning_flags({"amount": 250}, "misc") == []


def test_amount_limit_message(limits):
    limits.AMOUNT_LIMITS["mats"] = 500
    msg = pr.audit_warning_flags({"amount": 612.5}, "mats")[0]
    assert "612.50" in msg and "500" in msg and "mats" in msg


def test_age_limit_flags_old_only(limits):
    limits.MAX_RECEIPT_AGE_DAYS = 90
    old = (date.today() - timedelta(days=200)).isoformat()
    new = (date.today() - timedelta(days=10)).isoformat()
    assert pr.audit_warning_flags({"date": old}, "misc")
    assert pr.audit_warning_flags({"date": new}, "misc") == []


def test_age_limit_handles_bad_date(limits):
    limits.MAX_RECEIPT_AGE_DAYS = 30
    assert pr.audit_warning_flags({"date": "not a date"}, "misc") == []
    assert pr.audit_warning_flags({"date": ""}, "misc") == []


def test_age_limit_normalizes_us_date(limits):
    limits.MAX_RECEIPT_AGE_DAYS = 30
    # A US-format old date still triggers (normalize_date is applied first).
    old_us = (date.today() - timedelta(days=200)).strftime("%m/%d/%y")
    assert pr.audit_warning_flags({"date": old_us}, "misc")


# ── /settings/audit round-trip ─────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    monkeypatch.setattr(pr, "AMOUNT_LIMITS", {"fuel": None, "mats": None, "misc": None})
    monkeypatch.setattr(pr, "MAX_RECEIPT_AGE_DAYS", None)
    with TestClient(server.app) as c:
        yield c


def test_audit_settings_round_trip(client):
    r = client.post("/settings/audit", json={"fuel_limit": 200, "max_age_days": 60})
    assert r.status_code == 200 and r.json()["ok"]
    assert pr.AMOUNT_LIMITS["fuel"] == 200
    assert pr.MAX_RECEIPT_AGE_DAYS == 60
    g = client.get("/settings/audit").json()
    assert g["amount_limits"]["fuel"] == 200 and g["max_age_days"] == 60


def test_audit_blank_clears_warning(client):
    client.post("/settings/audit", json={"fuel_limit": 200})
    assert pr.AMOUNT_LIMITS["fuel"] == 200
    client.post("/settings/audit", json={"fuel_limit": None})
    assert pr.AMOUNT_LIMITS["fuel"] is None


def test_audit_rejects_nonpositive(client):
    client.post("/settings/audit", json={"misc_limit": 0, "max_age_days": -5})
    assert pr.AMOUNT_LIMITS["misc"] is None      # 0 → off
    assert pr.MAX_RECEIPT_AGE_DAYS is None        # negative → off
