"""gdrive_intake.py — pull receipts from a Google Drive folder into the pipeline.

The Google-Drive-as-hub capture path (see `GOOGLE_DRIVE_IMPORT.md`): make one Drive
folder the app's "receipts inbox," fill it from your phone (Drive Scan / share-sheet)
and/or from Gmail (the `gmail_to_drive.gs` Apps Script), and have the app poll that
folder and download new files into the existing intake — where the folder watcher
and pipeline take over **unchanged**.

This module mirrors `email_intake.py`'s shape:
- **Pure / import-light core.** Listing + the download decision (`poll_once`,
  `_list_folder`, `_safe_name`, `_ext_kind`) are unit-testable with a *fake* Drive
  service object — no Google libraries and no network. The actual byte download
  (`_download_media`) and OAuth (`build_service`, `auth_url`, `exchange_code`) lazily
  import the Google client libraries, so the module imports fine without them.
- **Dedup by Drive file ID** (NOT filename — names collide across sources and
  re-uploads). The caller persists the seen ids (see `server._load_gdrive_seen`).
- **Basename-only writes** into the intake folder (no path traversal).

OFF BY DEFAULT, opt-in: nothing here runs unless the user configures a folder and
connects Google. Privacy posture is covered in `GOOGLE_DRIVE_IMPORT.md` §6 and the
README/TUTORIAL/ADVISORY — the new surface is the stored OAuth token, not the mail
(which is already on Google's servers).
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

try:  # classify files by the same extension sets the pipeline uses
    from process_receipts import IMAGE_EXTENSIONS, PDF_EXTENSIONS
except Exception:  # pragma: no cover - keep the module importable standalone
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
    PDF_EXTENSIONS = {".pdf"}

# Drive OAuth scopes we support. read-only is enough to list + download; drive.file
# is only needed if the app should also move/label processed files (Phase 3).
_SCOPE_URLS = {
    "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    "drive.file":     "https://www.googleapis.com/auth/drive.file",
}
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Bound network calls so an unreachable host fails fast rather than hanging a poll.
GDRIVE_TIMEOUT = int(os.getenv("GDRIVE_TIMEOUT", "30"))


# ── Config ──────────────────────────────────────────────────────────────────────

@dataclass
class GDriveConfig:
    enabled: bool = False
    folder_id: str = ""
    poll_interval: int = 300
    scope: str = "drive.readonly"
    move_processed: bool = False
    client_id: str = ""          # OAuth client id (public — not a secret)

    @classmethod
    def from_dict(cls, d: dict | None) -> "GDriveConfig":
        d = d or {}
        try:
            interval = int(d.get("poll_interval") or 300)
        except (TypeError, ValueError):
            interval = 300
        scope = str(d.get("scope") or "drive.readonly").strip()
        if scope not in _SCOPE_URLS:
            scope = "drive.readonly"
        return cls(
            enabled=bool(d.get("enabled")),
            folder_id=str(d.get("folder_id") or "").strip(),
            poll_interval=max(30, interval),
            scope=scope,
            move_processed=bool(d.get("move_processed")),
            client_id=str(d.get("client_id") or "").strip(),
        )

    def to_public_dict(self) -> dict:
        """Config for the UI — never includes the client secret or token."""
        return {
            "enabled": self.enabled, "folder_id": self.folder_id,
            "poll_interval": self.poll_interval, "scope": self.scope,
            "move_processed": self.move_processed, "client_id": self.client_id,
        }

    def scope_url(self) -> str:
        return _SCOPE_URLS.get(self.scope, _SCOPE_URLS["drive.readonly"])


def _safe_name(name: str, fallback: str) -> str:
    """Basename-only, sanitized on-disk name (mirrors email_intake._safe_name)."""
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


# ── Listing + polling (testable with a fake `service`) ──────────────────────────

def _list_folder(service, folder_id: str, *, limit: int = 200) -> list[dict]:
    """List image/PDF files directly in `folder_id` (newest first), paging as needed."""
    if not folder_id:
        return []
    q = (f"'{folder_id}' in parents and trashed = false "
         f"and (mimeType contains 'image/' or mimeType = 'application/pdf')")
    out: list[dict] = []
    page_token = None
    while True:
        resp = service.files().list(
            q=q, spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime)",
            pageSize=min(100, limit), pageToken=page_token,
            orderBy="modifiedTime desc",
        ).execute()
        out.extend(resp.get("files", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token or len(out) >= limit:
            break
    return out[:limit]


def _download_media(service, file_id: str) -> bytes:
    """Download one Drive file's bytes. Lazily imports the Google HTTP helper."""
    from googleapiclient.http import MediaIoBaseDownload  # lazy
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def poll_once(service, config: GDriveConfig, intake_dir, *,
              already_seen: set[str] | None = None, limit: int = 200) -> dict:
    """List the inbox folder, download files we haven't seen (by Drive file ID),
    and write each into `intake_dir` so the folder watcher picks it up.

    Dedup is by **Drive file ID**, never by name. Returns a summary dict; the
    `seen_ids` it reports are the file ids the caller should persist.
    """
    already_seen = already_seen if already_seen is not None else set()
    intake_dir = Path(intake_dir)
    summary = {"files": 0, "downloaded": 0, "skipped": 0, "seen_ids": []}
    files = _list_folder(service, config.folder_id, limit=limit)
    for f in files:
        fid = f.get("id")
        if not fid:
            continue
        summary["files"] += 1
        if fid in already_seen:
            summary["skipped"] += 1
            continue
        kind = _ext_kind(f.get("name") or "", (f.get("mimeType") or "").lower())
        if kind is None:                       # not an image/PDF we can process
            summary["skipped"] += 1
            already_seen.add(fid)              # don't re-evaluate it every poll
            summary["seen_ids"].append(fid)
            continue
        name = _safe_name(f.get("name") or fid, fid)
        if not Path(name).suffix:
            name += ".pdf" if kind == "pdf" else ".jpg"
        try:
            data = _download_media(service, fid)
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


