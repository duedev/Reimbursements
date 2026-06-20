"""Force the optional LLM-OCR vision pass on a manual retry.

LLM-OCR (the vision transcription the distiller cross-references against RapidOCR)
is off by default to spare the free-tier quota. A manual *Retry* from the review
screen turns it on for that ONE receipt — even when the batch toggle is off — to
rescue fringe cases RapidOCR mangles (logo-only vendors, glyph confusions). It
borrows the active distill model and must not poison the per-batch throttle breaker.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import process_receipts as pr
import server
import watch_mode


def _setup(monkeypatch, tmp_path, *, ocr_toggle_off=True):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_active_ocr_model", "" if ocr_toggle_off else "distill-model")
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", True)
    monkeypatch.setattr(pr, "_extract_local_ocr",
                        MagicMock(return_value="HOME DEPOT\nTOTAL $248.81"))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(return_value={"vendor": "Home Depot", "amount": 248.81,
                                                "date": "2023-09-05", "flags": []}))
    monkeypatch.setattr(pr, "_extract_with_model",
                        MagicMock(side_effect=AssertionError("vision rescue should not run")))
    monkeypatch.setattr(pr, "_combine_ocr_sources",
                        lambda a, b: "\n".join(x for x in (a, b) if x))
    return img


def test_forced_pass_runs_with_distill_model_when_toggle_off(tmp_path, monkeypatch):
    pr.reset_batch_llm_state()
    img = _setup(monkeypatch, tmp_path)
    used = {}

    def _raw_ocr(client, image_path, model_id):
        used["model"] = model_id
        return "THE HOME DEPOT  How doers get more done.  TOTAL $248.81"

    monkeypatch.setattr(pr, "_extract_raw_ocr", _raw_ocr)

    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps,
                                           force_llm_ocr=True)
    assert data is not None
    # The forced pass borrowed the active distill model (toggle was off → no ocr model).
    assert used["model"] == "distill-model"
    llm_step = next(s for s in steps if s["step"] == "llm_ocr")
    assert llm_step["ok"] is True
    assert "forced by retry" in llm_step["detail"].lower()


def test_no_forced_pass_keeps_llm_ocr_off(tmp_path, monkeypatch):
    """Without force AND with the batch toggle off, the vision pass never runs."""
    pr.reset_batch_llm_state()
    img = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_extract_raw_ocr",
                        MagicMock(side_effect=AssertionError("LLM-OCR should not run")))

    steps: list = []
    pr._extract_receipt_with_status(MagicMock(), img, None, steps, force_llm_ocr=False)
    assert not any(s["step"] == "llm_ocr" for s in steps)


def test_forced_throttle_does_not_trip_breaker(tmp_path, monkeypatch):
    """A forced one-off retry that 429s must not suspend the pass for the batch."""
    pr.reset_batch_llm_state()
    img = _setup(monkeypatch, tmp_path)

    def _raw_ocr(client, image_path, model_id):
        pr._set_llm_error("rate-limited (HTTP 429) — Rate limit exceeded: free-models-per-min.")
        return None

    monkeypatch.setattr(pr, "_extract_raw_ocr", _raw_ocr)

    for _ in range(pr._LLM_OCR_THROTTLE_LIMIT + 2):
        steps: list = []
        pr._extract_receipt_with_status(MagicMock(), img, None, steps, force_llm_ocr=True)
    # Forced throttles never accumulate on the breaker.
    assert pr._llm_ocr_suspended() is False


def test_forced_pass_skips_when_image_not_allowed(tmp_path, monkeypatch):
    """OpenRouter 'send OCR text only' → no image leaves the machine, so even a
    forced retry cannot run the vision OCR pass."""
    pr.reset_batch_llm_state()
    img = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", False)
    monkeypatch.setattr(pr, "_extract_raw_ocr",
                        MagicMock(side_effect=AssertionError("must not send image")))

    steps: list = []
    pr._extract_receipt_with_status(MagicMock(), img, None, steps, force_llm_ocr=True)
    assert not any(s["step"] == "llm_ocr" for s in steps)


# ── server: the retry endpoint sets the flag on the re-queued item ───────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", tmp_path / ".app_config.json")
    monkeypatch.setattr(server, "INTAKE_FOLDER", tmp_path / "intake")
    monkeypatch.setattr(server, "PROCESSING_FOLDER", tmp_path / "processing")
    (tmp_path / "intake").mkdir()
    (tmp_path / "processing").mkdir()
    # Keep background loops inert so the queued item is observable.
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    server._work_queue.clear()
    server._kanban.clear()
    server._item_cache.clear()
    with TestClient(server.app) as c:
        yield c
    server._work_queue.clear()


def test_retry_endpoint_forces_llm_ocr_by_default(client, tmp_path):
    (tmp_path / "intake" / "r.jpg").write_bytes(b"fake")
    r = client.post("/retry-receipt", json={"filename": "r.jpg"})
    assert r.status_code == 200 and r.json()["ok"]
    assert server._work_queue[0]["force_llm_ocr"] is True


def test_retry_endpoint_respects_explicit_false(client, tmp_path):
    (tmp_path / "intake" / "r2.jpg").write_bytes(b"fake")
    r = client.post("/retry-receipt", json={"filename": "r2.jpg", "force_llm_ocr": False})
    assert r.status_code == 200 and r.json()["ok"]
    assert server._work_queue[0]["force_llm_ocr"] is False
