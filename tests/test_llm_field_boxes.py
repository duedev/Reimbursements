"""Tests for LLM-placed field boxes (spatial awareness) from the vision path."""
import json

import process_receipts as pr


def test_normalize_llm_boxes_basic():
    raw = {
        "vendor": {"x": 0.1, "y": 0.05, "w": 0.5, "h": 0.1, "confidence": 92},
        "amount": {"x": 0.6, "y": 0.8, "w": 0.3, "h": 0.08, "confidence": 70.4},
    }
    out = pr._normalize_llm_boxes(raw)
    assert out["vendor"] == [0.1, 0.05, 0.5, 0.1, 92.0]
    assert out["amount"][4] == 70.4
    assert "date" not in out


def test_normalize_llm_boxes_clamps_and_drops_bad():
    raw = {
        "vendor": {"x": -0.2, "y": 2.0, "w": 1.5, "h": 0.1, "confidence": 250},
        "date":   {"x": 0.1, "y": 0.1, "w": 0, "h": 0.1, "confidence": 50},   # zero width → drop
        "amount": "not-a-box",                                                  # wrong type → drop
    }
    out = pr._normalize_llm_boxes(raw)
    assert out["vendor"] == [0.0, 1.0, 1.0, 0.1, 100.0]   # clamped
    assert "date" not in out and "amount" not in out


def test_normalize_llm_boxes_handles_junk():
    assert pr._normalize_llm_boxes(None) == {}
    assert pr._normalize_llm_boxes([]) == {}
    assert pr._normalize_llm_boxes({"vendor": {"x": "a", "y": 0, "w": 1, "h": 1}}) == {}


def test_parse_llm_record_lifts_boxes_to_private_key():
    raw = json.dumps({
        "vendor": "Shell", "amount": 45.2, "date": "2026-05-01",
        "boxes": {"amount": {"x": 0.6, "y": 0.8, "w": 0.3, "h": 0.05, "confidence": 88}},
    })
    rec = pr._parse_llm_record(raw)
    assert rec is not None
    assert "boxes" not in rec                       # raw key removed
    assert rec["_llm_field_boxes"]["amount"][:4] == [0.6, 0.8, 0.3, 0.05]
    assert rec["_llm_field_boxes"]["amount"][4] == 88.0


def test_parse_llm_record_without_boxes_has_no_key():
    rec = pr._parse_llm_record(json.dumps({"vendor": "Shell", "amount": 1.0}))
    assert rec is not None
    assert "_llm_field_boxes" not in rec


def test_vision_prompt_requests_boxes():
    assert '"boxes"' in pr._GEMMA_VISION_TEMPLATE
    assert "confidence" in pr._GEMMA_VISION_TEMPLATE
