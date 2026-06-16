"""Tests for the finish-batch tidy-up endpoint (/results/finish).

After a report is downloaded the embedded receipt images are temporary; the
user can clear them or keep a copy in the archive folder. Archived receipts
must never resurface as orphaned files in the maintenance scan.
"""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    intake     = tmp_path / "intake"
    out        = tmp_path / "out"
    images     = out / "receipts"
    processing = out / "processing"
    archive    = out / "archive"
    for d in (intake, images, processing):
        d.mkdir(parents=True)

    monkeypatch.setattr(server, "INTAKE_FOLDER", intake)
    monkeypatch.setattr(server, "OUT_FOLDER", out)
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(server, "PROCESSING_FOLDER", processing)
    monkeypatch.setattr(server, "ARCHIVE_FOLDER", archive)
    monkeypatch.setattr(server, "STATE_FILE", out / ".app_state.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)

    server._results.clear()
    server._kanban.clear()
    with TestClient(server.app) as c:
        yield c, images, processing, archive
    server._results.clear()
    server._kanban.clear()


def _add_result(images):
    img = images / "fuel_shell_2026-05-01.jpg"
    img.write_bytes(b"x" * 12)
    server._results.append({
        "vendor": "Shell", "amount": 45.20, "_file": "r.jpg",
        "_new_filename": img.name, "_image_path": str(img),
    })
    server._kanban[img.name] = {"status": "done", "data": {"_file": img.name}}
    return img


def test_finish_archive_moves_files_and_clears_board(env):
    client, images, processing, archive = env
    img = _add_result(images)

    d = client.post("/results/finish", json={"mode": "archive"}).json()
    assert d["ok"] is True
    assert d["archived"] == 1
    assert not img.exists()                      # moved out of the working folder
    archived = list(archive.rglob("*.jpg"))
    assert len(archived) == 1                     # now lives in the archive
    assert server._results == []                  # board cleared
    assert server._kanban == {}


def test_finish_delete_removes_files_and_clears_board(env):
    client, images, processing, archive = env
    img = _add_result(images)

    d = client.post("/results/finish", json={"mode": "delete"}).json()
    assert d["ok"] is True
    assert d["deleted"] == 1
    assert not img.exists()
    assert not archive.exists() or not list(archive.rglob("*.jpg"))
    assert server._results == []


def test_archived_files_are_not_reported_as_orphans(env):
    client, images, processing, archive = env
    _add_result(images)
    client.post("/results/finish", json={"mode": "archive"})

    # The archive lives outside the scanned working folders, so the now-cleared
    # results don't make the kept receipts look orphaned.
    d = client.get("/maintenance/orphans").json()
    assert d["ok"] is True
    assert d["count"] == 0
    assert not any("archive" in o["folder"] for o in d["orphans"])


def test_finish_rejects_bad_mode(env):
    client, images, processing, archive = env
    _add_result(images)
    res = client.post("/results/finish", json={"mode": "nuke"})
    assert res.status_code == 400


def test_finish_archive_handles_name_collision(env):
    client, images, processing, archive = env
    # Two results that would land on the same archive filename.
    for sub in ("Processed_a", "Processed_b"):
        d = images / sub
        d.mkdir()
        img = d / "dupe.jpg"
        img.write_bytes(b"x")
        server._results.append({"_new_filename": "dupe.jpg", "_image_path": str(img)})

    out = client.post("/results/finish", json={"mode": "archive"}).json()
    assert out["archived"] == 2
    assert len(list(archive.rglob("dupe*.jpg"))) == 2     # both preserved, unique names
