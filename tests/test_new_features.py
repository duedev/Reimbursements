"""Tests for the newer features:

- per-stage reasoning (OCR never reasons, distillation follows the toggle)
- dual OCR (built-in + LLM) cross-referenced by the distillation model
- job name/number placeholder defaults
- manual clear of report history
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import server
import process_receipts as pr


GOOD = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}


# ── Per-stage reasoning ────────────────────────────────────────────────────────

def test_ocr_pass_never_reasons(monkeypatch):
    monkeypatch.setattr(pr, "_thinking_enabled", True)
    # OCR transcription forces reasoning off even when the distill toggle is on
    assert pr._thinking_body(4096, enabled=False) == {"thinking": {"type": "disabled"}}
    # distillation follows the toggle
    assert pr._thinking_body(8192) == {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    monkeypatch.setattr(pr, "_thinking_enabled", False)
    assert pr._thinking_body(8192) == {"thinking": {"type": "disabled"}}


# ── OCR source combination ─────────────────────────────────────────────────────

def test_combine_ocr_sources_both():
    out = pr._combine_ocr_sources("LOCAL\nTOTAL $5", "VISION\nTOTAL $5")
    assert "LOCAL" in out and "VISION" in out
    assert "transcription A" in out and "transcription B" in out


def test_combine_ocr_sources_single_passthrough():
    assert pr._combine_ocr_sources("only local", None) == "only local"
    assert pr._combine_ocr_sources("", "only llm") == "only llm"
    assert pr._combine_ocr_sources(None, None) is None
    assert pr._combine_ocr_sources("   ", "  ") is None


# ── Dual OCR + cross-reference pipeline ────────────────────────────────────────

def test_dual_ocr_cross_references_with_distill(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_active_ocr_model", "ocr-model")    # enables LLM OCR pass
    monkeypatch.setattr(pr, "_extract_local_ocr",
                        MagicMock(return_value="SHELL local\nTOTAL $45.20"))
    monkeypatch.setattr(pr, "_extract_raw_ocr",
                        MagicMock(return_value="SHELL vision\nTOTAL $45.20"))
    distill = MagicMock(return_value=dict(GOOD))
    monkeypatch.setattr(pr, "_unified_distillation", distill)
    vision = MagicMock(return_value=dict(GOOD))
    monkeypatch.setattr(pr, "_extract_with_model", vision)

    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    assert data["_ocr_engine"] == "rapidocr+llm"
    # both transcriptions were handed to the distill model in a single call
    combined = distill.call_args[0][1]
    assert "SHELL local" in combined and "SHELL vision" in combined
    vision.assert_not_called()                                  # no vision rescue needed
    assert any(s["step"] == "cross_reference" for s in steps)
    assert any(s["step"] == "llm_ocr" and s["ok"] for s in steps)


def test_single_builtin_ocr_skips_llm_ocr(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_active_ocr_model", "")            # no LLM OCR configured
    monkeypatch.setattr(pr, "_extract_local_ocr",
                        MagicMock(return_value="SHELL\nTOTAL $45.20"))
    raw = MagicMock()
    monkeypatch.setattr(pr, "_extract_raw_ocr", raw)
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=dict(GOOD)))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert data["_ocr_engine"] == "rapidocr"
    raw.assert_not_called()


# ── Job-field placeholders ─────────────────────────────────────────────────────

def test_job_default_constants():
    assert pr.DEFAULT_JOB_NAME == "Default Job Name"
    assert pr.DEFAULT_JOB_NUMBER == "Default Job Number"


# ── Endpoints: clear report history + manual job defaults ──────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json", raising=False)
    server._results.clear()
    server._kanban.clear()
    server._last_context.update({"employee": "Employee", "job_name": "", "job_number": ""})
    with TestClient(server.app) as c:
        yield c
    server._results.clear()
    server._kanban.clear()


def test_clear_reports_deletes_only_reports(client, tmp_path):
    (tmp_path / "Reimbursements_Jane_2026-05-01.xlsx").write_bytes(b"x")
    (tmp_path / "Reimbursements_Jane_2026-05-02.csv").write_text("a,b")
    keep = tmp_path / "notes.txt"
    keep.write_text("keep me")
    assert client.get("/reports").json()["reports"]            # workbook is listed
    body = client.post("/reports/clear").json()
    assert body["ok"] and body["removed"] == 2
    assert keep.exists()                                        # unrelated file untouched
    assert client.get("/reports").json()["reports"] == []


def test_clear_reports_when_empty(client):
    assert client.post("/reports/clear").json() == {"ok": True, "removed": 0, "errors": []}


def test_manual_blank_job_uses_placeholder(client):
    r = client.post("/results/add-manual", json={
        "filename": "IMG.jpg", "vendor": "Depot", "date": "2026-05-01",
        "amount": "10.00", "category": "misc", "job_name": "", "job_number": "",
        "summary": "stuff", "review_required": False, "approved": False,
    })
    assert r.status_code == 200
    rec = server._results[-1]
    assert rec["job_name"] == "Default Job Name"
    assert rec["job_number"] == "Default Job Number"
