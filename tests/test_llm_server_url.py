"""Regression tests for LLM-server URL resolution.

Covers the bug where selecting the "Docker bundled server" option persisted
``server_type: docker`` and then forced ``LMSTUDIO_BASE_URL`` to the
docker-compose-internal hostname ``http://model-server:11434/v1`` on every
startup — which is unresolvable when the app runs on the host (not in Docker),
permanently stranding a perfectly good LM Studio connection on localhost.
"""
import pytest
from fastapi.testclient import TestClient

import server
import process_receipts as pr


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    saved_url = pr.LMSTUDIO_BASE_URL
    saved_model = pr._active_distill_model
    with TestClient(server.app) as c:
        yield c
    pr.LMSTUDIO_BASE_URL = saved_url
    pr._active_distill_model = saved_model


# ── _normalize_llm_url ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("http://127.0.0.1:1234",      "http://127.0.0.1:1234/v1"),
    ("http://127.0.0.1:1234/",     "http://127.0.0.1:1234/v1"),
    ("http://127.0.0.1:1234/v1",   "http://127.0.0.1:1234/v1"),
    ("http://127.0.0.1:1234/v1/",  "http://127.0.0.1:1234/v1"),
])
def test_normalize_llm_url(raw, expected):
    assert server._normalize_llm_url(raw) == expected


# ── _docker_llm_url is runtime-aware ──────────────────────────────────────────

def test_docker_url_uses_localhost_on_host(monkeypatch):
    """Outside Docker the bundled server is on the published host port, NOT the
    unresolvable compose service name."""
    monkeypatch.setattr(server, "_in_docker", lambda: False)
    assert server._docker_llm_url() == "http://127.0.0.1:11434/v1"
    assert "model-server" not in server._docker_llm_url()


def test_docker_url_uses_service_name_in_docker(monkeypatch):
    monkeypatch.setattr(server, "_in_docker", lambda: True)
    assert server._docker_llm_url() == "http://model-server:11434/v1"


# ── persisted docker config no longer strands a host-run app ───────────────────

def test_persisted_docker_config_resolves_to_host_port(monkeypatch):
    """The core regression: a persisted ``server_type: docker`` must resolve to a
    reachable URL when the app runs on the host."""
    monkeypatch.setattr(server, "_in_docker", lambda: False)
    cfg = {"llm_server": {"server_type": "docker", "base_url": ""}}
    server._apply_llm_server_config(cfg)
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:11434/v1"
    assert "model-server" not in pr.LMSTUDIO_BASE_URL


def test_post_llm_server_docker_returns_resolved_url(client, monkeypatch):
    monkeypatch.setattr(server, "_in_docker", lambda: False)
    r = client.post("/settings/llm-server",
                    json={"server_type": "docker", "base_url": ""})
    assert r.status_code == 200
    assert r.json()["base_url"] == "http://127.0.0.1:11434/v1"
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:11434/v1"


def test_custom_url_normalized_and_applied(client):
    r = client.post("/settings/llm-server",
                    json={"server_type": "custom", "base_url": "http://127.0.0.1:1234"})
    assert r.status_code == 200
    assert r.json()["base_url"] == "http://127.0.0.1:1234/v1"
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:1234/v1"


# ── Configure Model dialog must not silently hijack the active URL ─────────────

def test_llm_model_config_does_not_change_url_immediately(client):
    """POST /settings/llm-model (Configure Model dialog) sets the model for the
    session but defers URL/server-type to next startup, so it cannot silently
    overwrite a working LMSTUDIO_BASE_URL."""
    client.post("/settings/llm-server",
                json={"server_type": "custom", "base_url": "http://127.0.0.1:1234"})
    before = pr.LMSTUDIO_BASE_URL
    r = client.post("/settings/llm-model",
                    json={"model_id": "my-model", "server_type": "docker", "base_url": ""})
    assert r.status_code == 200
    # URL unchanged in-session despite docker server_type
    assert pr.LMSTUDIO_BASE_URL == before
    # but the model id was applied for the current session
    assert pr._active_distill_model == "my-model"
