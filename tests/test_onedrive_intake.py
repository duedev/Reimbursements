"""Microsoft OneDrive receipt intake — folder listing/download, item-ID dedup,
the device-code OAuth helpers, the settings + connect/disconnect endpoints, and
token storage (including Microsoft's refresh-token ROTATION).

No live network — the Graph client is faked and the token/HTTP helpers are
monkeypatched (the same way the OCR/LLM stack and the Drive service are mocked
elsewhere). The module uses only stdlib urllib, so nothing extra is installed.
"""
import json

import pytest
from fastapi.testclient import TestClient

import app_secrets
import onedrive_intake as od
import server


# ── A fake GraphClient (poll_once/_list_folder only call .get_json) ──────────────

class _FakeGraph:
    def __init__(self, pages):
        """`pages` is a list of children-listing response dicts; each get_json call
        returns the next one (mirroring @odata.nextLink paging)."""
        self._pages = list(pages)
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        return self._pages.pop(0) if self._pages else {"value": []}


def _items():
    return [
        {"id": "a", "name": "r1.jpg", "file": {"mimeType": "image/jpeg"}},
        {"id": "b", "name": "r2.pdf", "file": {"mimeType": "application/pdf"}},
        {"id": "c", "name": "notes.txt", "file": {"mimeType": "text/plain"}},  # not image/pdf
        {"id": "d", "name": "Subfolder", "folder": {"childCount": 1}},         # no file facet
    ]


def _graph():
    return _FakeGraph([{"value": _items()}])


# ── Config ────────────────────────────────────────────────────────────────────

def test_config_roundtrip_hides_secrets():
    cfg = od.OneDriveConfig.from_dict({
        "enabled": True, "folder_path": " /Receipts/ ", "poll_interval": "5",
        "scope": "files.readwrite", "client_id": "cid-1", "tenant": "common",
    })
    assert cfg.enabled and cfg.folder_path == "Receipts" and cfg.client_id == "cid-1"
    assert cfg.poll_interval == 30            # clamped to the 30s floor
    assert cfg.scope == "files.readwrite"
    assert cfg.scope_string() == "Files.ReadWrite offline_access"
    pub = cfg.to_public_dict()
    assert "client_secret" not in pub and "token" not in pub
    # Unknown scope falls back to read-only; bad tenant chars are stripped.
    assert od.OneDriveConfig.from_dict({"scope": "bogus"}).scope == "files.read"
    assert od.OneDriveConfig.from_dict({"tenant": "evil/../path"}).tenant == "evil..path"
    assert od.OneDriveConfig.from_dict({"tenant": "///"}).tenant == "consumers"


def test_folder_children_url_quotes_path():
    assert od._folder_children_url("Receipts") == "/me/drive/root:/Receipts:/children?$top=100"
    assert "My%20Receipts/2026" in od._folder_children_url(" /My Receipts/2026/ ")


# ── _list_folder: files only, paging ─────────────────────────────────────────────

