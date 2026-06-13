"""Tests for the orphaned-file maintenance check."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def env(tmp_path, monkeypatch):
    intake     = tmp_path / "intake"
    out        = tmp_path / "out"
    images     = out / "receipts"
    processing = out / "processing"
    for d in (intake, images, processing):
        d.mkdir(parents=True)

    monkeypatch.setattr(server, "INTAKE_FOLDER", intake)
    monkeypatch.setattr(server, "OUT_FOLDER", out)
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(server, "PROCESSING_FOLDER", processing)
    monkeypatch.setattr(server, "STATE_FILE", out / ".app_state.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)

    server._results.clear()
    server._kanban.clear()
    server._work_queue.clear()
    server._item_cache.clear()
    with TestClient(server.app) as c:
        yield c, intake, images, processing
    server._results.clear()
    server._kanban.clear()
    server._work_queue.clear()
    server._item_cache.clear()


def test_orphans_reported_and_referenced_files_skipped(env):
    client, intake, images, processing = env
    (images / "fuel_shell_2026-05-01.jpg").write_bytes(b"x" * 10)
    (images / "leftover.jpg").write_bytes(b"x" * 20)
    (processing / "stuck.png").write_bytes(b"x" * 30)
    server._results.append({
        "vendor": "Shell", "amount": 45.20,
        "_file": "r.jpg",
        "_new_filename": "fuel_shell_2026-05-01.jpg",
        "_image_path": str(images / "fuel_shell_2026-05-01.jpg"),
    })

    d = client.get("/maintenance/orphans").json()
    assert d["ok"] is True
    names = {(o["folder"], o["name"]) for o in d["orphans"]}
    assert ("receipts", "leftover.jpg") in names
    assert ("processing", "stuck.png") in names
    assert not any(o["name"] == "fuel_shell_2026-05-01.jpg" for o in d["orphans"])
    assert d["count"] == 2
    assert d["total_size"] == 50


def test_extension_change_still_counts_as_referenced(env):
    client, intake, images, processing = env
    # Original was photo.png; compression rewrote it as photo.jpg
    (processing / "photo.jpg").write_bytes(b"x")
    server._kanban["photo.png"] = {"status": "failed", "data": {"_file": "photo.png"}}

    d = client.get("/maintenance/orphans").json()
    assert d["count"] == 0


def test_queued_and_cached_items_are_referenced(env):
    client, intake, images, processing = env
    (processing / "queued.jpg").write_bytes(b"x")
    (processing / "cached.jpg").write_bytes(b"x")
    server._work_queue.append({"filename": "queued.jpg", "path": str(processing / "queued.jpg")})
    server._item_cache["cached.jpg"] = {"path": str(processing / "cached.jpg")}

    d = client.get("/maintenance/orphans").json()
    assert d["count"] == 0


def test_stale_pdf_page_dirs_scanned_and_empty_dirs_reported(env):
    client, intake, images, processing = env
    pdf_dir = intake / "_pdf_scan"
    pdf_dir.mkdir()
    (pdf_dir / "scan_page1.jpg").write_bytes(b"x")
    empty = intake / "_pdf_old"
    empty.mkdir()
    # Plain intake files are pending input — never orphans
    (intake / "tomorrow.jpg").write_bytes(b"x")

    d = client.get("/maintenance/orphans").json()
    assert any(o["folder"] == "intake/_pdf_scan" and o["name"] == "scan_page1.jpg"
               for o in d["orphans"])
    assert "intake/_pdf_old" in d["empty_dirs"]
    assert not any(o["name"] == "tomorrow.jpg" for o in d["orphans"])


def test_archived_pdf_referenced_through_its_pages(env):
    client, intake, images, processing = env
    # Original PDF parked in images folder; its converted page is a live result
    (images / "invoice.pdf").write_bytes(b"x")
    (images / "misc_acme_2026-05-02.jpg").write_bytes(b"x")
    server._results.append({
        "_file": "invoice_page1.jpg",
        "_new_filename": "misc_acme_2026-05-02.jpg",
    })

    d = client.get("/maintenance/orphans").json()
    assert d["count"] == 0


def test_orphan_report_includes_full_path(env):
    client, intake, images, processing = env
    (images / "leftover.jpg").write_bytes(b"x" * 5)
    d = client.get("/maintenance/orphans").json()
    o = next(o for o in d["orphans"] if o["name"] == "leftover.jpg")
    assert o["path"] == str((images / "leftover.jpg").resolve())


def test_delete_orphans_removes_only_orphaned_files(env):
    client, intake, images, processing = env
    keep = images / "fuel_shell_2026-05-01.jpg"
    keep.write_bytes(b"x" * 10)
    orphan1 = images / "leftover.jpg"
    orphan1.write_bytes(b"x" * 20)
    orphan2 = processing / "stuck.png"
    orphan2.write_bytes(b"x" * 30)
    server._results.append({
        "vendor": "Shell", "amount": 45.20, "_file": "r.jpg",
        "_new_filename": "fuel_shell_2026-05-01.jpg",
        "_image_path": str(keep),
    })

    d = client.post("/maintenance/delete-orphans").json()
    assert d["ok"] is True
    assert d["count"] == 2
    assert d["freed"] == 50
    assert d["errors"] == []
    assert not orphan1.exists() and not orphan2.exists()
    assert keep.exists()   # referenced file is never deleted

    # A follow-up scan now finds nothing
    assert client.get("/maintenance/orphans").json()["count"] == 0


def test_delete_orphans_noop_when_clean(env):
    client, intake, images, processing = env
    (images / "fuel_shell_2026-05-01.jpg").write_bytes(b"x")
    server._results.append({
        "_new_filename": "fuel_shell_2026-05-01.jpg", "_file": "r.jpg",
    })
    d = client.post("/maintenance/delete-orphans").json()
    assert d["ok"] is True and d["count"] == 0 and d["freed"] == 0


def test_session_start_sweeps_empty_orphaned_dirs(tmp_path, monkeypatch):
    """On startup the lifespan sweep removes empty orphaned folders but leaves
    populated ones (and pending intake) alone."""
    intake     = tmp_path / "intake"
    out        = tmp_path / "out"
    images     = out / "receipts"
    processing = out / "processing"
    for d in (intake, images, processing):
        d.mkdir(parents=True)

    # Empty leftovers that should be swept the moment the session starts
    (images / "Processed_2026-05-01").mkdir()
    (intake / "_pdf_old").mkdir()
    # Populated folder that must survive
    keep = images / "Processed_2026-06-13"
    keep.mkdir()
    (keep / "fuel_shell.jpg").write_bytes(b"x")

    monkeypatch.setattr(server, "INTAKE_FOLDER", intake)
    monkeypatch.setattr(server, "OUT_FOLDER", out)
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(server, "PROCESSING_FOLDER", processing)
    monkeypatch.setattr(server, "STATE_FILE", out / ".app_state.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)

    # Starting the client runs the lifespan startup, which performs the sweep.
    with TestClient(server.app):
        pass

    assert not (images / "Processed_2026-05-01").exists()
    assert not (intake / "_pdf_old").exists()
    assert keep.exists() and (keep / "fuel_shell.jpg").exists()


def test_cleanup_removes_empty_dirs_keeps_populated(env):
    client, intake, images, processing = env
    # Empty job folder + nested empties under receipts — should be removed
    (images / "JB-1-old").mkdir()
    (images / "nested" / "deep").mkdir(parents=True)
    # Empty temp dirs in intake — removed
    (intake / "_pdf_done").mkdir()
    (intake / "_upload_stale").mkdir()
    # Populated folder — preserved
    keep = images / "JB-2-active"
    keep.mkdir()
    (keep / "receipt.jpg").write_bytes(b"x")
    # Pending input dir that isn't a temp folder — left alone
    (intake / "RealJobFolder").mkdir()

    d = client.post("/maintenance/cleanup-empty-dirs").json()
    assert d["ok"] is True
    removed_paths = {r["path"] for r in d["removed"]}

    assert str((images / "JB-1-old").resolve()) in removed_paths
    assert str((images / "nested").resolve()) in removed_paths
    assert str((intake / "_pdf_done").resolve()) in removed_paths
    assert str((intake / "_upload_stale").resolve()) in removed_paths

    assert keep.exists() and (keep / "receipt.jpg").exists()
    assert (intake / "RealJobFolder").exists()   # non-temp intake dir untouched
