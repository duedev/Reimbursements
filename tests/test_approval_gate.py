"""Tests for the require-approval gate on /generate-spreadsheet."""
import json

import pytest
from fastapi.testclient import TestClient

import server


def _receipt(filename, vendor, approved):
    return {
        "vendor": vendor, "date": "2026-05-01", "amount": 10.0,
        "category": "misc", "_category": "misc",
        "ai_summary": "test", "_file": filename, "_approved": approved,
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json", raising=False)
    server._results.clear()
    server._kanban.clear()
    server._results.append(_receipt("a.jpg", "Shell", True))
    server._results.append(_receipt("b.jpg", "Home Depot", False))
    with TestClient(server.app) as c:
        yield c
    server._results.clear()
    server._kanban.clear()


def _set_gate(client, enabled):
    r = client.post("/settings/review", json={"require_approval": enabled})
    assert r.status_code == 200


def test_gate_off_allows_unapproved(client):
    _set_gate(client, False)
    r = client.post("/generate-spreadsheet", json={})
    assert r.status_code == 200


def test_gate_on_blocks_unapproved(client):
    _set_gate(client, True)
    r = client.post("/generate-spreadsheet", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert "1 of 2" in body["error"]


def test_gate_on_allows_fully_approved(client):
    _set_gate(client, True)
    for rec in server._results:
        rec["_approved"] = True
    r = client.post("/generate-spreadsheet", json={})
    assert r.status_code == 200


def test_gate_ignores_excluded_receipts(client):
    _set_gate(client, True)
    r = client.post("/generate-spreadsheet", json={"exclude_filenames": ["b.jpg"]})
    assert r.status_code == 200


def test_gate_setting_round_trip(client):
    _set_gate(client, True)
    assert client.get("/settings/review").json()["require_approval"] is True
    _set_gate(client, False)
    assert client.get("/settings/review").json()["require_approval"] is False


def test_ui_gate_lives_in_generate_card(client):
    page = client.get("/").text
    gen = page.find('id="generate-card"')
    gate = page.find('id="require-approval"')
    csv_btn = page.find('id="export-csv-btn"')
    assert gen != -1 and gate != -1
    assert gen < gate  # checkbox is inside the generate card markup
    assert page.find('id="approval-gate-status"') > gen
    # The old settings-tab section is gone
    assert "Approval workflow" not in page
    assert 'id="review-saved-msg"' not in page
