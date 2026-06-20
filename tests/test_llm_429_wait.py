"""Wait-for-bucket-refill on a 429'd essential call.

When the free-tier per-minute bucket is momentarily drained (e.g. a previous run
in the same minute), the *essential* distillation / vision call should wait for
the bucket to refill — honouring the provider's reset hint, bounded by
LLM_429_MAX_WAIT — and retry, instead of dropping straight to the offline parser.
The optional LLM-OCR pass never waits (it's skipped under throttling).
"""
import time
from unittest.mock import MagicMock

import process_receipts as pr


class _Status429(Exception):
    """Mimics an openai 429 with either a structured body or response headers."""

    def __init__(self, body=None, headers=None):
        super().__init__("rate limited")
        self.status_code = 429
        self.body = body
        if headers is not None:
            self.response = MagicMock(headers=headers)


def _body_with_reset(reset_ms: int) -> dict:
    return {"error": {"code": 429, "message": "Rate limit exceeded",
                      "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}}}


# ── _retry_after_seconds ────────────────────────────────────────────────────────

def test_retry_after_header_wins():
    exc = _Status429(headers={"Retry-After": "3"})
    assert pr._retry_after_seconds(exc) == 3.0


def test_reset_epoch_in_body_metadata():
    reset_ms = int((time.time() + 5) * 1000)
    exc = _Status429(body=_body_with_reset(reset_ms))
    secs = pr._retry_after_seconds(exc)
    assert 3.0 < secs <= 5.5


def test_reset_in_response_headers():
    reset_ms = int((time.time() + 4) * 1000)
    exc = _Status429(headers={"X-RateLimit-Reset": str(reset_ms)})
    assert 2.5 < pr._retry_after_seconds(exc) <= 4.5


def test_stale_or_garbage_reset_returns_zero():
    # Reset in the past → ignored.
    past = int((time.time() - 60) * 1000)
    assert pr._retry_after_seconds(_Status429(body=_body_with_reset(past))) == 0.0
    # No hint at all.
    assert pr._retry_after_seconds(_Status429()) == 0.0
    # Absurdly far future → ignored (sanity bound).
    far = int((time.time() + 9999) * 1000)
    assert pr._retry_after_seconds(_Status429(body=_body_with_reset(far))) == 0.0


# ── _llm_call wait + retry ──────────────────────────────────────────────────────

def _client(side_effects):
    c = MagicMock()
    c.chat.completions.create = MagicMock(side_effect=side_effects)
    return c


def test_waits_then_succeeds(monkeypatch):
    monkeypatch.setattr(pr, "LLM_429_WAIT_ENABLED", True)
    monkeypatch.setattr(pr, "LLM_429_MAX_WAIT", 30.0)
    slept: list = []
    monkeypatch.setattr(pr, "_interruptible_sleep", lambda s: slept.append(s))

    reset_ms = int((time.time() + 2) * 1000)
    ok = MagicMock(name="resp")
    client = _client([_Status429(body=_body_with_reset(reset_ms)), ok])

    out = pr._llm_call(client, wait_on_throttle=True, model="m", messages=[])
    assert out is ok
    assert len(slept) == 1 and slept[0] > 0
    assert client.chat.completions.create.call_count == 2
    assert not pr._get_llm_error()  # cleared on the eventual success


def test_no_wait_when_flag_off(monkeypatch):
    monkeypatch.setattr(pr, "LLM_429_WAIT_ENABLED", True)
    monkeypatch.setattr(pr, "LLM_429_MAX_WAIT", 30.0)
    slept: list = []
    monkeypatch.setattr(pr, "_interruptible_sleep", lambda s: slept.append(s))

    client = _client([_Status429(headers={"Retry-After": "1"})])
    # The optional LLM-OCR path calls _llm_call WITHOUT wait_on_throttle.
    try:
        pr._llm_call(client, model="m", messages=[])
        assert False, "expected the 429 to propagate"
    except _Status429:
        pass
    assert slept == []  # never waited
    assert client.chat.completions.create.call_count == 1


def test_gives_up_when_reset_beyond_budget(monkeypatch):
    monkeypatch.setattr(pr, "LLM_429_WAIT_ENABLED", True)
    monkeypatch.setattr(pr, "LLM_429_MAX_WAIT", 5.0)
    slept: list = []
    monkeypatch.setattr(pr, "_interruptible_sleep", lambda s: slept.append(s))

    # Bucket won't refill for 60s but our budget is 5s → don't burn the time.
    reset_ms = int((time.time() + 60) * 1000)
    client = _client([_Status429(body=_body_with_reset(reset_ms))])
    try:
        pr._llm_call(client, wait_on_throttle=True, model="m", messages=[])
        assert False, "expected the 429 to propagate"
    except _Status429:
        pass
    assert slept == []
    assert client.chat.completions.create.call_count == 1


def test_disabled_globally(monkeypatch):
    monkeypatch.setattr(pr, "LLM_429_WAIT_ENABLED", False)
    monkeypatch.setattr(pr, "LLM_429_MAX_WAIT", 30.0)
    slept: list = []
    monkeypatch.setattr(pr, "_interruptible_sleep", lambda s: slept.append(s))

    client = _client([_Status429(headers={"Retry-After": "1"})])
    try:
        pr._llm_call(client, wait_on_throttle=True, model="m", messages=[])
        assert False
    except _Status429:
        pass
    assert slept == []


def test_set_429_wait_reconfigures():
    saved = (pr.LLM_429_WAIT_ENABLED, pr.LLM_429_MAX_WAIT)
    try:
        pr.set_429_wait(enabled=False, max_wait=12.5)
        assert pr.LLM_429_WAIT_ENABLED is False
        assert pr.LLM_429_MAX_WAIT == 12.5
        pr.set_429_wait(max_wait=-5)  # clamped at 0
        assert pr.LLM_429_MAX_WAIT == 0.0
    finally:
        (pr.LLM_429_WAIT_ENABLED, pr.LLM_429_MAX_WAIT) = saved
