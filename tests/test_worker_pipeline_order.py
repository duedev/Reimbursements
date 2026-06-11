"""Regression tests for the worker's autocrop → compress → extraction order and
the resized-file path handoff (the '[Errno 2] No such file or directory' bug)."""
from pathlib import Path

import pytest
from PIL import Image

import server
import process_receipts as pr


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    images = tmp_path / "receipts"
    images.mkdir()
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(pr, "COMPRESS_ENABLED", True)
    monkeypatch.setattr(pr, "AUTOCROP_ENABLED", True)
    monkeypatch.setattr(pr, "JPEG_QUALITY", 85)
    monkeypatch.setattr(pr, "STORE_MAX_PX", 2000)
    server._worker_cancel.clear()   # another test may have left this set
    server._work_queue.clear()
    server._results.clear()
    server._kanban.clear()
    yield images
    server._work_queue.clear()
    server._results.clear()
    server._kanban.clear()


def test_png_upload_compresses_before_extraction_no_filenotfound(worker_env, monkeypatch):
    """A .png upload is compressed to .jpg before extraction; the worker must hand
    the resized .jpg path to extraction, not the unlinked original .png."""
    images = worker_env
    tmp_dir = images / "_upload_deadbeef"
    tmp_dir.mkdir()
    src = tmp_dir / "IMG_0406.png"
    Image.new("RGB", (1200, 1600), (90, 90, 90)).save(src, format="PNG")

    seen_paths: list[Path] = []

    def fake_extract(client, path, cb):
        # Whatever path the worker hands us MUST exist on disk.
        seen_paths.append(Path(path))
        assert Path(path).exists(), f"extraction got a missing file: {path}"
        return {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}

    monkeypatch.setattr(server, "_extract_receipt_with_status", fake_extract)

    item = {"filename": "IMG_0406.png", "path": str(src),
            "employee": "E", "job_name": "", "job_number": ""}
    server._work_queue.append(item)

    assert server._drain_once() is True

    # Extraction ran against the compressed .jpg, not the original .png
    assert seen_paths and seen_paths[0].suffix == ".jpg"
    # A processed file landed in IMAGES_FOLDER and the result was recorded
    assert len(server._results) == 1
    final = Path(server._results[0]["_image_path"])
    assert final.exists() and final.parent == images
