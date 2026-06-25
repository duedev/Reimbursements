"""Sent-ledger dedup: receipts already included in a sent report are recorded so
a later re-add is surfaced as "already reported" and excluded from the next report
(unless the user forces it back in). Covers the web worker/endpoint path, the
helpers, persistence, and watch-mode parity.
"""
from pathlib import Path

import pytest
from PIL import Image

import server
import process_receipts as pr


# ── receipt_identity (shared key) ──────────────────────────────────────────────

def test_receipt_identity_is_canonical():
    a = {"vendor": "Shell ", "date": "2026-05-01", "amount": 45.2}
    b = {"vendor": "shell", "date": "2026-05-01", "amount": 45.20}
    assert pr.receipt_identity(a) == pr.receipt_identity(b)
    assert pr.receipt_identity(a) == ("shell", "2026-05-01", 45.2)


def test_receipt_identity_zero_amount_is_non_identity():
    assert pr.receipt_identity({"vendor": "X", "date": "2026-01-01"})[2] == 0
    assert pr.receipt_identity({"vendor": "X", "amount": "junk"})[2] == 0


# ── _record_sent / _already_sent ───────────────────────────────────────────────

@pytest.fixture()
def ledger_env():
    server._sent_ledger.clear()
    import multiuser
    multiuser.default_workspace().last_report_date = ""
    yield
    server._sent_ledger.clear()


def test_record_and_lookup(ledger_env):
    results = [
        {"vendor": "Shell", "date": "2026-05-01", "amount": 45.20, "_new_filename": "a.jpg"},
        {"vendor": "Lowes", "date": "2026-05-02", "amount": 12.00, "_new_filename": "b.jpg"},
        {"vendor": "NoAmount", "date": "2026-05-03"},  # skipped — no identity
    ]
    added = server._record_sent(results, "Report1.xlsx")
    assert added == 2
    assert server._already_sent(pr.receipt_identity(results[0]))["report"] == "Report1.xlsx"
    assert server._already_sent(pr.receipt_identity(results[1])) is not None
    # Zero-amount receipt is never a match
    assert server._already_sent(pr.receipt_identity(results[2])) is None
    # Unknown receipt
    assert server._already_sent(("ghost", "2020-01-01", 9.99)) is None


def test_record_is_idempotent(ledger_env):
    r = [{"vendor": "Shell", "date": "2026-05-01", "amount": 45.20}]
    assert server._record_sent(r, "R1") == 1
    assert server._record_sent(r, "R2") == 0          # already present
    assert len(server._sent_ledger) == 1


def test_record_advances_last_report_date(ledger_env):
    import multiuser
    server._record_sent([
        {"vendor": "A", "date": "2026-05-01", "amount": 1.0},
        {"vendor": "B", "date": "2026-05-09", "amount": 2.0},
    ], "R")
    assert multiuser.default_workspace().last_report_date == "2026-05-09"


# ── Worker marks re-added receipts; generation excludes them ────────────────────

@pytest.fixture()
def worker_env(tmp_path, monkeypatch, ledger_env):
    images = tmp_path / "receipts"
    images.mkdir()
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(pr, "AUTOCROP_ENABLED", False)
    server._worker_cancel.clear()
    server._work_queue.clear()
    server._results.clear()
    server._kanban.clear()
    yield images
    server._work_queue.clear()
    server._results.clear()
    server._kanban.clear()


def _enqueue(images, name, fields, monkeypatch):
    tmp_dir = images / f"_upload_{name}"
    tmp_dir.mkdir(exist_ok=True)
    src = tmp_dir / f"{name}.png"
    Image.new("RGB", (400, 600), (90, 90, 90)).save(src, format="PNG")

    def fake_extract(client, path, cb, step_log=None, force_llm_ocr=False):
        return dict(fields, flags=[])

    monkeypatch.setattr(server, "_extract_receipt_with_status", fake_extract)
    server._work_queue.append({"filename": f"{name}.png", "path": str(src),
                               "employee": "E", "job_name": "", "job_number": ""})
    assert server._drain_once() is True


