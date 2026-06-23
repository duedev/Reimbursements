"""Google Drive receipt intake — folder listing/download, file-ID dedup, the
settings + connect/disconnect endpoints, and token storage.

No live Drive — the Drive `service` is faked, and the byte download is monkeypatched
(the same way the OCR/LLM stack is mocked elsewhere). The Google client libraries
are NOT installed in the test env; the module imports fine without them because they
are lazy-imported only inside the OAuth/download helpers.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app_secrets
import gdrive_intake as gd
import server


# ── A fake Drive service (mirrors the googleapiclient Resource shape) ─────────────

class _FakeExec:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeFiles:
    def __init__(self, files):
        self._files = files
        self.list_calls = []
        self.media_calls = []

    def list(self, **kw):
        self.list_calls.append(kw)
        return _FakeExec({"files": list(self._files)})   # single page

    def get_media(self, fileId=None):
        self.media_calls.append(fileId)
        return ("media", fileId)


class _FakeService:
    def __init__(self, files):
        self._files = _FakeFiles(files)

    def files(self):
        return self._files


# ── Config ────────────────────────────────────────────────────────────────────

def test_config_roundtrip_hides_secrets():
    cfg = gd.GDriveConfig.from_dict({
        "enabled": True, "folder_id": "  F123  ", "poll_interval": "5",
        "scope": "drive.file", "client_id": "cid", "move_processed": True,
    })
    assert cfg.enabled and cfg.folder_id == "F123" and cfg.client_id == "cid"
    assert cfg.poll_interval == 30          # clamped to the 30s floor
    assert cfg.scope == "drive.file" and cfg.scope_url().endswith("drive.file")
    pub = cfg.to_public_dict()
    assert "client_secret" not in pub and "token" not in pub
    # An unknown scope falls back to read-only.
    assert gd.GDriveConfig.from_dict({"scope": "bogus"}).scope == "drive.readonly"


# ── poll_once: list + download + file-ID dedup ───────────────────────────────────

def _files():
    return [
        {"id": "a", "name": "r1.jpg", "mimeType": "image/jpeg"},
        {"id": "b", "name": "r2.pdf", "mimeType": "application/pdf"},
        {"id": "c", "name": "notes.txt", "mimeType": "text/plain"},   # not image/pdf
    ]


def test_poll_lists_and_downloads_new_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gd, "_download_media", lambda service, fid: b"DATA-" + fid.encode())
    service = _FakeService(_files())
    cfg = gd.GDriveConfig(enabled=True, folder_id="F")
    intake = tmp_path / "intake"
    seen: set[str] = set()
    summary = gd.poll_once(service, cfg, intake, already_seen=seen)

    assert summary["files"] == 3 and summary["downloaded"] == 2 and summary["skipped"] == 1
    assert (intake / "r1.jpg").exists() and (intake / "r2.pdf").exists()
    assert (intake / "r1.jpg").read_bytes() == b"DATA-a"
    # The non-image/PDF is recorded as seen too, so it isn't re-evaluated forever.
    assert set(summary["seen_ids"]) == {"a", "b", "c"}
    assert seen == {"a", "b", "c"}


def test_poll_dedup_by_file_id_skips_seen(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gd, "_download_media",
                        lambda service, fid: calls.append(fid) or b"X")
    service = _FakeService(_files())
    cfg = gd.GDriveConfig(enabled=True, folder_id="F")
    intake = tmp_path / "intake"
    summary = gd.poll_once(service, cfg, intake, already_seen={"a", "b", "c"})
    assert summary["downloaded"] == 0 and summary["skipped"] == 3
    assert calls == []                       # nothing re-downloaded
    assert not intake.exists() or not list(intake.iterdir())


def test_poll_basename_only_no_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(gd, "_download_media", lambda service, fid: b"X")
    service = _FakeService([{"id": "z", "name": "../../evil.png", "mimeType": "image/png"}])
    cfg = gd.GDriveConfig(enabled=True, folder_id="F")
    intake = tmp_path / "intake"
    gd.poll_once(service, cfg, intake, already_seen=set())
    written = list(intake.iterdir())
    assert len(written) == 1
    assert written[0].parent == intake           # stayed inside the intake folder
    assert ".." not in written[0].name


def test_poll_empty_folder_id_is_noop(tmp_path):
    service = _FakeService(_files())
    summary = gd.poll_once(service, gd.GDriveConfig(folder_id=""), tmp_path, already_seen=set())
    assert summary["files"] == 0 and summary["downloaded"] == 0


# ── Token round-trips through app_secrets ────────────────────────────────────────

def test_token_roundtrips_through_secrets():
    app_secrets.save_secret("gdrive_token", "rt-abc-123")
    assert server._gdrive_token() == "rt-abc-123"
    app_secrets.save_secret("gdrive_token", "")          # clear
    assert server._gdrive_token() == ""


# ── Settings + connect/disconnect endpoints ──────────────────────────────────────

def test_gdrive_settings_roundtrip():
    c = TestClient(server.app)
    r = c.post("/settings/gdrive", json={
        "enabled": True, "folder_id": "FOLDER-1", "poll_interval": 600,
        "scope": "drive.readonly", "client_id": "client-1.apps.googleusercontent.com",
        "client_secret": "shh-secret",
    })
    assert r.status_code == 200 and r.json()["ok"]
    got = c.get("/settings/gdrive").json()
    assert got["folder_id"] == "FOLDER-1" and got["poll_interval"] == 600
    assert got["client_id"] == "client-1.apps.googleusercontent.com"
    assert got["client_secret_set"] is True
    assert "client_secret" not in got and "token" not in got
    assert got["connected"] is False        # no token yet


def test_gdrive_connect_with_refresh_token_then_disconnect():
    c = TestClient(server.app)
    c.post("/settings/gdrive", json={
        "enabled": True, "folder_id": "F", "client_id": "cid", "client_secret": "sec",
    })
    r = c.post("/settings/gdrive/connect", json={"refresh_token": "rt-xyz"})
    assert r.status_code == 200 and r.json()["connected"] is True
    assert server._gdrive_token() == "rt-xyz"
    assert c.get("/settings/gdrive").json()["connected"] is True

    # Disconnect clears the token locally even if the network revoke is unreachable.
    r2 = c.post("/settings/gdrive/disconnect")
    assert r2.status_code == 200 and r2.json()["connected"] is False
    assert server._gdrive_token() == ""


def test_gdrive_connect_requires_code_or_token():
    c = TestClient(server.app)
    assert c.post("/settings/gdrive/connect", json={}).status_code == 400


def test_pollnow_requires_config():
    c = TestClient(server.app)
    # Nothing configured / not connected → 400, a clean no-op.
    assert c.post("/settings/gdrive/poll-now").status_code == 400


def test_disabled_or_unconnected_poll_is_noop(monkeypatch):
    # With no token the service can't be built → _gdrive_poll is a no-op.
    app_secrets.save_secret("gdrive_token", "")
    cfg = gd.GDriveConfig(enabled=False, folder_id="F")
    summary = server._gdrive_poll(cfg, set())
    assert summary["downloaded"] == 0 and summary.get("error") == "not connected"


def test_pollnow_downloads_via_fake_service(tmp_path, monkeypatch):
    # End-to-end through the endpoint with a faked Drive service.
    monkeypatch.setattr(server._default_ws, "out_folder", tmp_path)
    monkeypatch.setattr(server._default_ws, "intake_folder", tmp_path / "intake")
    monkeypatch.setattr(gd, "_download_media", lambda service, fid: b"IMG-" + fid.encode())
    monkeypatch.setattr(server, "_build_gdrive_service", lambda: _FakeService(_files()))
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    app_secrets.save_secret("gdrive_token", "rt")
    c = TestClient(server.app)
    c.post("/settings/gdrive", json={"enabled": True, "folder_id": "F", "client_id": "cid"})
    r = c.post("/settings/gdrive/poll-now")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["downloaded"] == 2
    assert (tmp_path / "intake" / "r1.jpg").exists()
    # Seen ids persisted so a second poll dedups.
    seen = json.loads((tmp_path / ".gdrive_seen.json").read_text())
    assert set(seen) >= {"a", "b"}
