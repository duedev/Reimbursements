"""LLM request-rate limiter + failure-reason surfacing.

Covers the free-tier 429 guard (a sliding-window cap on outbound LLM requests,
enabled by default) and the project's "always say *why* a call failed" practice:
_describe_llm_error / _llm_call record a concrete reason on a thread-local channel
that the step log reads, so a failed stage never shows a bare "no text".
"""
import time
from types import SimpleNamespace

import pytest

import process_receipts as pr
import server


# ── Sliding-window rate limiter ────────────────────────────────────────────────

def test_rate_limiter_allows_burst_up_to_limit():
    rl = pr._RateLimiter(max_requests=5, window_s=60.0, enabled=True)
    t0 = time.monotonic()
    for _ in range(5):
        rl.acquire()
    # Five acquisitions under the cap return immediately (no meaningful wait).
    assert time.monotonic() - t0 < 0.5


def test_rate_limiter_blocks_past_limit_until_window_frees():
    rl = pr._RateLimiter(max_requests=2, window_s=0.5, enabled=True)
    rl.acquire()
    rl.acquire()
    t0 = time.monotonic()
    rl.acquire()  # 3rd must wait ~window for an early slot to age out
    assert time.monotonic() - t0 >= 0.4


def test_rate_limiter_disabled_never_blocks():
    rl = pr._RateLimiter(max_requests=1, window_s=60.0, enabled=False)
    t0 = time.monotonic()
    for _ in range(50):
        rl.acquire()
    assert time.monotonic() - t0 < 0.5


def test_rate_limiter_zero_limit_never_blocks():
    rl = pr._RateLimiter(max_requests=0, window_s=60.0, enabled=True)
    t0 = time.monotonic()
    for _ in range(50):
        rl.acquire()  # max<=0 means "off"
    assert time.monotonic() - t0 < 0.5


def test_set_rate_limit_reconfigures_global():
    saved = (pr.LLM_RATE_LIMIT_PER_MIN, pr.LLM_RATE_LIMIT_ENABLED)
    try:
        pr.set_rate_limit(per_min=7, enabled=False)
        assert pr.LLM_RATE_LIMIT_PER_MIN == 7
        assert pr.LLM_RATE_LIMIT_ENABLED is False
        assert pr._RATE_LIMITER.max_requests == 7
        assert pr._RATE_LIMITER.enabled is False
    finally:
        pr.set_rate_limit(per_min=saved[0], enabled=saved[1])


def test_rate_limit_default_is_free_tier_ceiling():
    # The shipped default matches OpenRouter's documented free-tier cap (20/min)
    # and is enabled out of the box.
    assert pr.LLM_RATE_LIMIT_PER_MIN == 20
    assert pr.LLM_RATE_LIMIT_ENABLED is True


def test_default_concurrency_is_serial():
    assert pr.MAX_PARALLEL_REQUESTS == 1


# ── Failure-reason classifier ──────────────────────────────────────────────────

class _StatusError(Exception):
    """Mimics an openai.APIStatusError (carries .status_code + .body)."""

    def __init__(self, status, message=""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.body = {"error": {"message": message}} if message else None


def test_describe_llm_error_classifies_429():
    msg = pr._describe_llm_error(_StatusError(429, "rate limit exceeded"))
    assert "429" in msg and "rate" in msg.lower()


def test_describe_llm_error_classifies_404():
    msg = pr._describe_llm_error(
        _StatusError(404, "No endpoints found that support image input"))
    assert "404" in msg
    assert "image" in msg.lower() or "provider" in msg.lower()


def test_describe_llm_error_classifies_auth():
    msg = pr._describe_llm_error(_StatusError(401, "invalid key"))
    assert "401" in msg


def test_describe_llm_error_classifies_timeout():
    msg = pr._describe_llm_error(TimeoutError("request timed out"))
    assert "timed out" in msg.lower()


# ── _llm_call records / clears the reason on the thread-local channel ───────────

def _client_raising(exc):
    def create(**kw):
        raise exc
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def _client_returning(content):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]))))


def test_llm_call_sets_reason_on_failure():
    pr._set_llm_error(None)
    with pytest.raises(_StatusError):
        pr._llm_call(_client_raising(_StatusError(429, "slow down")),
                     model="m", messages=[])
    assert "429" in pr._get_llm_error()


def test_llm_call_clears_reason_on_success():
    pr._set_llm_error("stale reason")
    pr._llm_call(_client_returning("ok"), model="m", messages=[])
    assert pr._get_llm_error() == ""


# ── empty OCR response surfaces a concrete reason (not "no text") ───────────────

def test_extract_raw_ocr_empty_sets_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "encode_image", lambda p: ("", "image/jpeg"))
    out = pr._extract_raw_ocr(_client_returning("   "), tmp_path / "x.jpg", "some-model")
    assert out is None
    assert "empty" in pr._get_llm_error().lower()


def test_extract_raw_ocr_rate_limited_sets_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "encode_image", lambda p: ("", "image/jpeg"))
    out = pr._extract_raw_ocr(_client_raising(_StatusError(429, "too many")),
                              tmp_path / "x.jpg", "some-model")
    assert out is None
    assert "429" in pr._get_llm_error()


# ── settings wiring (apply path) ───────────────────────────────────────────────

def test_rate_limit_apply_from_config():
    saved = (pr.LLM_RATE_LIMIT_PER_MIN, pr.LLM_RATE_LIMIT_ENABLED)
    try:
        server._apply_processing_config(
            {"processing": {"rate_limit_per_min": 15, "rate_limit_enabled": True}})
        assert pr.LLM_RATE_LIMIT_PER_MIN == 15
        assert pr._RATE_LIMITER.max_requests == 15
        assert pr.LLM_RATE_LIMIT_ENABLED is True
        # clamp + disable
        server._apply_processing_config(
            {"processing": {"rate_limit_per_min": 99999, "rate_limit_enabled": False}})
        assert pr.LLM_RATE_LIMIT_PER_MIN == 1000
        assert pr.LLM_RATE_LIMIT_ENABLED is False
    finally:
        pr.set_rate_limit(per_min=saved[0], enabled=saved[1])
