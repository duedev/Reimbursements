"""Tests for the processing/email settings and queue-nudge endpoints."""
import json

import pytest
from fastapi.testclient import TestClient

import server
import watch_mode
import process_receipts as pr


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    # watch_mode reads the same app-config file in production; point it at tmp here
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", tmp_path / ".app_config.json")
    # Keep background loops inert so the queue tests are deterministic
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    # Save runtime globals so endpoint mutations don't leak into other tests
    saved = (pr.AUTOROTATE_ENABLED, pr.AUTOCROP_ENABLED, pr.COMPRESS_ENABLED,
             pr.LOCAL_OCR_ENABLED, pr.JPEG_QUALITY, pr._thinking_enabled,
             pr.MAX_PARALLEL_REQUESTS, pr.AUTOCROP_AGGRESSIVENESS,
             pr.LLM_RATE_LIMIT_PER_MIN, pr.LLM_RATE_LIMIT_ENABLED)
    saved_429 = (pr.LLM_429_WAIT_ENABLED, pr.LLM_429_MAX_WAIT)
    server._work_queue.clear()
    server._kanban.clear()
    server._item_cache.clear()
    with TestClient(server.app) as c:
        yield c
    server._work_queue.clear()
    server._kanban.clear()
    server._item_cache.clear()
    (pr.AUTOROTATE_ENABLED, pr.AUTOCROP_ENABLED, pr.COMPRESS_ENABLED,
     pr.LOCAL_OCR_ENABLED, pr.JPEG_QUALITY, pr._thinking_enabled,
     pr.MAX_PARALLEL_REQUESTS, pr.AUTOCROP_AGGRESSIVENESS,
     pr.LLM_RATE_LIMIT_PER_MIN, pr.LLM_RATE_LIMIT_ENABLED) = saved
    pr.set_rate_limit(per_min=pr.LLM_RATE_LIMIT_PER_MIN,
                      enabled=pr.LLM_RATE_LIMIT_ENABLED)
    (pr.LLM_429_WAIT_ENABLED, pr.LLM_429_MAX_WAIT) = saved_429


def test_processing_round_trip(client):
    r = client.post("/settings/processing",
                    json={"autorotate": False, "autocrop": False,
                          "local_ocr": False, "jpeg_quality": 55})
    assert r.status_code == 200 and r.json()["ok"]
    assert pr.AUTOROTATE_ENABLED is False
    assert pr.AUTOCROP_ENABLED is False
    assert pr.LOCAL_OCR_ENABLED is False
    assert pr.JPEG_QUALITY == 55
    g = client.get("/settings/processing").json()
    assert g["jpeg_quality"] == 55 and g["autorotate"] is False


def test_processing_clamps_quality(client):
    assert client.post("/settings/processing", json={"jpeg_quality": 5}).json()["jpeg_quality"] == 40
    assert client.post("/settings/processing", json={"jpeg_quality": 200}).json()["jpeg_quality"] == 95


def test_max_parallel_round_trip_and_clamp(client):
    r = client.post("/settings/processing", json={"max_parallel": 5})
    assert r.status_code == 200 and r.json()["max_parallel"] == 5
    assert pr.MAX_PARALLEL_REQUESTS == 5
    assert client.get("/settings/processing").json()["max_parallel"] == 5
    # clamps to 1..8
    assert client.post("/settings/processing", json={"max_parallel": 0}).json()["max_parallel"] == 1
    assert client.post("/settings/processing", json={"max_parallel": 99}).json()["max_parallel"] == 8