def test_reprocessed_receipt_marked_already_sent(worker_env, monkeypatch):
    images = worker_env
    fields = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01"}

    # First pass — process and "send" (record into ledger).
    _enqueue(images, "first", fields, monkeypatch)
    assert len(server._results) == 1
    assert not server._results[0].get("_already_sent")
    server._record_sent(list(server._results), "Report1.xlsx")

    # Second pass — same receipt re-added. Worker must mark it already-sent.
    server._results.clear()
    _enqueue(images, "second", fields, monkeypatch)
    marked = server._results[0]
    assert marked.get("_already_sent")
    assert marked["_already_sent"]["report"] == "Report1.xlsx"
    assert "already reported" in (marked.get("_flag") or "").lower()


def test_force_included_receipt_not_marked(worker_env, monkeypatch):
    images = worker_env
    fields = {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01"}
    server._record_sent([dict(fields, _new_filename="x.jpg")], "Report1.xlsx")

    # Force-include is set on the queue item / data before processing in practice;
    # here we simulate by pre-marking force-included via a patched extractor.
    def fake_extract(client, path, cb, step_log=None, force_llm_ocr=False):
        return dict(fields, flags=[], _force_included=True)

    monkeypatch.setattr(server, "_extract_receipt_with_status", fake_extract)
    tmp_dir = images / "_upload_force"
    tmp_dir.mkdir()
    src = tmp_dir / "force.png"
    Image.new("RGB", (400, 600), (90, 90, 90)).save(src, format="PNG")
    server._work_queue.append({"filename": "force.png", "path": str(src),
                               "employee": "E", "job_name": "", "job_number": ""})
    assert server._drain_once() is True
    assert not server._results[0].get("_already_sent")


# ── Persistence round-trip ─────────────────────────────────────────────────────

def test_ledger_persists_and_restores(ledger_env, tmp_path, monkeypatch):
    import multiuser
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    server._record_sent([{"vendor": "Shell", "date": "2026-05-01", "amount": 45.20}], "R1")
    server._persist_state()

    # Wipe in-memory and restore.
    server._sent_ledger.clear()
    multiuser.default_workspace().last_report_date = ""
    server._restore_state()
    assert len(server._sent_ledger) == 1
    assert server._already_sent(("shell", "2026-05-01", 45.2)) is not None
    assert multiuser.default_workspace().last_report_date == "2026-05-01"


# ── force-include endpoint ─────────────────────────────────────────────────────

def test_force_include_endpoint(ledger_env, monkeypatch):
    from fastapi.testclient import TestClient
    server._results.clear()
    server._results.append({
        "_file": "r.png", "_new_filename": "r.jpg", "vendor": "Shell",
        "date": "2026-05-01", "amount": 45.20,
        "_already_sent": {"report": "R1", "date": "2026-05-01"},
        "_flag": "Already reported in a previously sent report",
    })
    monkeypatch.setattr(server, "_persist_state", lambda: None)
    client = TestClient(server.app)
    resp = client.post("/results/force-include", json={"filename": "r.jpg", "include": True})
    assert resp.status_code == 200
    assert server._results[0]["_force_included"] is True
    assert server._results[0]["_flag"] == ""        # warning cleared
    server._results.clear()


# ── watch-mode parity ──────────────────────────────────────────────────────────

def test_watch_mode_ledger_skip_and_record(monkeypatch, tmp_path):
    import watch_mode as wm
    state = {"employee_name": "E", "receipts": [], "last_emailed": None, "sent_ledger": []}

    r1 = {"vendor": "Shell", "date": "2026-05-01", "amount": 45.20, "_new_filename": "a.jpg"}
    state["receipts"].append(r1)

    # Let the REAL build_report run (so its ledger filter is exercised); only the
    # spreadsheet write + SMTP send are stubbed.
    out = tmp_path / "R1.xlsx"
    out.write_text("x")
    monkeypatch.setattr(wm, "generate_spreadsheet", lambda results, d, e: out)
    monkeypatch.setattr(wm, "send_workbook_email", lambda p, n, ctx=None: {"ok": True})
    monkeypatch.setattr(wm, "load_email_config",
                        lambda: {"host": "h", "user": "u", "pass": "p", "to": "t",
                                 "from": "", "subject": "s", "port": 587})
    monkeypatch.setattr(wm, "save_state", lambda s: None)
    res = wm.send_report(state)
    assert res["ok"]
    assert wm._ledger_keys(state) == {("shell", "2026-05-01", 45.2)}

    # build_report now excludes the already-sent receipt → nothing left to build.
    with pytest.raises(ValueError):
        wm.build_report(state)
