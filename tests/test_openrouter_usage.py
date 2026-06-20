"""Live OpenRouter daily free-request counter + queried daily cap.

The app keeps a local per-UTC-day tally of every request it sends while pointed at
OpenRouter (failures included — they count toward the free-tier quota) and queries
OpenRouter's /credits endpoint to know whether the daily cap is 50 (under $10 of
lifetime credit) or 1000 (at/over).
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import process_receipts as pr
import server
import watch_mode


# ── process_receipts: the local daily counter ───────────────────────────────────

def test_counts_only_on_openrouter_endpoint(monkeypatch):
    pr.reset_openrouter_usage()
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    pr._note_openrouter_request()
    assert pr.get_openrouter_usage()["count"] == 0      # local server → not counted
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", pr.OPENROUTER_BASE_URL)
    pr._note_openrouter_request()
    pr._note_openrouter_request()
    assert pr.get_openrouter_usage()["count"] == 2


def test_count_resets_on_new_utc_day(monkeypatch):
    pr.reset_openrouter_usage()
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", pr.OPENROUTER_BASE_URL)
    monkeypatch.setattr(pr, "_utc_day", lambda: "2026-06-20")
    pr._note_openrouter_request()
    assert pr.get_openrouter_usage() == {"date": "2026-06-20", "count": 1}
    # Day rolls over → the tally resets to 0 for the new day.
    monkeypatch.setattr(pr, "_utc_day", lambda: "2026-06-21")
    assert pr.get_openrouter_usage() == {"date": "2026-06-21", "count": 0}
    pr._note_openrouter_request()
    assert pr.get_openrouter_usage()["count"] == 1


def test_set_usage_drops_stale_day(monkeypatch):
    pr.reset_openrouter_usage()
    monkeypatch.setattr(pr, "_utc_day", lambda: "2026-06-20")
    pr.set_openrouter_usage("2026-06-20", 7)
    assert pr.get_openrouter_usage()["count"] == 7
    pr.set_openrouter_usage("2026-06-19", 99)   # yesterday → quota has reset
    assert pr.get_openrouter_usage()["count"] == 0


def test_llm_call_increments_counter_on_openrouter(monkeypatch):
    pr.reset_openrouter_usage()
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", pr.OPENROUTER_BASE_URL)
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=MagicMock())
    pr._llm_call(client, model="m", messages=[])
    assert pr.get_openrouter_usage()["count"] == 1


def test_llm_call_counts_failed_attempts(monkeypatch):
    """OpenRouter counts failed requests toward the daily quota, so we do too."""
    pr.reset_openrouter_usage()
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", pr.OPENROUTER_BASE_URL)
    client = MagicMock()
    client.chat.completions.create = MagicMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        pr._llm_call(client, model="m", messages=[])
    assert pr.get_openrouter_usage()["count"] == 1


# ── server: cap inference from /credits ──────────────────────────────────────────

def test_cap_info_high_when_credits_at_threshold(monkeypatch):
    server._or_cap_cache["data"] = None
    monkeypatch.setattr(server, "_fetch_openrouter_credits",
                        lambda *a, **k: {"total_credits": 10.0, "total_usage": 2.0})
    info = server._openrouter_cap_info(force=True)
    assert info["cap"] == 1000 and info["credits_known"] is True


def test_cap_info_low_under_threshold(monkeypatch):
    server._or_cap_cache["data"] = None
    monkeypatch.setattr(server, "_fetch_openrouter_credits",
                        lambda *a, **k: {"total_credits": 3.0, "total_usage": 0.0})
    info = server._openrouter_cap_info(force=True)
    assert info["cap"] == 50


def test_cap_info_defaults_low_when_unknown(monkeypatch):
    server._or_cap_cache["data"] = None
    monkeypatch.setattr(server, "_fetch_openrouter_credits", lambda *a, **k: None)
    info = server._openrouter_cap_info(force=True)
    assert info["cap"] == 50 and info["credits_known"] is False


# ── server: the usage endpoint ───────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    server._or_cap_cache["data"] = None
    with TestClient(server.app) as c:
        yield c


def test_usage_endpoint_no_key(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "")
    d = client.get("/settings/openrouter/usage").json()
    assert d["has_key"] is False and d["cap"] is None


def test_usage_endpoint_with_key_and_cap(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "sk-or-test")
    monkeypatch.setattr(server, "_fetch_openrouter_credits",
                        lambda *a, **k: {"total_credits": 12.0, "total_usage": 1.0})
    pr.reset_openrouter_usage()
    monkeypatch.setattr(pr, "_utc_day", lambda: "2026-06-20")
    pr.set_openrouter_usage("2026-06-20", 5)
    d = client.get("/settings/openrouter/usage?force=1").json()
    assert d["has_key"] is True
    assert d["count"] == 5
    assert d["cap"] == 1000
    assert d["remaining"] == 995
    assert d["per_min"] == 20