def test_rate_limit_round_trip_and_clamp(client):
    r = client.post("/settings/processing",
                    json={"rate_limit_per_min": 12, "rate_limit_enabled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["rate_limit_per_min"] == 12 and body["rate_limit_enabled"] is True
    assert pr.LLM_RATE_LIMIT_PER_MIN == 12 and pr._RATE_LIMITER.max_requests == 12
    assert client.get("/settings/processing").json()["rate_limit_per_min"] == 12
    # clamps to 1..1000
    assert client.post("/settings/processing", json={"rate_limit_per_min": 0}).json()["rate_limit_per_min"] == 1
    assert client.post("/settings/processing", json={"rate_limit_per_min": 99999}).json()["rate_limit_per_min"] == 1000
    # toggle off
    off = client.post("/settings/processing", json={"rate_limit_enabled": False}).json()
    assert off["rate_limit_enabled"] is False
    assert pr.LLM_RATE_LIMIT_ENABLED is False and pr._RATE_LIMITER.enabled is False


def test_email_password_hidden_and_preserved(client):
    import app_secrets
    client.post("/settings/email", json={
        "smtp_host": "smtp.x", "smtp_user": "u", "smtp_pass": "secret", "email_to": "t@x.com"})
    g = client.get("/settings/email").json()
    assert "smtp_pass" not in g                 # secret never leaves the server
    assert g["password_set"] and g["configured"]

    # The secret is stored OUT of the (often cloud-synced) config file
    assert "smtp_pass" not in json.loads(server.CONFIG_FILE.read_text()).get("email", {})
    assert app_secrets.load_secrets()["smtp_pass"] == "secret"

    # Blank password keeps the saved one while other fields update
    client.post("/settings/email", json={
        "smtp_host": "smtp.y", "smtp_user": "u", "smtp_pass": "", "email_to": "t@x.com"})
    saved = json.loads(server.CONFIG_FILE.read_text())["email"]
    assert "smtp_pass" not in saved
    assert saved["smtp_host"] == "smtp.y"
    assert app_secrets.load_secrets()["smtp_pass"] == "secret"   # still preserved


def test_version_endpoint(client):
    assert client.get("/version").json()["version"] == pr.APP_VERSION
    assert client.get("/settings").json()["version"] == pr.APP_VERSION


def test_reasoning_toggle_round_trip(client):
    assert client.post("/models/thinking", json={"enabled": True}).json()["thinking"] is True
    assert pr._thinking_enabled is True
    assert client.get("/models/available").json()["thinking"] is True
    # persisted, and re-applied from config
    assert json.loads(server.CONFIG_FILE.read_text())["thinking_enabled"] is True
    pr._thinking_enabled = False
    server._apply_processing_config()
    assert pr._thinking_enabled is True


def test_thinking_body_reflects_flag(monkeypatch):
    monkeypatch.setattr(pr, "_thinking_enabled", False)
    assert pr._thinking_body(8192) == {"thinking": {"type": "disabled"}}
    monkeypatch.setattr(pr, "_thinking_enabled", True)
    assert pr._thinking_body(8192) == {"thinking": {"type": "enabled", "budget_tokens": 8192}}


def test_nudge_requeues_queued_item(client):
    server._kanban["IMG.jpg"] = {"status": "queued", "data": {}, "model": ""}
    server._item_cache["IMG.jpg"] = {
        "filename": "IMG.jpg", "path": "/tmp/IMG.jpg",
        "employee": "E", "job_name": "", "job_number": "",
    }
    r = client.post("/queue/nudge")
    assert r.status_code == 200 and r.json()["ok"]
    assert "IMG.jpg" in r.json()["requeued"]
    assert any(it["filename"] == "IMG.jpg" for it in server._work_queue)


def test_nudge_skips_items_already_in_queue(client):
    server._kanban["A.jpg"] = {"status": "queued", "data": {}, "model": ""}
    server._item_cache["A.jpg"] = {
        "filename": "A.jpg", "path": "/tmp/A.jpg",
        "employee": "E", "job_name": "", "job_number": "",
    }
    server._work_queue.append({"filename": "A.jpg", "path": "/tmp/A.jpg"})
    r = client.post("/queue/nudge")
    assert r.json()["count"] == 0               # already queued — not duplicated
    assert sum(1 for it in server._work_queue if it["filename"] == "A.jpg") == 1


def test_processing_advanced_tunables_roundtrip(client):
    """The previously env-only tunables are now user-settable, applied, clamped."""
    saved = (pr.LLM_TIMEOUT, pr.LLM_MAX_RETRIES, pr.STORE_MAX_PX, pr.PDF_MAX_PAGES,
             server.MAX_UPLOAD_BYTES)
    try:
        r = client.post("/settings/processing", json={
            "llm_timeout": 45, "llm_max_retries": 1, "store_max_px": 1600,
            "pdf_max_pages": 20, "max_upload_mb": 50,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["llm_timeout"] == 45
        assert d["llm_max_retries"] == 1
        assert d["store_max_px"] == 1600
        assert d["pdf_max_pages"] == 20
        assert d["max_upload_mb"] == 50
        # applied to the live module globals
        assert pr.LLM_TIMEOUT == 45
        assert pr.PDF_MAX_PAGES == 20
        assert server.MAX_UPLOAD_BYTES == 50 * 1024 * 1024
        # round-trips via GET
        assert client.get("/settings/processing").json()["store_max_px"] == 1600
        # out-of-range values are clamped, not rejected
        d2 = client.post("/settings/processing",
                         json={"llm_timeout": 5, "store_max_px": 99999}).json()
        assert d2["llm_timeout"] == 10        # floored
        assert d2["store_max_px"] == 4000     # ceiled
    finally:
        (pr.LLM_TIMEOUT, pr.LLM_MAX_RETRIES, pr.STORE_MAX_PX, pr.PDF_MAX_PAGES,
         server.MAX_UPLOAD_BYTES) = saved
