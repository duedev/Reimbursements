"""Per-batch LLM-OCR throttle breaker + readable 429 error reasons.

Driven by a real run where OpenRouter's free tier was exhausted: every optional
LLM-OCR (vision) pass 429'd, the giant nested ``previous_errors`` dump flooded the
step/run log, and the doomed vision calls starved the essential distillation call
of the shared per-minute bucket (dropping a receipt to the offline parser).

Two fixes are covered here:
  1. ``_describe_llm_error`` recovers just the headline provider message and caps
     it, so the log shows e.g. "rate-limited (HTTP 429) — Rate limit exceeded:
     free-models-per-min." instead of a multi-thousand-character JSON dump.
  2. After a couple of throttles the optional LLM-OCR pass is skipped for the rest
     of the batch (RapidOCR already supplied the text), freeing the shared quota.
"""
from unittest.mock import MagicMock

import process_receipts as pr


# A trimmed copy of the real OpenRouter 429 body the SDK stuffs into exc.message.
_BIG_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit exceeded: "
    "free-models-per-min. ', 'code': 429, 'metadata': {'headers': "
    "{'X-RateLimit-Limit': '16', 'X-RateLimit-Remaining': '0', "
    "'X-RateLimit-Reset': '1781920200000'}, 'provider_name': None, "
    "'previous_errors': [" + ", ".join(
        ["{'code': 429, 'message': 'Rate limit exceeded: free-models-per-min. '}"] * 7
    ) + "]}}, 'user_id': 'user_abc'}"
)


class _SDKError(Exception):
    """Mimics an openai.APIStatusError whose body wasn't parsed (message only)."""

    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status_code = status
        self.message = message
        self.body = body


# ── readable 429 reasons ───────────────────────────────────────────────────────

def test_describe_429_recovers_clean_message_from_blob():
    msg = pr._describe_llm_error(_SDKError(429, _BIG_429))
    assert msg.startswith("rate-limited (HTTP 429)")
    assert "Rate limit exceeded: free-models-per-min." in msg
    # The nested dump is gone — no header keys / user id bleed through.
    assert "previous_errors" not in msg
    assert "user_id" not in msg
    assert "X-RateLimit" not in msg


def test_describe_429_message_is_capped():
    msg = pr._describe_llm_error(_SDKError(429, _BIG_429))
    # Headline + a short detail, never the multi-thousand-char raw body.
    assert len(msg) < 280


def test_describe_truncates_overlong_provider_detail():
    long_detail = "x" * 5000
    err = _SDKError(500, long_detail, body={"error": {"message": long_detail}})
    msg = pr._describe_llm_error(err)
    assert "provider error (HTTP 500)" in msg
    assert len(msg) < 280
    assert msg.rstrip().endswith("…")


def test_describe_still_uses_structured_body_when_present():
    err = _SDKError(429, "ignored", body={"error": {"message": "slow down please"}})
    msg = pr._describe_llm_error(err)
    assert "slow down please" in msg


# ── throttle classifier ────────────────────────────────────────────────────────

def test_reason_is_throttle():
    assert pr._reason_is_throttle("rate-limited (HTTP 429) — …")
    assert pr._reason_is_throttle("Rate limit exceeded")
    assert not pr._reason_is_throttle("model returned an empty response")
    assert not pr._reason_is_throttle("")


# ── breaker state machine ──────────────────────────────────────────────────────

def test_breaker_trips_after_limit_and_resets():
    pr.reset_batch_llm_state()
    assert pr._llm_ocr_suspended() is False
    for _ in range(pr._LLM_OCR_THROTTLE_LIMIT):
        assert pr._llm_ocr_suspended() is False
        pr._note_llm_ocr_throttle()
    assert pr._llm_ocr_suspended() is True
    pr.reset_batch_llm_state()
    assert pr._llm_ocr_suspended() is False


# ── end-to-end: repeated throttles suspend the vision pass for the batch ────────

def _setup_ocr(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_distill_model", "distill-model")
    monkeypatch.setattr(pr, "_active_ocr_model", "vision-model")
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", True)
    # RapidOCR always reads the receipt; distillation always succeeds from it.
    monkeypatch.setattr(pr, "_extract_local_ocr",
                        MagicMock(return_value="SHELL\nTOTAL $45.20"))
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(return_value={"vendor": "Shell", "amount": 45.20,
                                                "date": "2026-05-01", "flags": []}))
    monkeypatch.setattr(pr, "_extract_with_model",
                        MagicMock(side_effect=AssertionError("vision rescue should not run")))
    return img


def test_llm_ocr_pass_skipped_after_repeated_throttles(tmp_path, monkeypatch):
    pr.reset_batch_llm_state()
    img = _setup_ocr(monkeypatch, tmp_path)

    calls = {"n": 0}

    def _raw_ocr(client, image_path, model_id):
        calls["n"] += 1
        pr._set_llm_error("rate-limited (HTTP 429) — Rate limit exceeded: free-models-per-min.")
        return None  # throttled → no transcription

    monkeypatch.setattr(pr, "_extract_raw_ocr", _raw_ocr)

    # Process several receipts in the same batch.
    last_steps = None
    for _ in range(pr._LLM_OCR_THROTTLE_LIMIT + 2):
        steps: list = []
        data = pr._extract_receipt_with_status(MagicMock(), img, None, steps)
        assert data is not None  # RapidOCR + distillation still produced fields
        last_steps = steps

    # The vision pass was attempted only up to the throttle limit, then suspended.
    assert calls["n"] == pr._LLM_OCR_THROTTLE_LIMIT
    # The final receipt's step log records the skip (not another 429 dump).
    llm_step = next(s for s in last_steps if s["step"] == "llm_ocr")
    assert llm_step["ok"] is False
    assert "skipped" in llm_step["detail"].lower()


def test_llm_ocr_not_skipped_without_throttle(tmp_path, monkeypatch):
    """A successful vision pass never trips the breaker."""
    pr.reset_batch_llm_state()
    img = _setup_ocr(monkeypatch, tmp_path)
    monkeypatch.setattr(pr, "_extract_raw_ocr",
                        MagicMock(return_value="SHELL TOTAL 45.20"))
    monkeypatch.setattr(pr, "_combine_ocr_sources",
                        lambda a, b: (a or "") + "\n" + (b or ""))

    for _ in range(5):
        steps: list = []
        pr._extract_receipt_with_status(MagicMock(), img, None, steps)
        llm_step = next(s for s in steps if s["step"] == "llm_ocr")
        assert llm_step["ok"] is True
        assert "skipped" not in llm_step["detail"].lower()
    assert pr._llm_ocr_suspended() is False
