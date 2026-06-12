"""Tests for /results/add-manual — reviewing must not clobber extraction metadata."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "CONFIG_FILE", tmp_path / ".app_config.json", raising=False)
    server._results.clear()
    server._kanban.clear()
    server._results.append({
        "vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
        "category": "fuel", "_category": "fuel",
        "ai_summary": "Fuel fill-up", "_file": "IMG_1.jpg",
        "_new_filename": "fuel_05-01-26_shell.jpg",
        "_flag": "", "_confidence": 92, "_amount_verified": True,
        "_ocr_engine": "paddleocr", "_proc_seconds": 12.5,
    })
    with TestClient(server.app) as c:
        yield c
    server._results.clear()
    server._kanban.clear()


def _form(**overrides):
    base = {
        "filename": "IMG_1.jpg", "vendor": "Shell", "date": "2026-05-01",
        "amount": "45.20", "category": "fuel", "summary": "Fuel fill-up",
        "review_required": False, "approved": True,
    }
    base.update(overrides)
    return base


def test_approving_preserves_extraction_metadata(client):
    r = client.post("/results/add-manual", json=_form())
    assert r.status_code == 200
    rec = server._results[0]
    assert rec["_approved"] is True
    assert rec["_confidence"] == 92
    assert rec["_amount_verified"] is True
    assert rec["_flag"] == ""              # not rewritten as "Manual entry"
    assert rec["_ocr_engine"] == "paddleocr"


def test_editing_amount_clears_amount_verified(client):
    r = client.post("/results/add-manual", json=_form(amount="50.00"))
    assert r.status_code == 200
    rec = server._results[0]
    assert rec["amount"] == 50.00
    assert "_amount_verified" not in rec
    assert rec["_confidence"] == 92        # confidence still preserved


def test_new_manual_entry_gets_manual_flag(client):
    r = client.post("/results/add-manual", json=_form(filename="failed_receipt.jpg",
                                                      vendor="Depot", approved=False))
    assert r.status_code == 200
    assert len(server._results) == 2
    rec = server._results[1]
    assert rec["_flag"] == "Manual entry"
    assert rec["_confidence"] is None
    assert rec["_approved"] is False


def test_update_matches_renamed_file(client):
    r = client.post("/results/add-manual", json=_form(filename="fuel_05-01-26_shell.jpg"))
    assert r.status_code == 200
    assert len(server._results) == 1       # updated in place, not duplicated
    assert server._results[0]["_approved"] is True
