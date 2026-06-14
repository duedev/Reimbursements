"""Tests for per-item process step logging (OCR-first pipeline)."""
from unittest.mock import MagicMock

import process_receipts as pr


GOOD = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}


def _setup(monkeypatch, tmp_path, *, local_ocr, distill, vision):
    """Mock the OCR-first pipeline pieces: RapidOCR text, LLM distillation, vision."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_local_ocr", MagicMock(return_value=local_ocr))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(side_effect=distill) if callable(distill)
                        else MagicMock(return_value=distill))
    monkeypatch.setattr(pr, "_extract_with_model",
                        MagicMock(side_effect=vision) if callable(vision)
                        else MagicMock(return_value=vision))
    return img


# ── _append_step helper ────────────────────────────────────────────────────────

def test_append_step_noop_on_none():
    pr._append_step(None, "test", "Test", "detail")  # must not raise


def test_append_step_populates_list():
    steps: list = []
    pr._append_step(steps, "autocrop", "Autocrop", "borders trimmed", ok=True, duration_s=0.05)
    assert len(steps) == 1
    s = steps[0]
    assert s["step"] == "autocrop"
    assert s["label"] == "Autocrop"
    assert s["detail"] == "borders trimmed"
    assert s["ok"] is True
    assert s["duration_s"] == 0.05


# ── Step recording inside _extract_receipt_with_status ────────────────────────

def test_steps_recorded_ocr_then_distill(tmp_path, monkeypatch):
    """Happy path: RapidOCR → LLM distillation → step log shows both ok, no vision."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr="SHELL\nTOTAL $45.20",
                 distill=lambda c, t: dict(GOOD),
                 vision=AssertionError("vision rescue should not run"))
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    by_step = {s["step"]: s for s in steps}
    assert by_step["local_ocr"]["ok"] is True
    assert by_step["distillation"]["ok"] is True
    assert "vision" not in by_step
    assert data["_steps"] and len(data["_steps"]) == len(steps)


def test_steps_no_ocr_text_falls_back_to_vision(tmp_path, monkeypatch):
    """RapidOCR finds nothing → vision rescue; step log shows the handoff."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr=None,
                 distill=AssertionError("distill should not run without OCR text"),
                 vision=lambda c, p, m: dict(GOOD))
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    by_step = {s["step"]: s for s in steps}
    assert by_step["local_ocr"]["ok"] is False
    assert by_step["vision"]["ok"] is True


def test_steps_fully_failed_all_logged(tmp_path, monkeypatch):
    """Everything fails → step log records each failure."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr=None, distill=None,
                 vision=None)
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is None
    by_step = {s["step"]: s for s in steps}
    assert by_step["local_ocr"]["ok"] is False
    assert by_step["vision"]["ok"] is False


def test_steps_distillation_falls_back_to_local_parse(tmp_path, monkeypatch):
    """LM distillation unreachable → offline parser; step log shows both."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr="SHELL\nUNLEADED\nTOTAL $45.20\n05/01/2026",
                 distill=lambda c, t: None,  # LM distillation unreachable
                 vision=AssertionError("vision rescue should not run"))
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None  # local parse rescued it
    by_step = {s["step"]: s for s in steps}
    assert by_step["local_ocr"]["ok"] is True
    assert by_step["distillation"]["ok"] is False
    assert by_step["local_parse"]["ok"] is True


def test_steps_empty_when_step_log_none(tmp_path, monkeypatch):
    """Omitting step_log (backward compat) must not affect the return value."""
    img = _setup(monkeypatch, tmp_path,
                 local_ocr="SHELL\nTOTAL $45.20",
                 distill=lambda c, t: dict(GOOD),
                 vision=AssertionError("vision rescue should not run"))
    data = pr._extract_receipt_with_status(MagicMock(), img, None)  # no step_log
    assert data is not None
    assert "_steps" not in data


# ── safe_receipt_data whitelist ────────────────────────────────────────────────

def test_safe_receipt_data_includes_steps():
    import server
    steps = [{"step": "autocrop", "label": "Autocrop", "detail": "ok", "ok": True, "duration_s": 0.0}]
    d = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "_steps": steps}
    out = server._safe_receipt_data(d)
    assert "_steps" in out
    assert out["_steps"] == steps
