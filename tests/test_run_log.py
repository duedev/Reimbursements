"""Full per-run logging: 'what gets sent' transparency, the reviewable run log,
image-processing steps, and the routing of detail into the live log stream."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import process_receipts as pr
import server


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    server._runs.clear()
    server._current_run = None
    server._worker_cancel.clear()
    with TestClient(server.app) as c:
        yield c
    server._runs.clear()
    server._current_run = None


@pytest.fixture(autouse=True)
def _reset_run_state():
    server._runs.clear()
    server._current_run = None
    yield
    server._runs.clear()
    server._current_run = None


# ── "What gets sent" instructions payload ─────────────────────────────────────

def test_instructions_payload_exposes_every_prompt():
    p = server._llm_instructions_payload()
    assert p["provider"] in ("local", "openrouter")
    assert "stages" in p and len(p["stages"]) == 3
    blob = "\n".join(s["user"] for s in p["stages"])
    # The actual instruction text the model receives must be present, in full.
    assert pr.OLMOCR_RAW_PROMPT in blob
    assert "receipt data extractor" in blob          # distillation + vision templates
    assert "{ocr_text}" not in blob                  # placeholder is humanised, not raw
    # The distillation system prompt is surfaced.
    assert any("valid JSON only" in (s.get("system") or "") for s in p["stages"])


def test_instructions_endpoint(client):
    d = client.get("/settings/llm-instructions").json()
    assert d["stages"][0]["user"] == pr.OLMOCR_RAW_PROMPT
    assert "send_image" in d and "thinking_enabled" in d


def test_instructions_reflect_privacy_gate(monkeypatch):
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", False)
    p = server._llm_instructions_payload()
    assert p["send_image"] is False
    vision = next(s for s in p["stages"] if s["stage"].startswith("3"))
    assert "Off" in vision["runs_when"]              # vision rescue disabled when image off


# ── Live-log capture into the active run ──────────────────────────────────────

def test_emit_log_captures_into_active_run():
    run = server._begin_run([{"filename": "a.jpg"}])
    server._emit_log("hello world")
    assert any(l["message"] == "hello world" for l in run["lines"])


def test_emit_log_noop_when_idle():
    server._current_run = None
    server._emit_log("nobody listening")             # must not raise
    assert server._current_run is None


def test_append_run_line_caps_buffer(monkeypatch):
    monkeypatch.setattr(server, "RUN_MAX_LINES", 10)
    run = server._begin_run([])
    for i in range(50):
        server._append_run_line(f"line {i}")
    assert len(run["lines"]) == 10
    assert run["lines"][-1]["message"] == "line 49"  # newest kept


# ── Per-receipt recording + finalize ──────────────────────────────────────────

def test_record_run_receipt_captures_steps_and_streams():
    run = server._begin_run([{"filename": "r.jpg"}])
    steps = [
        {"step": "grayscale", "label": "Grayscale", "detail": "B&W", "ok": True, "duration_s": 0.0},
        {"step": "distillation", "label": "Distillation", "detail": "m", "ok": True, "duration_s": 1.2},
    ]
    data = {"vendor": "Shell", "amount": 45.2, "_category": "fuel",
            "_new_filename": "Fuel_Shell.jpg", "_steps": steps, "_confidence": 90}
    server._record_run_receipt("r.jpg", "done", data, steps)
    assert len(run["receipts"]) == 1
    rec = run["receipts"][0]
    assert rec["status"] == "done" and rec["vendor"] == "Shell"
    assert len(rec["steps"]) == 2
    # The full per-step breakdown was streamed into the live log too.
    joined = "\n".join(l["message"] for l in run["lines"])
    assert "Grayscale" in joined and "Distillation" in joined
    assert "Fuel_Shell.jpg" in joined


def test_finalize_run_pushes_and_clears():
    run = server._begin_run([{"filename": "r.jpg"}])
    server._finalize_run(run, 3.4)
    assert server._current_run is None
    assert server._runs[0] is run and run["total_seconds"] == 3.4
    assert run["ts_end"] is not None


def test_finalize_run_caps_history(monkeypatch):
    monkeypatch.setattr(server, "RUNS_MAX_ENTRIES", 3)
    for _ in range(8):
        server._finalize_run(server._begin_run([]), 1.0)
    assert len(server._runs) == 3


def test_abort_current_run_salvages_partial():
    run = server._begin_run([{"filename": "r.jpg"}])
    server._emit_log("got this far")
    server._abort_current_run()
    assert server._current_run is None
    assert server._runs and server._runs[0] is run


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _seed_run():
    run = server._begin_run([{"filename": "r.jpg"}])
    server._record_run_receipt(
        "r.jpg", "done",
        {"vendor": "Shell", "amount": 45.2, "_category": "fuel",
         "_new_filename": "Fuel_Shell.jpg", "_confidence": 90,
         "_steps": [{"step": "grayscale", "label": "Grayscale", "ok": True}]},
        [{"step": "grayscale", "label": "Grayscale", "ok": True}],
    )
    server._finalize_run(run, 2.0)
    return run


def test_runs_list_and_detail_endpoints(client):
    run = _seed_run()
    listing = client.get("/runs").json()["runs"]
    assert listing and listing[0]["id"] == run["id"]
    assert listing[0]["done"] == 1 and listing[0]["count"] == 1
    detail = client.get(f"/runs/{run['id']}").json()
    assert detail["receipts"][0]["vendor"] == "Shell"
    assert detail["instructions"]["stages"]


def test_run_download_is_readable_text(client):
    run = _seed_run()
    r = client.get(f"/runs/{run['id']}/download")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "WHAT WAS SENT TO THE MODEL" in body
    assert "Shell" in body and "Grayscale" in body


def test_run_detail_404(client):
    assert client.get("/runs/nope").status_code == 404


def test_runs_clear(client):
    _seed_run()
    assert client.get("/runs").json()["runs"]
    assert client.post("/runs/clear").json()["ok"] is True
    assert client.get("/runs").json()["runs"] == []


# ── Persistence ───────────────────────────────────────────────────────────────

def test_runs_persist_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    run = _seed_run()
    server._persist_state()
    server._runs.clear()
    server._restore_state()
    assert len(server._runs) == 1 and server._runs[0]["id"] == run["id"]


# ── Image-processing steps land in the per-receipt step log ───────────────────

def test_image_processing_steps_logged(tmp_path, monkeypatch):
    """Grayscale (and friends) are recorded as steps so the card + run log show
    exactly what was done to the picture before OCR."""
    from PIL import Image
    img = tmp_path / "receipt.jpg"
    Image.new("RGB", (200, 320), (210, 215, 220)).save(img, format="JPEG")

    monkeypatch.setattr(pr, "GRAYSCALE_ENABLED", True)
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_local_ocr", MagicMock(return_value="SHELL\nTOTAL $45.20"))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(return_value={"vendor": "Shell", "amount": 45.2,
                                                "date": "2026-05-01", "flags": []}))
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock(return_value=None))

    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    by_step = {s["step"]: s for s in steps}
    assert "grayscale" in by_step and by_step["grayscale"]["ok"] is True


# ── End-to-end: a real drain captures a complete run ──────────────────────────

def test_drain_once_captures_full_run(tmp_path, monkeypatch):
    from PIL import Image
    images = tmp_path / "receipts"
    images.mkdir()
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    server._worker_cancel.clear()
    server._work_queue.clear(); server._results.clear(); server._kanban.clear()

    src_dir = images / "_upload"; src_dir.mkdir()
    src = src_dir / "IMG_1.png"
    Image.new("RGB", (400, 600), (90, 90, 90)).save(src, format="PNG")

    def fake_extract(client, path, cb, step_log=None, force_llm_ocr=False):
        if step_log is not None:
            step_log.append({"step": "grayscale", "label": "Grayscale", "ok": True, "duration_s": 0.0})
        return {"vendor": "Shell", "amount": 45.2, "date": "2026-05-01", "flags": []}

    monkeypatch.setattr(server, "_extract_receipt_with_status", fake_extract)
    server._work_queue.append({"filename": "IMG_1.png", "path": str(src),
                               "employee": "E", "job_name": "", "job_number": ""})

    assert server._drain_once() is True
    assert server._current_run is None               # finalized
    assert len(server._runs) == 1
    run = server._runs[0]
    assert run["count"] == 1 and run["total_seconds"] is not None
    assert run["receipts"] and run["receipts"][0]["vendor"] == "Shell"
    assert run["instructions"]["stages"]             # 'what was sent' embedded
    joined = "\n".join(l["message"] for l in run["lines"])
    assert "Processing 1 receipt" in joined and "Grayscale" in joined

    server._work_queue.clear(); server._results.clear(); server._kanban.clear()
