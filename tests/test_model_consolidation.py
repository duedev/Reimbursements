"""Tests for the consolidated single-model setup, auto-load, and warm-up.

OCR and distillation now share ONE model (auto-selected + auto-loaded at
startup). The optional LLM-OCR cross-reference re-uses that same model rather
than a separate one. A tiny dummy receipt warms the model into memory on boot.
"""
import pytest
from fastapi.testclient import TestClient

import server
import watch_mode
import process_receipts as pr


def test_active_model_and_ocr_alias_stay_in_lockstep(monkeypatch):
    monkeypatch.setattr(pr, "_llm_ocr_enabled", False)
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "")

    # OCR off → the OCR alias stays empty (built-in RapidOCR only).
    pr.set_active_model("model-x")
    assert pr._active_distill_model == "model-x"
    assert pr._active_ocr_model == ""

    # Enable LLM OCR → the alias points at the one active model.
    pr.set_llm_ocr(True)
    assert pr._active_ocr_model == "model-x"

    # Switching the model carries OCR along — there is no second model to pick.
    pr.set_active_model("model-y")
    assert pr._active_ocr_model == "model-y"

    # Disable again → alias clears, distill model untouched.
    pr.set_llm_ocr(False)
    assert pr._active_ocr_model == ""
    assert pr._active_distill_model == "model-y"


def test_warm_up_sends_dummy_receipt(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "model-x")
    seen = []
    monkeypatch.setattr(pr, "_make_client", lambda: object())
    monkeypatch.setattr(
        pr, "_unified_distillation",
        lambda client, text, **kw: seen.append(text) or {"vendor": "x"},
    )
    assert pr.warm_up_model() is True
    assert seen and "TOTAL" in seen[0]


def test_warm_up_noop_without_model(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "")
    assert pr.warm_up_model() is False


def test_initialize_models_autoloads_and_warms(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "")
    monkeypatch.setattr(pr, "_llm_ocr_enabled", False)
    monkeypatch.setattr(pr, "list_available_models", lambda: ["gemma-3-4b-it"])
    loaded, warmed = [], []
    monkeypatch.setattr(pr, "_try_load_model", lambda m: loaded.append(m) or True)
    monkeypatch.setattr(pr, "warm_up_model", lambda: warmed.append(True) or True)

    pr.initialize_models()
    assert pr._active_distill_model == "gemma-3-4b-it"
    assert loaded == ["gemma-3-4b-it"]   # auto-loaded into memory
    assert warmed == [True]              # primed


def test_initialize_models_can_skip_warmup(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "")
    monkeypatch.setattr(pr, "list_available_models", lambda: ["m"])
    monkeypatch.setattr(pr, "_try_load_model", lambda m: True)
    warmed = []
    monkeypatch.setattr(pr, "warm_up_model", lambda: warmed.append(True))
    pr.initialize_models(warm=False)
    assert warmed == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "initialize_models", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    monkeypatch.setattr(pr, "_active_distill_model", "")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_llm_ocr_enabled", False)
    with TestClient(server.app) as c:
        yield c


def test_distill_endpoint_sets_single_model_and_persists(client):
    r = client.post("/models/distill", json={"model": "model-z"})
    assert r.status_code == 200
    assert r.json()["active_distill"] == "model-z"
    assert pr._active_distill_model == "model-z"


def test_ocr_endpoint_toggles_and_reuses_active_model(client):
    client.post("/models/distill", json={"model": "model-z"})
    r = client.post("/models/ocr", json={"enabled": True})
    assert r.json()["llm_ocr"] is True
    assert pr._active_ocr_model == "model-z"   # one model for both
    r = client.post("/models/ocr", json={"enabled": False})
    assert r.json()["llm_ocr"] is False
    assert pr._active_ocr_model == ""


def test_model_choice_restored_from_config(client, monkeypatch):
    client.post("/models/distill", json={"model": "saved-model"})
    client.post("/models/ocr", json={"enabled": True})
    # Wipe the live globals, then restore from the persisted config.
    monkeypatch.setattr(pr, "_active_distill_model", "")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_llm_ocr_enabled", False)
    server._apply_model_config()
    assert pr._active_distill_model == "saved-model"
    assert pr._llm_ocr_enabled is True
    assert pr._active_ocr_model == "saved-model"