def test_list_folder_skips_subfolders_and_pages():
    graph = _FakeGraph([
        {"value": _items()[:2], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
        {"value": _items()[2:]},
    ])
    files = od._list_folder(graph, "Receipts")
    assert [f["id"] for f in files] == ["a", "b", "c"]   # folder "d" skipped
    assert graph.calls[1] == "https://graph.microsoft.com/v1.0/next"


def test_list_folder_empty_path_is_noop():
    graph = _graph()
    assert od._list_folder(graph, "") == []
    assert od._list_folder(graph, " / ") == []
    assert graph.calls == []                  # never hit the API without a folder


# ── poll_once: list + download + item-ID dedup ───────────────────────────────────

def test_poll_lists_and_downloads_new_files(tmp_path, monkeypatch):
    monkeypatch.setattr(od, "_download_media",
                        lambda graph, item: b"DATA-" + item["id"].encode())
    cfg = od.OneDriveConfig(enabled=True, folder_path="Receipts")
    intake = tmp_path / "intake"
    seen: set[str] = set()
    summary = od.poll_once(_graph(), cfg, intake, already_seen=seen)

    assert summary["files"] == 3 and summary["downloaded"] == 2 and summary["skipped"] == 1
    assert (intake / "r1.jpg").exists() and (intake / "r2.pdf").exists()
    assert (intake / "r1.jpg").read_bytes() == b"DATA-a"
    # The non-image/PDF is recorded as seen too, so it isn't re-evaluated forever.
    assert set(summary["seen_ids"]) == {"a", "b", "c"}
    assert seen == {"a", "b", "c"}


def test_poll_dedup_by_item_id_skips_seen(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(od, "_download_media",
                        lambda graph, item: calls.append(item["id"]) or b"X")
    cfg = od.OneDriveConfig(enabled=True, folder_path="Receipts")
    intake = tmp_path / "intake"
    summary = od.poll_once(_graph(), cfg, intake, already_seen={"a", "b", "c"})
    assert summary["downloaded"] == 0 and summary["skipped"] == 3
    assert calls == []                        # nothing re-downloaded
    assert not intake.exists() or not list(intake.iterdir())


def test_poll_basename_only_no_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(od, "_download_media", lambda graph, item: b"X")
    graph = _FakeGraph([{"value": [
        {"id": "z", "name": "../../evil.png", "file": {"mimeType": "image/png"}},
    ]}])
    cfg = od.OneDriveConfig(enabled=True, folder_path="Receipts")
    intake = tmp_path / "intake"
    od.poll_once(graph, cfg, intake, already_seen=set())
    written = list(intake.iterdir())
    assert len(written) == 1
    assert written[0].parent == intake        # stayed inside the intake folder
    assert ".." not in written[0].name


def test_download_prefers_preauthenticated_url():
    got = {}

    class _G:
        def get_bytes(self, url, auth=True):
            got["url"], got["auth"] = url, auth
            return b"B"

    item = {"id": "i1", "@microsoft.graph.downloadUrl": "https://cdn.example/x"}
    assert od._download_media(_G(), item) == b"B"
    assert got == {"url": "https://cdn.example/x", "auth": False}
    # Without the annotation it falls back to the authenticated /content endpoint.
    od._download_media(_G(), {"id": "i1"})
    assert got["url"].endswith("/me/drive/items/i1/content") and got["auth"] is True


# ── Device-code flow classification (no network — _form_post faked) ──────────────

def test_device_code_poll_states(monkeypatch):
    cfg = od.OneDriveConfig(client_id="cid")
    monkeypatch.setattr(od, "_form_post",
                        lambda url, fields: (400, {"error": "authorization_pending"}))
    r = od.device_code_poll(cfg, "dc")
    assert not r["ok"] and r["pending"] is True

    monkeypatch.setattr(od, "_form_post",
                        lambda url, fields: (200, {"refresh_token": "rt", "access_token": "at"}))
    r = od.device_code_poll(cfg, "dc")
    assert r["ok"] and r["refresh_token"] == "rt"

    monkeypatch.setattr(od, "_form_post",
                        lambda url, fields: (400, {"error": "authorization_declined",
                                                   "error_description": "User declined."}))
    r = od.device_code_poll(cfg, "dc")
    assert not r["ok"] and r["pending"] is False and "declined" in r["error"]


def test_device_code_start_requires_client_id():
    assert od.device_code_start(od.OneDriveConfig())["ok"] is False


def test_redeem_returns_rotated_token(monkeypatch):
    sent = {}

    def fake_post(url, fields):
        sent.update(fields)
        return 200, {"access_token": "at", "refresh_token": "rt-NEW"}

    monkeypatch.setattr(od, "_form_post", fake_post)
    cfg = od.OneDriveConfig(client_id="cid", tenant="consumers")
    r = od.redeem_refresh_token(cfg, "rt-old")
    assert r == {"ok": True, "access_token": "at", "refresh_token": "rt-NEW"}
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "rt-old"
    assert "client_secret" not in sent        # public client: no secret sent


def test_build_graph_unconfigured_and_failure(monkeypatch):
    assert od.build_graph(od.OneDriveConfig(), "") == (None, "")
    monkeypatch.setattr(od, "redeem_refresh_token",
                        lambda cfg, rt, cs="": {"ok": False, "error": "expired"})
    with pytest.raises(RuntimeError):
        od.build_graph(od.OneDriveConfig(client_id="cid"), "rt")


# ── Token round-trips + rotation persistence through app_secrets ─────────────────

def test_token_roundtrips_through_secrets():
    app_secrets.save_secret("onedrive_token", "rt-abc-123")
    assert server._onedrive_token() == "rt-abc-123"
    app_secrets.save_secret("onedrive_token", "")          # clear
    assert server._onedrive_token() == ""


def test_build_graph_persists_rotated_token(monkeypatch):
    c = TestClient(server.app)
    c.post("/settings/onedrive", json={"enabled": True, "folder_path": "Receipts",
                                       "client_id": "cid"})
    app_secrets.save_secret("onedrive_token", "rt-old")
    monkeypatch.setattr(od, "redeem_refresh_token",
                        lambda cfg, rt, cs="": {"ok": True, "access_token": "at",
                                                "refresh_token": "rt-rotated"})
    graph = server._build_onedrive_graph()
    assert isinstance(graph, od.GraphClient)
    # Microsoft rotates refresh tokens — the replacement must be what's stored now.
    assert server._onedrive_token() == "rt-rotated"


# ── Settings + connect/disconnect endpoints ──────────────────────────────────────

def test_onedrive_settings_roundtrip():
    c = TestClient(server.app)
    r = c.post("/settings/onedrive", json={
        "enabled": True, "folder_path": "/Receipts/", "poll_interval": 600,
        "scope": "files.read", "client_id": "app-guid-1", "tenant": "consumers",
        "client_secret": "shh-secret",
    })
    assert r.status_code == 200 and r.json()["ok"]
    got = c.get("/settings/onedrive").json()
    assert got["folder_path"] == "Receipts" and got["poll_interval"] == 600
    assert got["client_id"] == "app-guid-1" and got["tenant"] == "consumers"
    assert got["client_secret_set"] is True
    assert "client_secret" not in got and "token" not in got
    assert got["connected"] is False          # no token yet


def test_onedrive_connect_with_refresh_token_then_disconnect():
    c = TestClient(server.app)
    c.post("/settings/onedrive", json={"enabled": True, "folder_path": "R",
                                       "client_id": "cid"})
    r = c.post("/settings/onedrive/connect", json={"refresh_token": "rt-xyz"})
    assert r.status_code == 200 and r.json()["connected"] is True
    assert server._onedrive_token() == "rt-xyz"
    assert c.get("/settings/onedrive").json()["connected"] is True

    r2 = c.post("/settings/onedrive/disconnect")
    assert r2.status_code == 200 and r2.json()["connected"] is False
    assert server._onedrive_token() == ""


def test_onedrive_connect_device_code_pending_then_done(monkeypatch):
    c = TestClient(server.app)
    c.post("/settings/onedrive", json={"client_id": "cid"})
    monkeypatch.setattr(od, "device_code_poll",
                        lambda cfg, dc: {"ok": False, "pending": True,
                                         "error": "authorization_pending"})
    r = c.post("/settings/onedrive/connect", json={"device_code": "dc-1"})
    assert r.status_code == 200                # pending is not an error (UI keeps polling)
    assert r.json() == {"ok": False, "pending": True, "error": "authorization_pending"}

    monkeypatch.setattr(od, "device_code_poll",
                        lambda cfg, dc: {"ok": True, "refresh_token": "rt-dc"})
    r = c.post("/settings/onedrive/connect", json={"device_code": "dc-1"})
    assert r.status_code == 200 and r.json()["connected"] is True
    assert server._onedrive_token() == "rt-dc"


def test_onedrive_connect_requires_code_or_token():
    c = TestClient(server.app)
    assert c.post("/settings/onedrive/connect", json={}).status_code == 400


def test_pollnow_requires_config():
    c = TestClient(server.app)
    assert c.post("/settings/onedrive/poll-now").status_code == 400


def test_disabled_or_unconnected_poll_is_noop():
    # With no token the Graph client can't be built → _onedrive_poll is a no-op.
    app_secrets.save_secret("onedrive_token", "")
    cfg = od.OneDriveConfig(enabled=False, folder_path="Receipts")
    summary = server._onedrive_poll(cfg, set())
    assert summary["downloaded"] == 0 and summary.get("error") == "not connected"


def test_pollnow_downloads_via_fake_graph(tmp_path, monkeypatch):
    # End-to-end through the endpoint with a faked Graph client.
    monkeypatch.setattr(server._default_ws, "out_folder", tmp_path)
    monkeypatch.setattr(server._default_ws, "intake_folder", tmp_path / "intake")
    monkeypatch.setattr(od, "_download_media",
                        lambda graph, item: b"IMG-" + item["id"].encode())
    monkeypatch.setattr(server, "_build_onedrive_graph", lambda: _graph())
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    app_secrets.save_secret("onedrive_token", "rt")
    c = TestClient(server.app)
    c.post("/settings/onedrive", json={"enabled": True, "folder_path": "Receipts",
                                       "client_id": "cid"})
    r = c.post("/settings/onedrive/poll-now")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["downloaded"] == 2
    assert (tmp_path / "intake" / "r1.jpg").exists()
    # Seen ids persisted so a second poll dedups.
    seen = json.loads((tmp_path / ".onedrive_seen.json").read_text())
    assert set(seen) >= {"a", "b"}
