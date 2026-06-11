"""Tests that /generate-spreadsheet uses the live employee name in the filename."""
import pytest
from fastapi.testclient import TestClient

import server
from process_receipts import generate_spreadsheet


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json", raising=False)
    server._results.clear()
    server._kanban.clear()
    server._last_context.update({"employee": "Stale Context", "job_name": "", "job_number": ""})
    server._results.append({
        "vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
        "category": "fuel", "_category": "fuel",
        "ai_summary": "Fuel fill-up", "_file": "IMG_1.jpg",
        "_new_filename": "fuel_05-01-26_shell.jpg",
    })
    with TestClient(server.app) as c:
        yield c
    server._results.clear()
    server._kanban.clear()


def test_generate_uses_request_employee(client):
    r = client.post("/generate-spreadsheet", json={"employee": "Jane Doe"})
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "Reimbursements_Jane_Doe_" in cd
    # Live name should also refresh the server-side context
    assert server._last_context["employee"] == "Jane Doe"


def test_generate_falls_back_to_last_context(client):
    r = client.post("/generate-spreadsheet", json={"employee": "   "})
    assert r.status_code == 200
    assert "Reimbursements_Stale_Context_" in r.headers["content-disposition"]


def test_filename_sanitization(tmp_path):
    results = [{
        "vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
        "category": "fuel", "_category": "fuel", "_file": "IMG_1.jpg",
    }]
    path = generate_spreadsheet(results, tmp_path, employee_name="Jane O'Doe!")
    assert "Reimbursements_Jane_ODoe_" in str(path)
