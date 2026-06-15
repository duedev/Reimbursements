"""Tests for the /debug/autocrop-test preview endpoint."""
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import process_receipts as _pr
import server


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    with TestClient(server.app) as c:
        yield c


def _jpeg(size=(1000, 1000), box=(200, 150, 800, 900), bg=(255, 255, 255), fg=(60, 60, 60)):
    img = Image.new("RGB", size, bg)
    if box:
        img.paste(Image.new("RGB", (box[2] - box[0], box[3] - box[1]), fg), box[:2])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _post(client, content, name="r.jpg"):
    return client.post("/debug/autocrop-test",
                       files=[("files", (name, content, "image/jpeg"))])


def test_autocrop_test_crops_bordered_receipt(client):
    r = _post(client, _jpeg())
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["cropped"] is True and d["would_crop"] is True
    assert d["original"] == [1000, 1000]
    assert d["result"][0] < 1000 and d["result"][1] < 1000
    assert 0.40 <= d["kept_ratio"] <= 0.95
    assert d["reason"]
    assert d["preview"].startswith("data:image/jpeg;base64,")


def test_autocrop_test_leaves_solid_image_unchanged(client):
    r = _post(client, _jpeg(box=None))
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["cropped"] is False
    assert d["result"] == d["original"]


def test_autocrop_test_preview_only_when_disabled(client, monkeypatch):
    # When auto-crop is turned off, the preview still shows what *would* happen,
    # but reports it wasn't actually applied.
    monkeypatch.setattr(_pr, "AUTOCROP_ENABLED", False)
    d = _post(client, _jpeg()).json()
    assert d["enabled"] is False
    assert d["would_crop"] is True      # detection still runs
    assert d["cropped"] is False        # ...but nothing was applied


def test_autocrop_test_rejects_empty_file(client):
    r = _post(client, b"")
    assert r.status_code == 400


def test_autocrop_test_handles_non_image(client):
    r = _post(client, b"this is not an image", name="notes.txt")
    assert r.status_code == 500
    assert r.json()["ok"] is False
