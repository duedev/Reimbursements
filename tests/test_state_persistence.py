"""Tests for crash-safe server state persistence (results/board survive restarts)."""
import json

import pytest

import server


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point the state file at a temp dir and reset server globals."""
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    server._results.clear()
    server._kanban.clear()
    server._last_context.update({"employee": "Employee", "job_name": "", "job_number": ""})
    yield tmp_path
    server._results.clear()
    server._kanban.clear()


def _done_receipt(fname="fuel_05-01-26_shell.jpg"):
    return {"vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
            "_category": "fuel", "_file": "IMG_1.jpg", "_new_filename": fname}


def test_round_trip_restores_results_and_board(isolated_state):
    server._results.append(_done_receipt())
    server._kanban["IMG_1.jpg"] = {"status": "done", "data": {"vendor": "Shell"}, "model": ""}
    server._kanban["IMG_2.jpg"] = {"status": "failed", "data": {"_error": "low confidence"}, "model": ""}
    server._last_context.update({"employee": "Jane", "job_name": "HQ", "job_number": "JB-1"})

    server._persist_state()
    server._results.clear()
    server._kanban.clear()
    server._last_context.update({"employee": "Employee", "job_name": "", "job_number": ""})

    server._restore_state()
    assert len(server._results) == 1
    assert server._results[0]["vendor"] == "Shell"
    assert server._kanban["IMG_1.jpg"]["status"] == "done"
    assert server._kanban["IMG_2.jpg"]["status"] == "failed"
    assert server._last_context["employee"] == "Jane"
    assert server._last_context["job_number"] == "JB-1"


def test_in_flight_items_not_persisted(isolated_state):
    server._kanban["queued.jpg"]  = {"status": "queued", "data": {}, "model": ""}
    server._kanban["working.jpg"] = {"status": "distilling", "data": {}, "model": "gemma"}
    server._kanban["done.jpg"]    = {"status": "done", "data": {}, "model": ""}

    server._persist_state()
    server._kanban.clear()
    server._restore_state()

    assert "queued.jpg" not in server._kanban
    assert "working.jpg" not in server._kanban
    assert "done.jpg" in server._kanban


def test_restore_with_no_state_file_is_noop(isolated_state):
    server._restore_state()
    assert server._results == []
    assert server._kanban == {}


def test_restore_with_corrupt_state_file_is_noop(isolated_state, tmp_path):
    (tmp_path / ".app_state.json").write_text("{not valid json")
    server._restore_state()
    assert server._results == []
    assert server._kanban == {}


def test_persist_is_atomic_no_tmp_left_behind(isolated_state, tmp_path):
    server._results.append(_done_receipt())
    server._persist_state()
    assert (tmp_path / ".app_state.json").exists()
    assert not (tmp_path / ".app_state.json.tmp").exists()
    payload = json.loads((tmp_path / ".app_state.json").read_text())
    assert payload["results"][0]["vendor"] == "Shell"


def test_clearing_state_persists_empty_snapshot(isolated_state):
    server._results.append(_done_receipt())
    server._kanban["IMG_1.jpg"] = {"status": "done", "data": {}, "model": ""}
    server._persist_state()

    server._results.clear()
    server._kanban.clear()
    server._persist_state()   # what /results/clear and /queue/clear-all do

    server._restore_state()
    assert server._results == []
    assert server._kanban == {}
