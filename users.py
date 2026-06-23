"""users.py — local username/password store + signed session tokens.

Powers multi-user mode (``MULTIUSER_ENABLED``). Deliberately dependency-free:

* Passwords are hashed with stdlib :func:`hashlib.pbkdf2_hmac` (SHA-256, salted,
  high iteration count) — no argon2/bcrypt wheel to install, works everywhere the
  app already runs.
* Sessions are stateless HMAC-signed tokens (``user_id:expiry:sig``) carried in a
  signed, HttpOnly cookie. The signing secret lives in the out-of-band secrets
  store (:mod:`app_secrets`), never in the synced config blob, and is generated on
  first use.

The user store is a small JSON file at the instance root (``.app_users.json``) —
instance-level admin data, not per-user. Identities are the strict slugs validated
by :func:`multiuser.valid_user_id`, so a user id is always safe to use as a path
segment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

import app_secrets
import multiuser

# Instance-level user store. Patchable in tests; defaults beside the app config.
USERS_FILE: Path = Path(os.getenv("OUTPUT_FOLDER", "output")) / ".app_users.json"

_PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS", "240000"))
SESSION_TTL_SECS = int(os.getenv("SESSION_TTL_SECS", str(30 * 24 * 3600)))  # 30 days
SESSION_COOKIE = "mu_session"

_store_lock = __import__("threading").Lock()


# ── Password hashing ────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── Store I/O ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        if USERS_FILE.exists():
            data = json.loads(USERS_FILE.read_text())
            if isinstance(data, dict) and isinstance(data.get("users"), dict):
                return data
    except Exception:
        pass
    return {"users": {}}


def _save(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=USERS_FILE.name + ".", dir=str(USERS_FILE.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(blob)
        tmp.replace(USERS_FILE)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ── User management ─────────────────────────────────────────────────────────────

class UserError(ValueError):
    """Raised for bad input to a user-management operation (mapped to HTTP 400)."""


def list_users() -> list[dict]:
    """Public (no password hashes) view of every account, sorted by id."""
    users = _load()["users"]
    out = []
    for uid in sorted(users):
        rec = users[uid]
        out.append({
            "user_id": uid,
            "display": rec.get("display") or uid,
            "is_admin": bool(rec.get("is_admin")),
            "created": rec.get("created", 0),
        })
    return out


def user_count() -> int:
    return len(_load()["users"])


def get_user(user_id: str) -> dict | None:
    return _load()["users"].get(user_id)


def create_user(user_id: str, password: str, *, is_admin: bool = False,
                display: str = "") -> dict:
    user_id = (user_id or "").strip().lower()
    if not multiuser.valid_user_id(user_id):
        raise UserError("Username must be 1–32 chars: lowercase letters, digits, '-' or '_'.")
    if not password or len(password) < 6:
        raise UserError("Password must be at least 6 characters.")
    with _store_lock:
        data = _load()
        if user_id in data["users"]:
            raise UserError(f"User '{user_id}' already exists.")
        data["users"][user_id] = {
            "pw": hash_password(password),
            "is_admin": bool(is_admin),
            "display": (display or user_id).strip()[:64],
            "created": int(time.time()),
        }
        _save(data)
    return {"user_id": user_id, "is_admin": bool(is_admin), "display": display or user_id}


def set_password(user_id: str, password: str) -> None:
    if not password or len(password) < 6:
        raise UserError("Password must be at least 6 characters.")
    with _store_lock:
        data = _load()
        rec = data["users"].get(user_id)
        if not rec:
            raise UserError(f"No such user '{user_id}'.")
        rec["pw"] = hash_password(password)
        _save(data)


def set_admin(user_id: str, is_admin: bool) -> None:
    with _store_lock:
        data = _load()
        rec = data["users"].get(user_id)
        if not rec:
            raise UserError(f"No such user '{user_id}'.")
        if not is_admin and rec.get("is_admin") and _admin_count(data) <= 1:
            raise UserError("Cannot remove the last admin.")
        rec["is_admin"] = bool(is_admin)
        _save(data)


def delete_user(user_id: str) -> None:
    with _store_lock:
        data = _load()
        rec = data["users"].get(user_id)
        if not rec:
            raise UserError(f"No such user '{user_id}'.")
        if rec.get("is_admin") and _admin_count(data) <= 1:
            raise UserError("Cannot delete the last admin.")
        del data["users"][user_id]
        _save(data)


def _admin_count(data: dict) -> int:
    return sum(1 for r in data["users"].values() if r.get("is_admin"))


def is_admin(user_id: str) -> bool:
    rec = get_user(user_id)
    return bool(rec and rec.get("is_admin"))


def authenticate(user_id: str, password: str) -> bool:
    rec = get_user((user_id or "").strip().lower())
    if not rec:
        # Compare against a dummy hash anyway to blunt user-enumeration timing.
        verify_password(password or "", "pbkdf2_sha256$1$00$00")
        return False
    return verify_password(password or "", rec.get("pw", ""))


def ensure_seed() -> None:
    """On a fresh multi-user instance with no users yet, seed an admin from
    ``MULTIUSER_ADMIN_USER`` / ``MULTIUSER_ADMIN_PASSWORD`` if both are set, so the
    operator always has a first way in without editing JSON by hand."""
    if user_count() > 0:
        return
    uid = os.getenv("MULTIUSER_ADMIN_USER", "").strip().lower()
    pw = os.getenv("MULTIUSER_ADMIN_PASSWORD", "")
    if uid and pw:
        try:
            create_user(uid, pw, is_admin=True, display=uid)
        except UserError:
            pass


# ── Session tokens (stateless, HMAC-signed) ─────────────────────────────────────

def _secret() -> bytes:
    sec = app_secrets.get_secret("session_secret", env="SESSION_SECRET")
    if not sec:
        sec = secrets.token_hex(32)
        app_secrets.save_secret("session_secret", sec)
    return sec.encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(user_id: str, ttl: int | None = None) -> str:
    expiry = int(time.time()) + (SESSION_TTL_SECS if ttl is None else ttl)
    payload = f"{user_id}:{expiry}"
    return f"{payload}:{_sign(payload)}"


def verify_session(token: str) -> str:
    """Return the ``user_id`` of a valid, unexpired token, else ''."""
    try:
        user_id, expiry_s, sig = (token or "").rsplit(":", 2)
        payload = f"{user_id}:{expiry_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return ""
        if int(expiry_s) < int(time.time()):
            return ""
        if not multiuser.valid_user_id(user_id):
            return ""
        # Token survives only while the account exists (deleting a user logs them out).
        if get_user(user_id) is None:
            return ""
        return user_id
    except Exception:
        return ""
