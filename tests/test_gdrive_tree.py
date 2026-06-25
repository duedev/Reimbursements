"""Auto-provisioned Google Drive tree + report-bundle upload.

The Drive `service` is faked (no Google libs, no network). The fake supports the
find-or-create folder flow (`files().list` + `files().create`) and media uploads
that `upload_file` performs. `_media_file` is monkeypatched so no real
MediaFileUpload / Google client is needed.
"""
from pathlib import Path

import pytest

import gdrive_intake as gd


class _Exec:
    def __init__(self, resp): self._resp = resp
    def execute(self): return self._resp


class _Files:
    """Tracks created folders/files; serves find-or-create list() queries by name."""
    def __init__(self):
        self.items = {}          # id -> {name, mimeType, parents}
        self._seq = 0
        self.uploads = []        # (name, parent, media)

    def _new_id(self, prefix):
        self._seq += 1
        return f"{prefix}{self._seq}"

    def list(self, q="", **kw):
        # Parse "name = 'X'" and optional "'<parent>' in parents" out of the query.
        import re
        name = None
        m = re.search(r"name = '([^']*)'", q)
        if m: name = m.group(1).replace("\\'", "'")
        parent = None
        m = re.search(r"'([^']+)' in parents", q)
        if m: parent = m.group(1)
        is_folder = gd._FOLDER_MIME in q
        out = []
        for fid, it in self.items.items():
            if name is not None and it["name"] != name:
                continue
            if parent is not None and parent not in it.get("parents", []):
                continue
            if is_folder and it["mimeType"] != gd._FOLDER_MIME:
                continue
            out.append({"id": fid, "name": it["name"]})
        return _Exec({"files": out})

    def create(self, body=None, media_body=None, fields=None, **kw):
        body = body or {}
        is_folder = body.get("mimeType") == gd._FOLDER_MIME
        fid = self._new_id("fold" if is_folder else "file")
        self.items[fid] = {
            "name": body.get("name", ""),
            "mimeType": body.get("mimeType", "application/octet-stream"),
            "parents": body.get("parents", []),
        }
        if media_body is not None:
            self.uploads.append((body.get("name"), body.get("parents", [None])[0], media_body))
        return _Exec({"id": fid})


class _Service:
    def __init__(self): self._files = _Files()
    def files(self): return self._files


@pytest.fixture(autouse=True)
def _fake_media(monkeypatch):
    # upload_file builds a MediaFileUpload via _media_file — fake it (no Google libs).
    monkeypatch.setattr(gd, "_media_file", lambda path, mime=None: ("MEDIA", str(path)))


def test_ensure_folder_is_idempotent():
    svc = _Service()
    a = gd.ensure_folder(svc, "Receipt App")
    b = gd.ensure_folder(svc, "Receipt App")
    assert a == b                                  # found, not re-created
    # Only one root folder exists.
    folders = [i for i in svc._files.items.values() if i["mimeType"] == gd._FOLDER_MIME]
    assert sum(1 for f in folders if f["name"] == "Receipt App") == 1


def test_provision_tree_creates_intake_and_output():
    svc = _Service()
    tree = gd.provision_tree(svc)
    assert set(tree) == {"root", "intake", "output"}
    items = svc._files.items
    assert items[tree["intake"]]["name"] == "Intake"
    assert items[tree["intake"]]["parents"] == [tree["root"]]
    assert items[tree["output"]]["name"] == "Output"
    # Re-provisioning returns the same ids (idempotent).
    assert gd.provision_tree(svc) == tree


def test_upload_report_bundle_groups_by_date(tmp_path):
    svc = _Service()
    tree = gd.provision_tree(svc)
    wb = tmp_path / "Reimbursements_Alice_2026-06-25.xlsx"; wb.write_text("x")
    r1 = tmp_path / "a.jpg"; r1.write_text("i1")
    r2 = tmp_path / "b.jpg"; r2.write_text("i2")
    missing = tmp_path / "gone.jpg"   # filtered out (doesn't exist)

    summary = gd.upload_report_bundle(svc, tree["output"], "2026-06-25", wb, [r1, r2, missing])

    assert summary["workbook"] is not None
    assert len(summary["receipts"]) == 2
    # A dated folder under Output, and a receipts/ subfolder under that.
    items = svc._files.items
    date_folder = items[summary["date_folder"]]
    assert date_folder["name"] == "2026-06-25" and date_folder["parents"] == [tree["output"]]
    # Workbook uploaded directly into the dated folder.
    up_names = [u[0] for u in svc._files.uploads]
    assert wb.name in up_names and "a.jpg" in up_names and "b.jpg" in up_names
    # receipts/ subfolder exists under the dated folder.
    receipts = [i for i in items.values()
                if i["name"] == "receipts" and i["parents"] == [summary["date_folder"]]]
    assert len(receipts) == 1


def test_upload_report_bundle_handles_no_receipts(tmp_path):
    svc = _Service()
    tree = gd.provision_tree(svc)
    wb = tmp_path / "R.xlsx"; wb.write_text("x")
    summary = gd.upload_report_bundle(svc, tree["output"], "2026-01-01", wb, [])
    assert summary["receipts"] == [] and summary["workbook"] is not None


def test_login_scope_set_excludes_send():
    # The login bridge requests identity + Drive + Gmail receive — never gmail.send.
    assert "gmail.send" not in gd.GOOGLE_LOGIN_SCOPES
    assert set(gd.GOOGLE_LOGIN_SCOPES) >= {"openid", "email", "drive.file", "gmail.readonly"}


# ── Server: provision endpoint + report-upload gating ─────────────────────────

def test_provision_endpoint_sets_tree_and_enables_upload(monkeypatch):
    import app_secrets
    import server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "_build_gdrive_service", lambda: _Service())
    app_secrets.save_secret("gdrive_token", "rt")
    c = TestClient(server.app)
    c.post("/settings/gdrive", json={"enabled": True, "folder_id": "F", "client_id": "cid"})
    r = c.post("/settings/gdrive/provision")
    assert r.status_code == 200 and r.json()["ok"]
    tree = r.json()["tree"]
    assert set(tree) == {"root", "intake", "output"}
    got = c.get("/settings/gdrive").json()
    assert got["provisioned"] is True and got["upload_output"] is True
    # The poller's intake folder is now the provisioned Intake id.
    assert got["folder_id"] == tree["intake"]
    app_secrets.save_secret("gdrive_token", "")


def test_upload_report_noop_when_not_enabled(monkeypatch):
    import server
    # No 'upload_output' in config → helper is a clean no-op (returns None).
    monkeypatch.setattr(server, "_load_config", lambda: {"gdrive": {}})
    assert server._gdrive_upload_report("wb.xlsx", [], "2026-06-25") is None
