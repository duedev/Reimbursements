"""Tests for quarantining unsupported files dropped in the intake folder."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    intake     = tmp_path / "intake"
    out        = tmp_path / "out"
    images     = out / "receipts"
    processing = out / "processing"
    rejected   = out / "unsupported"
    for d in (intake, images, processing):
        d.mkdir(parents=True)

    monkeypatch.setattr(server, "INTAKE_FOLDER", intake)
    monkeypatch.setattr(server, "OUT_FOLDER", out)
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(server, "PROCESSING_FOLDER", processing)
    monkeypatch.setattr(server, "REJECTED_FOLDER", rejected)
    monkeypatch.setattr(server, "STATE_FILE", out / ".app_state.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)

    server._results.clear()
    server._kanban.clear()
    server._work_queue.clear()
    server._item_cache.clear()
    server._seen_intake.clear()
    server._rejected_reasons.clear()
    with TestClient(server.app) as c:
        yield c, intake, rejected
    server._results.clear()
    server._kanban.clear()
    server._work_queue.clear()
    server._item_cache.clear()
    server._seen_intake.clear()
    server._rejected_reasons.clear()


def test_reject_intake_file_moves_and_records(env):
    client, intake, rejected = env
    bad = intake / "notes.txt"
    bad.write_bytes(b"hello")

    item = server._reject_intake_file(bad, "Unsupported file type '.txt'")
    assert item is not None
    assert not bad.exists()                       # moved out of intake
    moved = rejected / "notes.txt"
    assert moved.exists()
    assert item["original_name"] == "notes.txt"
    assert item["ext"] == ".txt"
    assert server._rejected_reasons["notes.txt"].startswith("Unsupported")


def test_reject_handles_name_collision(env):
    client, intake, rejected = env
    (rejected).mkdir(parents=True, exist_ok=True)
    (rejected / "dup.txt").write_bytes(b"first")
    bad = intake / "dup.txt"
    bad.write_bytes(b"second")

    item = server._reject_intake_file(bad, "Unsupported")
    assert item is not None
    assert item["name"] != "dup.txt"              # collision-safe rename
    assert (rejected / "dup.txt").read_bytes() == b"first"   # original preserved


def test_rejected_endpoint_lists_items(env):
    client, intake, rejected = env
    (intake / "a.zip").write_bytes(b"x" * 7)
    server._reject_intake_file(intake / "a.zip", "Unsupported file type '.zip'")

    d = client.get("/intake/rejected").json()
    assert d["ok"] is True and d["count"] == 1
    it = d["items"][0]
    assert it["name"] == "a.zip"
    assert it["size"] == 7
    assert "Unsupported" in it["reason"]


def test_delete_rejected_removes_file(env):
    client, intake, rejected = env
    (intake / "b.exe").write_bytes(b"x")
    server._reject_intake_file(intake / "b.exe", "Unsupported")
    assert (rejected / "b.exe").exists()

    d = client.post("/intake/rejected/delete", json={"name": "b.exe"}).json()
    assert d["ok"] is True and d["deleted"] == "b.exe"
    assert not (rejected / "b.exe").exists()
    assert "b.exe" not in server._rejected_reasons


def test_delete_rejected_rejects_path_traversal(env):
    client, intake, rejected = env
    secret = rejected.parent / "secret.txt"
    secret.write_bytes(b"top")
    d = client.post("/intake/rejected/delete", json={"name": "../secret.txt"})
    assert d.status_code == 400
    assert secret.exists()                        # never escapes the quarantine folder


def test_delete_all_rejected(env):
    client, intake, rejected = env
    for n in ("x.txt", "y.bin", "z.csv"):
        (intake / n).write_bytes(b"x")
        server._reject_intake_file(intake / n, "Unsupported")
    d = client.post("/intake/rejected/delete-all").json()
    assert d["ok"] is True and d["count"] == 3
    assert client.get("/intake/rejected").json()["count"] == 0


def test_queue_add_intake_quarantines_unsupported(env):
    client, intake, rejected = env
    (intake / "doc.docx").write_bytes(b"x")       # unsupported → quarantined
    d = client.post("/queue/add-intake", data={"employee": "E"}).json()
    assert d["queued"] == []
    assert any(r["original_name"] == "doc.docx" for r in d["rejected"])
    assert (rejected / "doc.docx").exists()
    assert not (intake / "doc.docx").exists()
