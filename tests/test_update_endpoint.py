"""Tests for the /results/update inline-edit endpoint."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    server._results.clear()
    server._kanban.clear()
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


def _update(client, field, value, filename="IMG_1.jpg"):
    return client.post("/results/update",
                       json={"filename": filename, "field": field, "value": value})


def test_update_vendor(client):
    r = _update(client, "vendor", "Chevron")
    assert r.status_code == 200 and r.json()["ok"]
    assert server._results[0]["vendor"] == "Chevron"
    assert server._kanban["IMG_1.jpg"]["status"] == "done"


def test_update_amount_strips_currency_formatting(client):
    r = _update(client, "amount", "$1,234.56")
    assert r.status_code == 200
    assert server._results[0]["amount"] == 1234.56


def test_update_invalid_amount_rejected(client):
    r = _update(client, "amount", "lots")
    assert r.status_code == 400
    assert server._results[0]["amount"] == 45.20


def test_update_category_sets_both_fields(client):
    r = _update(client, "category", "misc")
    assert r.status_code == 200
    assert server._results[0]["category"] == "misc"
    assert server._results[0]["_category"] == "misc"


def test_update_invalid_category_rejected(client):
    assert _update(client, "category", "snacks").status_code == 400


def test_update_unknown_field_rejected(client):
    assert _update(client, "_image_path", "/etc/passwd").status_code == 400


def test_update_unknown_filename_404(client):
    assert _update(client, "vendor", "X", filename="nope.jpg").status_code == 404


def test_update_matches_by_new_filename(client):
    r = _update(client, "vendor", "Chevron", filename="fuel_05-01-26_shell.jpg")
    assert r.status_code == 200
    assert server._results[0]["vendor"] == "Chevron"


def test_update_persists_state(client, tmp_path):
    _update(client, "vendor", "Chevron")
    assert (tmp_path / ".app_state.json").exists()


def test_edit_recomputes_duplicates(client):
    server._results.append({
        "vendor": "Chevron", "date": "2026-05-01", "amount": 45.20,
        "category": "fuel", "_category": "fuel", "_file": "IMG_2.jpg",
    })
    # Renaming receipt 1's vendor to Chevron makes the two identical
    r = _update(client, "vendor", "Chevron")
    assert r.status_code == 200
    flags = [res.get("_flag") or "" for res in server._results]
    assert any("duplicate" in f.lower() for f in flags)

    # Changing the amount apart again clears the duplicate flags
    r = _update(client, "amount", "99.99")
    assert r.status_code == 200
    flags = [res.get("_flag") or "" for res in server._results]
    assert not any("duplicate" in f.lower() for f in flags)
