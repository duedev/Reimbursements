"""Tests for the standalone watch-mode daemon's inbox processing.

Covers the previously-untested process_inbox loop (dedup / move / state) and the
H5 fix that the watcher builds its client through the provider-aware make_client.
"""
from unittest.mock import MagicMock

import pytest
from PIL import Image

import process_receipts
import watch_mode


@pytest.fixture()
def wm_dirs(tmp_path, monkeypatch):
    inbox  = tmp_path / "inbox"
    staged = tmp_path / "staged"
    state  = tmp_path / "state"
    for d in (inbox, staged, state):
        d.mkdir()
    monkeypatch.setattr(watch_mode, "WATCH_INBOX", inbox)
    monkeypatch.setattr(watch_mode, "WATCH_STAGED", staged)
    monkeypatch.setattr(watch_mode, "STATE_FILE", state / "receipts_state.json")
    return inbox, staged


def _img(path):
    Image.new("RGB", (220, 320), (210, 210, 210)).save(path, "PNG")


def test_process_inbox_extracts_moves_and_records(wm_dirs, monkeypatch):
    inbox, staged = wm_dirs
    _img(inbox / "IMG_1.png")
    monkeypatch.setattr(watch_mode, "extract_receipt_data",
                        lambda client, p: {"vendor": "Shell", "amount": 12.5,
                                           "date": "2026-05-01"})
    state = {"receipts": []}
    n = watch_mode.process_inbox(MagicMock(), state)
    assert n == 1
    assert len(state["receipts"]) == 1 and state["receipts"][0]["vendor"] == "Shell"
    # Original was renamed + moved out of the inbox into staged.
    assert not (inbox / "IMG_1.png").exists()
    assert any(staged.iterdir())


def test_process_inbox_skips_already_staged(wm_dirs, monkeypatch):
    inbox, staged = wm_dirs
    (staged / "IMG_1.png").write_bytes(b"x")
    _img(inbox / "IMG_1.png")
    called = []
    monkeypatch.setattr(watch_mode, "extract_receipt_data",
                        lambda c, p: called.append(p) or {"vendor": "x"})
    n = watch_mode.process_inbox(MagicMock(), {"receipts": []})
    assert n == 0 and called == []


def test_process_inbox_keeps_failed_in_inbox(wm_dirs, monkeypatch):
    inbox, _ = wm_dirs
    _img(inbox / "IMG_1.png")
    monkeypatch.setattr(watch_mode, "extract_receipt_data", lambda c, p: None)
    state = {"receipts": []}
    n = watch_mode.process_inbox(MagicMock(), state)
    assert n == 0 and state["receipts"] == []
    # On extraction failure the file is left in the inbox (not lost).
    assert (inbox / "IMG_1.png").exists()


def test_process_inbox_ignores_non_images(wm_dirs, monkeypatch):
    inbox, _ = wm_dirs
    (inbox / "notes.txt").write_text("hello")
    monkeypatch.setattr(watch_mode, "extract_receipt_data",
                        lambda c, p: {"vendor": "x"})
    assert watch_mode.process_inbox(MagicMock(), {"receipts": []}) == 0


def test_watch_mode_client_is_provider_aware():
    # H5: the watcher must build via make_client (provider-aware), not a hard-coded
    # dummy-key OpenAI() that breaks OpenRouter/custom endpoints.
    assert watch_mode.make_client is process_receipts.make_client