# ── OAuth + service (lazy Google client libraries) ──────────────────────────────

def build_service(config: GDriveConfig, client_secret: str, refresh_token: str):
    """Build an authenticated Drive v3 service from the stored refresh token."""
    if not (config.client_id and client_secret and refresh_token):
        return None
    from google.oauth2.credentials import Credentials  # lazy
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=config.client_id,
        client_secret=client_secret,
        token_uri=TOKEN_URI,
        scopes=[config.scope_url()],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def auth_url(config: GDriveConfig, client_secret: str, redirect_uri: str) -> str:
    """Build the Google consent URL for the one-time installed-app authorization."""
    from google_auth_oauthlib.flow import Flow  # lazy
    flow = Flow.from_client_config(
        {"installed": {
            "client_id": config.client_id, "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }},
        scopes=[config.scope_url()], redirect_uri=redirect_uri,
    )
    url, _state = flow.authorization_url(access_type="offline", prompt="consent",
                                         include_granted_scopes="true")
    return url


def exchange_code(config: GDriveConfig, client_secret: str, code: str,
                  redirect_uri: str) -> str:
    """Exchange an authorization code for a refresh token (one-time consent)."""
    from google_auth_oauthlib.flow import Flow  # lazy
    flow = Flow.from_client_config(
        {"installed": {
            "client_id": config.client_id, "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }},
        scopes=[config.scope_url()], redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    return getattr(flow.credentials, "refresh_token", "") or ""


def revoke_token(refresh_token: str) -> bool:
    """Best-effort revoke of the stored token at Google's revoke endpoint."""
    if not refresh_token:
        return True
    try:
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({"token": refresh_token}).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=GDRIVE_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def test_connection(service, config: GDriveConfig) -> dict:
    """Verify the Drive connection + that the folder is reachable. UI 'Test'."""
    if service is None:
        return {"ok": False, "error": "Connect Google and set the OAuth client first."}
    if not config.folder_id:
        return {"ok": False, "error": "Set the Drive inbox folder ID first."}
    try:
        files = _list_folder(service, config.folder_id, limit=1)
        return {"ok": True,
                "message": f"Connected — folder reachable ({'has files' if files else 'empty'})."}
    except Exception as exc:
        return {"ok": False, "error": f"Drive request failed: {exc}"}
