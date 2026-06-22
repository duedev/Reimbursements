"""Secret storage kept OUT of the (often cloud-synced) output folder.

The SMTP password and Dropbox access token used to be written straight into
``.app_config.json`` — the file that lives in the output folder users are told
to point at Dropbox/Drive/OneDrive. That silently synced live credentials to a
third-party cloud in cleartext.

Secrets now live in a separate file (default ``.app_secrets.json`` beside the
config file, but set ``SECRETS_PATH`` — the Docker image points it at a
non-synced volume — to relocate it). Any secret previously saved inside
``.app_config.json`` is still read as a fallback and migrated out the next time
it is written, so existing installs keep working.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import process_receipts

# Default location: beside the app config file unless SECRETS_PATH overrides it.
SECRETS_FILE: Path = (
    Path(os.getenv("SECRETS_PATH"))
    if os.getenv("SECRETS_PATH")
    else Path(process_receipts.CONFIG_FILE).parent / ".app_secrets.json"
)


def load_secrets() -> dict:
    try:
        if SECRETS_FILE.exists():
            data = json.loads(SECRETS_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_secret(key: str, value: str) -> None:
    """Persist (or, with a blank value, clear) one secret. Written atomically
    with 0600 perms so it isn't world-readable even before any cloud sync."""
    data = load_secrets()
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, indent=2)
    # mkstemp creates the temp file with 0600 perms *from the start* (and a unique
    # name), so the cleartext secret is never briefly world-readable — the previous
    # write-then-chmod left a race window — and concurrent writers don't collide.
    fd, tmp_name = tempfile.mkstemp(prefix=SECRETS_FILE.name + ".", dir=str(SECRETS_FILE.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(blob)
        tmp.replace(SECRETS_FILE)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _legacy_config_secret(legacy_block: str, legacy_key: str) -> str:
    """Read a secret that an older version left inside .app_config.json."""
    if not (legacy_block and legacy_key):
        return ""
    try:
        cf = Path(process_receipts.CONFIG_FILE)
        if cf.exists():
            block = json.loads(cf.read_text()).get(legacy_block) or {}
            val = block.get(legacy_key)
            if val:
                return str(val)
    except Exception:
        pass
    return ""


def get_secret(key: str, legacy_block: str = "", legacy_key: str = "",
               env: str = "") -> str:
    """Resolve a secret: secrets file → legacy config block → environment."""
    val = load_secrets().get(key)
    if val:
        return str(val)
    legacy = _legacy_config_secret(legacy_block, legacy_key)
    if legacy:
        return legacy
    if env:
        return os.getenv(env, "")
    return ""
