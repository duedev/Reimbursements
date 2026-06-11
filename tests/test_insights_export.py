"""Tests for the stats aggregation, CSV export, and report history endpoints."""
import csv
import io

import pytest
from fastapi.testclient import TestClient

import server
from server import _compute_stats, _results_to_csv


def _sample_results():
    return [
        {"vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
         "_category": "fuel", "_file": "a.jpg", "_amount_verified": True},
        {"vendor": "Shell", "date": "2026-05-03", "amount": 50.00,
         "_category": "fuel", "_file": "b.jpg"},
        {"vendor": "Home Depot", "date": "2026-05-02", "amount": 120.00,
         "_category": "mats", "_file": "c.jpg", "_flag": "Amount exceeds threshold"},
        {"vendor": "Butch's, \"Grinders\"", "date": "2026-05-02", "amount": 18.50,
         "_category": "misc", "_file": "d.jpg", "ai_summary": "Lunch"},
    ]


# ── _compute_stats ─────────────────────────────────────────────────────────────

def test_stats_totals():
    s = _compute_stats(_sample_results())
    assert s["count"] == 4
    assert s["total"] == 233.70
    assert s["average"] == round(233.70 / 4, 2)
    assert s["flagged"] == 1
    assert s["verified"] == 1


def test_stats_by_category():
    s = _compute_stats(_sample_results())
    assert s["by_category"]["fuel"] == {"count": 2, "total": 95.20}
    assert s["by_category"]["mats"]["total"] == 120.00


def test_stats_top_vendors_sorted_by_total():
    s = _compute_stats(_sample_results())
    assert s["top_vendors"][0]["vendor"] == "Home Depot"
    assert s["top_vendors"][1] == {"vendor": "Shell", "count": 2, "total": 95.20}


def test_stats_timeline_sorted_and_merged():
    s = _compute_stats(_sample_results())
    days = [t["date"] for t in s["timeline"]]
    assert days == sorted(days)
    may2 = next(t for t in s["timeline"] if t["date"] == "2026-05-02")
    assert may2["total"] == 138.50   # mats + misc on the same day


def test_stats_empty():
    s = _compute_stats([])
    assert s["count"] == 0 and s["total"] == 0.0 and s["average"] == 0.0
    assert s["timeline"] == [] and s["top_vendors"] == []


# ── CSV export ─────────────────────────────────────────────────────────────────

def test_csv_round_trip():
    text = _results_to_csv(_sample_results())
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:4] == ["Category", "Date", "Vendor", "Amount"]
    assert len(rows) == 5
    # sorted by date — Shell 5/1 first
    assert rows[1][2] == "Shell" and rows[1][3] == "45.20"
    # vendor with comma and quotes survives CSV escaping
    assert any(r[2] == 'Butch\'s, "Grinders"' for r in rows[1:])


# ── Endpoints ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    server._results.clear()
    server._kanban.clear()
    with TestClient(server.app) as c:
        yield c
    server._results.clear()
    server._kanban.clear()


def test_stats_endpoint(client):
    server._results.extend(_sample_results())
    d = client.get("/stats").json()
    assert d["count"] == 4 and d["total"] == 233.70


def test_csv_endpoint(client):
    server._results.extend(_sample_results())
    r = client.get("/export/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "Shell" in r.text


def test_csv_endpoint_empty_404(client):
    assert client.get("/export/csv").status_code == 404


def test_reports_listing(client, tmp_path):
    (tmp_path / "Reimbursements_Jane_2026-05-01.xlsx").write_bytes(b"x" * 100)
    (tmp_path / "Reimbursements_Jane_2026-06-01.xlsx").write_bytes(b"y" * 200)
    (tmp_path / "unrelated.xlsx").write_bytes(b"z")
    d = client.get("/reports").json()
    names = [r["filename"] for r in d["reports"]]
    assert len(names) == 2 and "unrelated.xlsx" not in names


def test_report_download(client, tmp_path):
    (tmp_path / "Reimbursements_Jane_2026-05-01.xlsx").write_bytes(b"workbook")
    r = client.get("/reports/download",
                   params={"filename": "Reimbursements_Jane_2026-05-01.xlsx"})
    assert r.status_code == 200
    assert r.content == b"workbook"


def test_report_download_rejects_traversal_and_unknown(client, tmp_path):
    assert client.get("/reports/download",
                      params={"filename": "../secret.xlsx"}).status_code == 400
    assert client.get("/reports/download",
                      params={"filename": "evil.xlsx"}).status_code == 400
    assert client.get("/reports/download",
                      params={"filename": "Reimbursements_missing.xlsx"}).status_code == 404
