"""Tests for per-item process step logging."""
from pathlib import Path
from unittest.mock import MagicMock

import process_receipts as pr


GOOD = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}


def _setup(monkeypatch, tmp_path, *, lm_ocr, local_ocr, distill, vision):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "ocr-model")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_raw_ocr",      MagicMock(return_value=lm_ocr))
    monkeypatch.setattr(pr, "_extract_local_ocr",    MagicMock(return_value=local_ocr))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(side_effect=distill) if callable(distill) else MagicMock(return_value=distill))
    monkeypatch.setattr(pr, "_extract_with_model",
                        MagicMock(side_effect=vision) if callable(vision) else MagicMock(return_value=vision))
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

def test_steps_recorded_lm_ocr_success(tmp_path, monkeypatch):
    """Happy path: LM OCR → distillation → step log shows both ok."""
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr="SHELL\nTOTAL $45.20", local_ocr=None,
                 distill=lambda c, t: dict(GOOD), vision=None)
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    labels = [s["label"] for s in steps]
    assert "OCR (LM Studio)" in labels
    assert "Distillation" in labels
    assert all(s["ok"] for s in steps)
    # Steps are also attached to data
    assert "_steps" in data
    assert len(data["_steps"]) == len(steps)


def test_steps_lm_ocr_fails_local_ocr_fallback(tmp_path, monkeypatch):
    """LM Studio OCR fails → local OCR (RapidOCR) fallback; step log shows the handoff."""
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr=None, local_ocr="SHELL\nTOTAL $45.20",
                 distill=lambda c, t: dict(GOOD), vision=None)
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    by_step = {s["step"]: s for s in steps}
    assert by_step["lm_ocr"]["ok"] is False
    assert by_step["local_ocr"]["ok"] is True
    assert "fallback" in by_step["local_ocr"]["detail"].lower()
    assert by_step["distillation"]["ok"] is True


def test_steps_vision_path(tmp_path, monkeypatch):
    """No OCR model → direct vision; step records vision step."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock(return_value=dict(GOOD)))
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None
    by_step = {s["step"]: s for s in steps}
    assert "vision" in by_step
    assert by_step["vision"]["ok"] is True


def test_steps_fully_failed_all_logged(tmp_path, monkeypatch):
    """Everything fails → step log records each failure."""
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_extract_with_model",  MagicMock(return_value=None))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=None))
    monkeypatch.setattr(pr, "_extract_local_ocr",    MagicMock(return_value=None))
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is None
    by_step = {s["step"]: s for s in steps}
    assert by_step["vision"]["ok"] is False
    assert by_step["local_ocr"]["ok"] is False


def test_steps_distillation_falls_back_to_local_parse(tmp_path, monkeypatch):
    """LM distillation unreachable → local parse; step log shows both."""
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr="SHELL\nTOTAL $45.20", local_ocr=None,
                 distill=lambda c, t: None,  # LM distillation unreachable
                 vision=None)
    steps: list = []
    data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
    assert data is not None  # local parse rescued it
    by_step = {s["step"]: s for s in steps}
    assert by_step["distillation"]["ok"] is False
    assert "local_parse" in by_step
    assert by_step["local_parse"]["ok"] is True


def test_steps_empty_when_step_log_none(tmp_path, monkeypatch):
    """Omitting step_log (backward compat) must not affect the return value."""
    img = _setup(monkeypatch, tmp_path,
                 lm_ocr="SHELL\nTOTAL $45.20", local_ocr=None,
                 distill=lambda c, t: dict(GOOD), vision=None)
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
