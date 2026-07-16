"""onedrive_intake.py — pull receipts from a Microsoft OneDrive folder into the pipeline.

The OneDrive-as-hub capture path (see `ONEDRIVE_IMPORT.md`): make one OneDrive
folder the app's "receipts inbox," fill it from your phone (the OneDrive app's
built-in **Scan** / share-sheet) or from any synced PC folder, and have the app
poll that folder and download new files into the existing intake — where the
folder watcher and pipeline take over **unchanged**.

This module mirrors `gdrive_intake.py`'s shape:
- **Pure / import-light core.** Listing + the download decision (`poll_once`,
  `_list_folder`, `_safe_name`, `_ext_kind`) are unit-testable with a *fake*
  `GraphClient` — no network. Unlike the Google path there are **no client
  libraries at all**: Microsoft Graph is called with stdlib `urllib`, so nothing
  new lands in requirements.txt.
- **Dedup by Graph item ID** (NOT filename — names collide across sources and
  re-uploads). The caller persists the seen ids (see `server._load_onedrive_seen`).
- **Basename-only writes** into the intake folder (no path traversal).
- **Device-code sign-in.** The one-time consent uses the OAuth 2.0 device
  authorization flow ("go to microsoft.com/devicelogin and enter this code"),
  which needs no redirect URI — it works even when the app runs headless in
  Docker. A refresh token can also be pasted directly (advanced).

GOTCHA — **Microsoft rotates refresh tokens**: every token refresh returns a
replacement refresh token that MUST be persisted (the old one eventually stops
working). `build_graph` returns the rotated token alongside the client so the
caller can save it (see `server._build_onedrive_graph`).

OFF BY DEFAULT, opt-in: nothing here runs unless the user configures a folder and
connects Microsoft. Privacy posture matches Google Drive intake — the new surface
is the stored OAuth token, not the receipts (already in OneDrive).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:  # classify files by the same extension sets the pipeline uses
    from process_receipts import IMAGE_EXTENSIONS, PDF_EXTENSIONS
except Exception:  # pragma: no cover - keep the module importable standalone
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
    PDF_EXTENSIONS = {".pdf"}

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"
# Where a user can later revoke the app's access (no programmatic revoke endpoint
# exists for consumer refresh tokens — disconnect clears the token locally and
# points the user here).
CONSENT_MANAGE_URL = "https://account.live.com/consent/Manage"
# Bound network calls so an unreachable host fails fast rather than hanging a poll.
ONEDRIVE_TIMEOUT = int(os.getenv("ONEDRIVE_TIMEOUT", "30"))

# Graph permission scopes we support. Files.Read is enough to list + download;
# Files.ReadWrite only if the app should also move/label processed files later.
# offline_access (the refresh token) is always added by scope_string().
_SCOPES = {
    "files.read":      "Files.Read",
    "files.readwrite": "Files.ReadWrite",
}

# Sign-in audience. "consumers" = personal Microsoft accounts (the typical
# personal-OneDrive case), "organizations" = work/school accounts only,
# "common" = both. A specific tenant GUID/domain also works.
_KNOWN_TENANTS = ("consumers", "organizations", "common")


# ── Config ──────────────────────────────────────────────────────────────────────

@dataclass
class OneDriveConfig:
    enabled: bool = False
    folder_path: str = "Receipts"   # path under the OneDrive root, e.g. "Receipts"
    poll_interval: int = 300
    scope: str = "files.read"
    client_id: str = ""             # Azure "Application (client) ID" (public — not a secret)
    tenant: str = "consumers"

    @classmethod
    def from_dict(cls, d: dict | None) -> "OneDriveConfig":
        d = d or {}
        try:
            interval = int(d.get("poll_interval") or 300)
        except (TypeError, ValueError):
            interval = 300
        scope = str(d.get("scope") or "files.read").strip().lower()
        if scope not in _SCOPES:
            scope = "files.read"
        return cls(
            enabled=bool(d.get("enabled")),
            folder_path=str(d.get("folder_path") or "").strip().strip("/"),
            poll_interval=max(30, interval),
            scope=scope,
            client_id=str(d.get("client_id") or "").strip(),
            tenant=_safe_tenant(str(d.get("tenant") or "")),
        )

    def to_public_dict(self) -> dict:
        """Config for the UI — never includes the client secret or token."""
        return {
            "enabled": self.enabled, "folder_path": self.folder_path,
            "poll_interval": self.poll_interval, "scope": self.scope,
            "client_id": self.client_id, "tenant": self.tenant,
        }

    def scope_string(self) -> str:
        """The OAuth scope parameter: the Graph file permission + a refresh token."""
        return f"{_SCOPES.get(self.scope, _SCOPES['files.read'])} offline_access"


def _safe_tenant(tenant: str) -> str:
    """Sanitize the tenant segment used in login URLs (slug/GUID/domain only)."""
    t = re.sub(r"[^A-Za-z0-9._-]", "", (tenant or "").strip())
    return t or "consumers"


def _safe_name(name: str, fallback: str) -> str:
    """Basename-only, sanitized on-disk name (mirrors gdrive_intake._safe_name)."""
    name = (name or "").replace("\x00", "")
    name = os.path.basename(name).strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._+\- ]", "_", name)[:120]
    return name or fallback


def _ext_kind(filename: str, content_type: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in PDF_EXTENSIONS or content_type == "application/pdf":
        return "pdf"
    if ext in IMAGE_EXTENSIONS or content_type.startswith("image/"):
        return "image"
    return None


def _unique_dest(intake_dir: Path, name: str) -> Path:
    """A non-colliding path in the intake folder for a downloaded file."""
    dest = intake_dir / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 10000):
        cand = intake_dir / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
    return intake_dir / f"{stem}_{os.urandom(4).hex()}{suffix}"


# ── Graph HTTP client (stdlib only; faked in tests) ─────────────────────────────

class GraphClient:
    """Thin authenticated Microsoft Graph wrapper. `poll_once`/`_list_folder` only
    call `.get_json()`, so tests fake this object with a few canned responses."""

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _request(self, url: str, *, auth: bool = True) -> urllib.request.Request:
        if url.startswith("/"):
            url = GRAPH_BASE + url
        headers = {"Authorization": f"Bearer {self.access_token}"} if auth else {}
        return urllib.request.Request(url, headers=headers)

    def get_json(self, url: str) -> dict:
        with urllib.request.urlopen(self._request(url), timeout=ONEDRIVE_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_bytes(self, url: str, *, auth: bool = True) -> bytes:
        with urllib.request.urlopen(self._request(url, auth=auth),
                                    timeout=ONEDRIVE_TIMEOUT) as resp:
            return resp.read()


# ── Listing + polling (testable with a fake `graph`) ────────────────────────────

def _folder_children_url(folder_path: str) -> str:
    """Children listing URL for a path under the drive root (path-based addressing)."""
    path = (folder_path or "").strip().strip("/")
    quoted = urllib.parse.quote(path)
    return f"/me/drive/root:/{quoted}:/children?$top=100"


def _list_folder(graph, folder_path: str, *, limit: int = 200) -> list[dict]:
    """List files directly in `folder_path` (subfolders skipped), paging as needed.

    No server-side type filter — Graph children listings can't filter by mimeType
    the way Drive queries can, so `poll_once` classifies via `_ext_kind` instead.
    """
    if not (folder_path or "").strip().strip("/"):
        return []                              # require an explicit inbox folder
    url = _folder_children_url(folder_path)
    out: list[dict] = []
    while url:
        resp = graph.get_json(url)
        for item in resp.get("value", []) or []:
            if "file" in item:                 # has the file facet (not a subfolder)
                out.append(item)
        if len(out) >= limit:
            break
        url = resp.get("@odata.nextLink") or ""
    return out[:limit]


def _download_media(graph, item: dict) -> bytes:
    """Download one drive item's bytes. Prefers the pre-authenticated
    `@microsoft.graph.downloadUrl` (no auth header wanted on that CDN URL);
    falls back to the item /content endpoint."""
    url = item.get("@microsoft.graph.downloadUrl") or ""
    if url:
        return graph.get_bytes(url, auth=False)
    item_id = urllib.parse.quote(str(item.get("id") or ""))
    return graph.get_bytes(f"/me/drive/items/{item_id}/content")


def poll_once(graph, config: OneDriveConfig, intake_dir, *,
              already_seen: set[str] | None = None, limit: int = 200) -> dict:
    """List the inbox folder, download files we haven't seen (by Graph item ID),
    and write each into `intake_dir` so the folder watcher picks it up.

    Dedup is by **Graph item ID**, never by name. Returns a summary dict; the
    `seen_ids` it reports are the item ids the caller should persist.
    """
    already_seen = already_seen if already_seen is not None else set()
    intake_dir = Path(intake_dir)
    summary = {"files": 0, "downloaded": 0, "skipped": 0, "seen_ids": []}
    files = _list_folder(graph, config.folder_path, limit=limit)
    for f in files:
        fid = f.get("id")
        if not fid:
            continue
        summary["files"] += 1
        if fid in already_seen:
            summary["skipped"] += 1
            continue
        mime = ((f.get("file") or {}).get("mimeType") or "").lower()
        kind = _ext_kind(f.get("name") or "", mime)
        if kind is None:                       # not an image/PDF we can process
            summary["skipped"] += 1
            already_seen.add(fid)              # don't re-evaluate it every poll
            summary["seen_ids"].append(fid)
            continue
        name = _safe_name(f.get("name") or fid, fid)
        if not Path(name).suffix:
            name += ".pdf" if kind == "pdf" else ".jpg"
        try:
            data = _download_media(graph, f)
        except Exception:
            continue                           # transient — retry on the next poll
        if not data:
            continue
        intake_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique_dest(intake_dir, name)
        dest.write_bytes(data)
        summary["downloaded"] += 1
        already_seen.add(fid)
        summary["seen_ids"].append(fid)
    return summary


# ── OAuth (device-code flow + refresh; stdlib urllib, no MSAL) ───────────────────

def _token_url(tenant: str) -> str:
    return f"{LOGIN_BASE}/{_safe_tenant(tenant)}/oauth2/v2.0/token"


def _form_post(url: str, fields: dict) -> tuple[int, dict]:
    """POST form-encoded, return (status, parsed-JSON body). 4xx bodies are parsed
    too — the device flow reports `authorization_pending` as an HTTP 400."""
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=ONEDRIVE_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"error": f"http_{exc.code}"}


def device_code_start(config: OneDriveConfig) -> dict:
    """Begin the device-code sign-in. Returns the `user_code` + `verification_uri`
    to show the user, and the `device_code` to poll the token endpoint with."""
    if not config.client_id:
        return {"ok": False, "error": "Set the Azure application (client) ID first."}
    status, d = _form_post(
        f"{LOGIN_BASE}/{_safe_tenant(config.tenant)}/oauth2/v2.0/devicecode",
        {"client_id": config.client_id, "scope": config.scope_string()})
    if status != 200 or not d.get("device_code"):
        return {"ok": False,
                "error": d.get("error_description") or d.get("error") or f"HTTP {status}"}
    return {
        "ok": True,
        "device_code":      d["device_code"],
        "user_code":        d.get("user_code", ""),
        "verification_uri": d.get("verification_uri") or "https://microsoft.com/devicelogin",
        "interval":         int(d.get("interval") or 5),
        "expires_in":       int(d.get("expires_in") or 900),
        "message":          d.get("message", ""),
    }


def device_code_poll(config: OneDriveConfig, device_code: str) -> dict:
    """One poll of the token endpoint. `pending=True` means the user hasn't finished
    signing in yet (keep polling); any other failure is terminal."""
    status, d = _form_post(_token_url(config.tenant), {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": config.client_id,
        "device_code": device_code,
    })
    if d.get("refresh_token"):
        return {"ok": True, "refresh_token": d["refresh_token"],
                "access_token": d.get("access_token", "")}
    err = str(d.get("error") or f"http_{status}")
    if err in ("authorization_pending", "slow_down"):
        return {"ok": False, "pending": True, "error": err}
    return {"ok": False, "pending": False,
            "error": d.get("error_description") or err}


def redeem_refresh_token(config: OneDriveConfig, refresh_token: str,
                         client_secret: str = "") -> dict:
    """Exchange the stored refresh token for an access token. Microsoft ROTATES the
    refresh token: the returned `refresh_token` (when present) replaces the old one
    and must be persisted by the caller."""
    fields = {
        "grant_type": "refresh_token",
        "client_id": config.client_id,
        "refresh_token": refresh_token,
        "scope": config.scope_string(),
    }
    if client_secret:                          # confidential clients only
        fields["client_secret"] = client_secret
    status, d = _form_post(_token_url(config.tenant), fields)
    if not d.get("access_token"):
        return {"ok": False,
                "error": d.get("error_description") or d.get("error") or f"HTTP {status}"}
    return {"ok": True, "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token", "")}


def build_graph(config: OneDriveConfig, refresh_token: str,
                client_secret: str = "") -> tuple[GraphClient | None, str]:
    """Build an authenticated Graph client from the stored refresh token.

    Returns `(client, rotated_refresh_token)` — the caller MUST persist the rotated
    token when non-empty (see the module docstring). `(None, "")` when unconfigured;
    raises RuntimeError when the token refresh itself fails (expired/revoked)."""
    if not (config.client_id and refresh_token):
        return None, ""
    tok = redeem_refresh_token(config, refresh_token, client_secret)
    if not tok.get("ok"):
        raise RuntimeError(tok.get("error") or "token refresh failed")
    return GraphClient(tok["access_token"]), tok.get("refresh_token") or ""


def test_connection(graph, config: OneDriveConfig) -> dict:
    """Verify the Graph connection + that the folder is reachable. UI 'Test'."""
    if graph is None:
        return {"ok": False, "error": "Connect Microsoft and set the client ID first."}
    if not config.folder_path:
        return {"ok": False, "error": "Set the OneDrive inbox folder path first."}
    try:
        files = _list_folder(graph, config.folder_path, limit=1)
        return {"ok": True,
                "message": f"Connected — folder reachable ({'has files' if files else 'empty'})."}
    except Exception as exc:
        return {"ok": False, "error": f"OneDrive request failed: {exc}"}
