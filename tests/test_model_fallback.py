"""Client-side model fallback ladder.

When a free model "bounces" a request with a SOFT failure (empty / unparseable
reply — the case OpenRouter's own routing counts as success and won't retry), the
pipeline walks to the next free model pinned in LLM_EXTRA_BODY["models"]. It must
NOT advance on a 429 (the whole free tier shares one per-minute bucket), and a
local single-model setup must behave exactly as before.
"""
from types import SimpleNamespace

import pytest

import process_receipts as pr


class _StatusError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.body = {"error": {"message": message}} if message else None


def _client_by_model(mapping, default=""):
    """Fake OpenAI client whose reply (or raised error) depends on model=..."""
    def create(**kw):
        val = mapping.get(kw.get("model"), default)
        if isinstance(val, Exception):
            raise val
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=val))])
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


# ── chain construction ──────────────────────────────────────────────────────────

def test_fallback_chain_local_is_single(monkeypatch):
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {})
    assert pr._fallback_model_chain("only-model") == ["only-model"]


def test_fallback_chain_appends_router_models_capped(monkeypatch):
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {"models": ["a", "b", "c", "d"]})
    monkeypatch.setattr(pr, "LLM_FALLBACK_MAX", 3)
    # primary + 2 fallbacks, capped at LLM_FALLBACK_MAX total
    assert pr._fallback_model_chain("openrouter/free") == ["openrouter/free", "a", "b"]


def test_fallback_chain_dedupes_primary(monkeypatch):
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {"models": ["a", "primary", "b"]})
    monkeypatch.setattr(pr, "LLM_FALLBACK_MAX", 4)
    assert pr._fallback_model_chain("primary") == ["primary", "a", "b"]


# ── advance decision ─────────────────────────────────────────────────────────────

def test_should_advance_on_404_only():
    assert pr._should_advance_model(_StatusError(404)) is True
    assert pr._should_advance_model(_StatusError(429)) is False
    assert pr._should_advance_model(_StatusError(500)) is False
    assert pr._should_advance_model(TimeoutError("x")) is False


# ── chain runner ─────────────────────────────────────────────────────────────────

def test_run_chain_advances_on_soft_failure():
    seen = []

    def attempt(cl, mid):
        seen.append(mid)
        return None if mid == "m1" else "ok"

    assert pr._run_model_chain(None, ["m1", "m2"], attempt) == "ok"
    assert seen == ["m1", "m2"]


def test_run_chain_stops_on_429():
    seen = []

    def attempt(cl, mid):
        seen.append(mid)
        raise _StatusError(429, "slow down")

    with pytest.raises(_StatusError):
        pr._run_model_chain(None, ["m1", "m2"], attempt)
    assert seen == ["m1"]          # never advanced past the throttled model


def test_run_chain_advances_on_404():
    seen = []

    def attempt(cl, mid):
        seen.append(mid)
        if mid == "m1":
            raise _StatusError(404, "no provider")
        return "ok"

    assert pr._run_model_chain(None, ["m1", "m2"], attempt) == "ok"
    assert seen == ["m1", "m2"]


# ── integration: _extract_raw_ocr ────────────────────────────────────────────────

def test_ocr_falls_back_to_next_free_model(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "encode_image", lambda p: ("", "image/jpeg"))
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {"models": ["m2"]})
    monkeypatch.setattr(pr, "LLM_FALLBACK_MAX", 3)
    client = _client_by_model({"m1": "   ", "m2": "REAL RECEIPT TEXT"})
    assert pr._extract_raw_ocr(client, tmp_path / "x.jpg", "m1") == "REAL RECEIPT TEXT"


def test_ocr_does_not_fall_back_on_429(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "encode_image", lambda p: ("", "image/jpeg"))
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {"models": ["m2"]})
    client = _client_by_model({"m1": _StatusError(429, "rate"),
                               "m2": "SHOULD NOT REACH"})
    assert pr._extract_raw_ocr(client, tmp_path / "x.jpg", "m1") is None
    assert "429" in pr._get_llm_error()


# ── integration: _unified_distillation ───────────────────────────────────────────

def test_distill_falls_back_to_next_model(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "m1")
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {"models": ["m2"]})
    monkeypatch.setattr(pr, "LLM_FALLBACK_MAX", 3)
    client = _client_by_model({"m1": "not json at all",
                               "m2": '{"vendor":"Costco","amount":9.5}'})
    out = pr._unified_distillation(client, "RECEIPT TEXT")
    assert out and out["vendor"] == "Costco"


def test_distill_local_single_model_unchanged(monkeypatch):
    # No routing body → chain is the one model; the same-model JSON reprompt still
    # applies (two attempts on the one model), exactly as before the ladder.
    monkeypatch.setattr(pr, "_active_distill_model", "only")
    monkeypatch.setattr(pr, "LLM_EXTRA_BODY", {})
    calls = {"n": 0}

    def create(**kw):
        calls["n"] += 1
        content = "junk" if calls["n"] == 1 else '{"vendor":"Lowes","amount":3.0}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    out = pr._unified_distillation(client, "RECEIPT TEXT")
    assert out and out["vendor"] == "Lowes"
    assert calls["n"] == 2          # initial + same-model reprompt
