"""Tests for LM Studio model auto-selection (adopt whatever is loaded)."""
from unittest.mock import patch

import process_receipts as pr


def test_chat_model_filter():
    assert pr._looks_like_chat_model("qwen2.5-vl-7b-instruct")
    assert pr._looks_like_chat_model("google/gemma-4-12b-qat")
    assert not pr._looks_like_chat_model("text-embedding-nomic")
    assert not pr._looks_like_chat_model("bge-reranker-base")
    assert not pr._looks_like_chat_model("whisper-large-v3")


def test_adopts_loaded_model_when_default_absent(monkeypatch):
    # Configured default isn't loaded; only a non-Gemma vision model + an embedder are
    monkeypatch.setattr(pr, "_active_distill_model", "google/gemma-4-12b-qat")
    with patch.object(pr, "list_available_models",
                      return_value=["nomic-embed-text", "qwen2.5-vl-7b-instruct"]):
        pr.initialize_models()
    assert pr._active_distill_model == "qwen2.5-vl-7b-instruct"


def test_prefers_gemma_when_present(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "absent-model")
    with patch.object(pr, "list_available_models",
                      return_value=["llava-1.6", "google/gemma-4-12b"]):
        pr.initialize_models()
    assert pr._active_distill_model == "google/gemma-4-12b"


def test_keeps_active_model_if_already_loaded(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "llava-1.6")
    with patch.object(pr, "list_available_models",
                      return_value=["llava-1.6", "google/gemma-4-12b"]):
        pr.initialize_models()
    assert pr._active_distill_model == "llava-1.6"


def test_no_models_leaves_active_unchanged(monkeypatch):
    monkeypatch.setattr(pr, "_active_distill_model", "google/gemma-4-12b-qat")
    with patch.object(pr, "list_available_models", return_value=[]):
        pr.initialize_models()
    assert pr._active_distill_model == "google/gemma-4-12b-qat"
