"""AI Model rework: "None" model (no LLM), processing presets (scan-app import),
and reasoning being off by default."""
import json
from pathlib import Path
from unittest.mock import MagicMock

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
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    saved = (pr.AUTOROTATE_ENABLED, pr.GRAYSCALE_ENABLED)
    with TestClient(server.app) as c:
        yield c
    (pr.AUTOROTATE_ENABLED, pr.GRAYSCALE_ENABLED) = saved


# ── "None" model: no LLM call at all ──────────────────────────────────────────

def test_distillation_skips_when_no_model(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "")
    client = MagicMock()
    assert pr._unified_distillation(client, "STORE\nTOTAL 5.00") is None
    client.chat.completions.create.assert_not_called()


def test_vision_skips_when_no_model(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"x")
    client = MagicMock()
    assert pr._extract_with_model(client, img, "") is None
    client.chat.completions.create.assert_not_called()


def test_set_active_model_none_clears(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "some-model")
    monkeypatch.setattr(pr, "_active_ocr_model", "some-model")
    monkeypatch.setattr(pr, "_llm_ocr_enabled", True)
    assert pr.set_active_model("") == ""
    assert pr._active_distill_model == ""
    assert pr._active_ocr_model == ""


def test_none_model_endpoint_round_trip(client):
    d = client.post("/models/distill", json={"model": ""}).json()
    assert d["ok"] is True
    assert d["active_distill"] == ""
    assert client.get("/models/available").json()["active_distill"] == ""


# ── Processing presets (scan-app / CamScanner import) ─────────────────────────

def test_processing_preset_camscanner(client):
    d = client.post("/settings/processing/preset", json={"preset": "camscanner"}).json()
    assert d["ok"] is True
    assert d["preset"] == "camscanner"
    assert d["autorotate"] is True
    assert d["grayscale"] is True
    assert "autocrop" not in d              # the auto-crop feature was removed
    proc = json.loads(server.CONFIG_FILE.read_text())["processing"]
    assert proc["autorotate"] is True and proc["grayscale"] is True


def test_processing_preset_scanned_alias(client):
    d = client.post("/settings/processing/preset", json={"preset": "scanned"}).json()
    assert d["ok"] is True and d["autorotate"] is True


def test_processing_preset_photo(client):
    d = client.post("/settings/processing/preset", json={"preset": "photo"}).json()
    assert d["ok"] is True and d["autorotate"] is True and d["grayscale"] is True


def test_processing_preset_unknown_400(client):
    r = client.post("/settings/processing/preset", json={"preset": "nope"})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "camscanner" in body["presets"]


# ── Reasoning is off by default (no UI toggle any more) ───────────────────────

def test_reasoning_off_in_fresh_source():
    """The module source default is reasoning OFF (no UI turns it on)."""
    src = Path(pr.__file__).read_text()
    assert "_thinking_enabled: bool = False" in src
