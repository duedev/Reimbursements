"""Regression tests for the worker pipeline order.

Compression is DEFERRED to spreadsheet-generation time, so the worker hands the
full-resolution stored file (original suffix) to extraction — never a rewritten
path — and the optimisation happens later, in compress_result_images.
"""
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
    monkeypatch.setattr(pr, "STORE_MAX_PX", 2000)
    server._worker_cancel.clear()   # another test may have left this set
    server._work_queue.clear()
    server._results.clear()
    server._kanban.clear()
    yield images
    server._work_queue.clear()
    server._results.clear()
    server._kanban.clear()


def test_extraction_reads_original_file_then_compresses_at_export(worker_env, monkeypatch):
    """A .png upload is handed to extraction unchanged (it must exist), stored at
    full resolution, and only re-encoded to .jpg when the report is generated."""
    images = worker_env
    tmp_dir = images / "_upload_deadbeef"
    tmp_dir.mkdir()
    src = tmp_dir / "IMG_0406.png"
    Image.new("RGB", (1200, 1600), (90, 90, 90)).save(src, format="PNG")

    seen_paths: list[Path] = []

    def fake_extract(client, path, cb, step_log=None, force_llm_ocr=False):
        # Whatever path the worker hands us MUST exist on disk and keep its suffix.
        seen_paths.append(Path(path))
        assert Path(path).exists(), f"extraction got a missing file: {path}"
        return {"vendor": "Shell", "amount": 45.20, "date": "2026-05-01", "flags": []}

    monkeypatch.setattr(server, "_extract_receipt_with_status", fake_extract)

    item = {"filename": "IMG_0406.png", "path": str(src),
            "employee": "E", "job_name": "", "job_number": ""}
    server._work_queue.append(item)

    assert server._drain_once() is True

    # Extraction ran against the ORIGINAL .png — compression no longer happens first
    assert seen_paths and seen_paths[0].suffix == ".png"

    # A processed (still uncompressed) file landed in a dated subfolder of
    # IMAGES_FOLDER (receipts/Processed_YYYY-MM-DD/…)
    assert len(server._results) == 1
    result = server._results[0]
    stored = Path(result["_image_path"])
    assert stored.exists() and stored.parent.parent == images
    assert stored.parent.name.startswith("Processed_")
    assert stored.suffix == ".png"          # not yet compressed
    assert not result.get("_compressed")

    # Now run the deferred compression (what generate_spreadsheet does at export)
    pr.compress_result_images(server._results)

    compressed = Path(result["_image_path"])
    assert compressed.suffix == ".jpg"      # rewritten to optimized JPEG
    assert compressed.exists() and not stored.exists()
    assert result["_new_filename"] == compressed.name
    assert result["_compressed"] is True

    # Idempotent — a second export does not re-encode
    size_after = compressed.stat().st_size
    pr.compress_result_images(server._results)
    assert compressed.stat().st_size == size_after
