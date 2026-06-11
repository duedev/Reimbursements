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
    saved = (pr.AUTOCROP_ENABLED, pr.COMPRESS_ENABLED, pr.PADDLEOCR_ENABLED,
             pr.JPEG_QUALITY, pr._thinking_enabled)
    server._work_queue.clear()
    server._kanban.clear()
    server._item_cache.clear()
    with TestClient(server.app) as c:
        yield c
    server._work_queue.clear()
    server._kanban.clear()
    server._item_cache.clear()
    (pr.AUTOCROP_ENABLED, pr.COMPRESS_ENABLED, pr.PADDLEOCR_ENABLED,
     pr.JPEG_QUALITY, pr._thinking_enabled) = saved


def test_processing_round_trip(client):
    r = client.post("/settings/processing",
                    json={"autocrop": False, "paddleocr": False, "jpeg_quality": 55})
    assert r.status_code == 200 and r.json()["ok"]
    assert pr.AUTOCROP_ENABLED is False
    assert pr.PADDLEOCR_ENABLED is False
    assert pr.JPEG_QUALITY == 55
    assert client.get("/settings/processing").json()["jpeg_quality"] == 55


def test_processing_clamps_quality(client):
    assert client.post("/settings/processing", json={"jpeg_quality": 5}).json()["jpeg_quality"] == 40
    assert client.post("/settings/processing", json={"jpeg_quality": 200}).json()["jpeg_quality"] == 95


def test_email_password_hidden_and_preserved(client):
    client.post("/settings/email", json={
        "smtp_host": "smtp.x", "smtp_user": "u", "smtp_pass": "secret", "email_to": "t@x.com"})
    g = client.get("/settings/email").json()
    assert "smtp_pass" not in g                 # secret never leaves the server
    assert g["password_set"] and g["configured"]

    # Blank password keeps the saved one while other fields update
    client.post("/settings/email", json={
        "smtp_host": "smtp.y", "smtp_user": "u", "smtp_pass": "", "email_to": "t@x.com"})
    saved = json.loads(server.CONFIG_FILE.read_text())["email"]
    assert saved["smtp_pass"] == "secret"
    assert saved["smtp_host"] == "smtp.y"


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
