"""Tests for LLM-endpoint auto-detection / auto-recovery.

The recurring "app won't connect to LM Studio" failure was a stale saved server
choice (e.g. a "docker" server-type pinned to :11434 while LM Studio actually
runs on :1234) being re-applied on every startup, with no way to self-recover.
The fix probes the well-known endpoints and adopts the first that answers, both
automatically at startup and via POST /llm-server/autodetect.

Probing is mocked (no real sockets) so the tests are deterministic — matching
how the rest of the suite mocks the network.
"""
import pytest
from fastapi.testclient import TestClient

import server
import process_receipts as pr


def _fake_probe(reachable: dict):
    """Build a _probe_llm_url stand-in from a {url: (ok, model_count)} map."""
    def _p(url, timeout=1.5):
        return reachable.get(url, (False, 0))
    return _p


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "_startup_models", lambda: None)
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(pr, "initialize_models", lambda *a, **k: None)
    monkeypatch.setattr(pr, "warm_up_model", lambda *a, **k: True)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    saved = pr.LMSTUDIO_BASE_URL
    with TestClient(server.app) as c:
        yield c
    pr.LMSTUDIO_BASE_URL = saved


# ── candidate list ─────────────────────────────────────────────────────────────

def test_candidate_urls_put_current_first_and_dedup(monkeypatch):
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    cands = server._candidate_llm_urls()
    assert cands[0] == "http://127.0.0.1:1234/v1"
    assert len(cands) == len(set(cands))               # no dupes
    assert "http://host.docker.internal:1234/v1" in cands
    assert any(":11434" in u for u in cands)           # bundled-server port covered


# ── _autodetect_llm_url preference order ────────────────────────────────────────

def test_autodetect_prefers_endpoint_with_model(monkeypatch):
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:9999/v1")  # dead
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({
        "http://127.0.0.1:1234/v1": (True, 0),    # reachable, no model
        "http://localhost:1234/v1": (True, 2),    # reachable WITH models
    }))
    assert server._autodetect_llm_url() == "http://localhost:1234/v1"


def test_autodetect_falls_back_to_merely_reachable(monkeypatch):
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({
        "http://127.0.0.1:1234/v1": (True, 0),
    }))
    assert server._autodetect_llm_url() == "http://127.0.0.1:1234/v1"


def test_autodetect_returns_none_when_all_dead(monkeypatch):
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({}))
    assert server._autodetect_llm_url() is None


# ── _ensure_llm_reachable (startup safety net) ──────────────────────────────────

def test_ensure_recovers_from_stranded_docker_url(monkeypatch):
    """The core regression: a dead :11434 (docker) URL auto-switches to the live
    LM Studio on :1234 without the user touching anything."""
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({
        "http://127.0.0.1:1234/v1": (True, 1),
    }))
    server._ensure_llm_reachable()
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:1234/v1"


def test_ensure_leaves_a_working_url_untouched(monkeypatch):
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({
        "http://127.0.0.1:1234/v1": (True, 1),
    }))
    server._ensure_llm_reachable()
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:1234/v1"


def test_ensure_does_not_persist(monkeypatch, tmp_path):
    """Startup recovery is session-only — it must not rewrite the saved config."""
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({
        "http://127.0.0.1:1234/v1": (True, 1),
    }))
    server._ensure_llm_reachable()
    assert not (tmp_path / ".app_config.json").exists()


# ── POST /llm-server/autodetect ─────────────────────────────────────────────────

def test_autodetect_endpoint_recovers_and_persists(client, monkeypatch):
    monkeypatch.setattr(pr, "LMSTUDIO_BASE_URL", "http://127.0.0.1:11434/v1")
    server._save_config({"llm_server": {"server_type": "docker", "base_url": ""}})
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({
        "http://127.0.0.1:1234/v1": (True, 1),
    }))
    r = client.post("/llm-server/autodetect")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["base_url"] == "http://127.0.0.1:1234/v1"
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:1234/v1"
    # The bad "docker" choice is overwritten with the working custom URL.
    saved = server._load_config()["llm_server"]
    assert saved == {"server_type": "custom",
                     "base_url": "http://127.0.0.1:1234/v1"}


def test_autodetect_endpoint_reports_failure(client, monkeypatch):
    monkeypatch.setattr(server, "_probe_llm_url", _fake_probe({}))
    r = client.post("/llm-server/autodetect")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["tried"]                       # lists what it tried


# ── regression: /llm-server/load no longer 500s ─────────────────────────────────

def test_load_endpoint_does_not_500(client):
    """run_in_executor returns a Future, not a coroutine — wrapping it in
    asyncio.create_task used to raise TypeError and 500 the endpoint."""
    r = client.post("/llm-server/load")
    assert r.status_code == 200
    assert r.json()["ok"] is True
