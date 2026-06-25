"""Multi-user mode: per-user workspace isolation + local username/password auth.

These run with ``multiuser.ENABLED`` flipped ON (the rest of the suite runs with it
OFF, proving single-user is unchanged). They assert the two things that matter for
a privacy feature: (1) no user can see another's board/results/folders, and (2) the
auth gate actually blocks unauthenticated and non-admin access.
"""
import time

import pytest
from fastapi.testclient import TestClient

import multiuser
import server
import users


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def mu(tmp_path, monkeypatch):
    """Enable multi-user mode with per-test isolated user store + per-user roots."""
    monkeypatch.setattr(multiuser, "ENABLED", True)
    monkeypatch.setattr(multiuser, "USERS_BASE", tmp_path / "users")
    monkeypatch.setattr(users, "USERS_FILE", tmp_path / ".app_users.json")
    # STATE_FILE is a context proxy in production; conftest replaces it with a fixed
    # tmp Path. Restore the proxy so each user persists to its own state file.
    monkeypatch.setattr(server, "STATE_FILE", multiuser.path_proxy("state_file"))

    def _fresh_registry():
        with multiuser._registry_lock:
            multiuser._workspaces.clear()
            multiuser._workspaces[multiuser.DEFAULT_USER] = multiuser.default_workspace()
    _fresh_registry()
    yield tmp_path
    _fresh_registry()


def _bind(uid):
    return multiuser.bind_user(uid)


# ── Identity rules ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("uid,ok", [
    ("alice", True), ("bob-1", True), ("a_b", True), ("a1", True),
    ("", False), ("Alice", False), ("default", False), ("users", False),
    ("../etc", False), ("a/b", False), ("a.b", False), ("-lead", False),
    ("x" * 33, False),
])
def test_valid_user_id(uid, ok):
    assert multiuser.valid_user_id(uid) is ok


# ── Password hashing + sessions ─────────────────────────────────────────────────

def test_password_hash_roundtrip():
    h = users.hash_password("hunter2!")
    assert h.startswith("pbkdf2_sha256$")
    assert users.verify_password("hunter2!", h)
    assert not users.verify_password("wrong", h)
    # Two hashes of the same password differ (random salt).
    assert h != users.hash_password("hunter2!")


def test_session_token_roundtrip(mu):
    users.create_user("alice", "secret1", is_admin=True)
    tok = users.make_session("alice")
    assert users.verify_session(tok) == "alice"
    # Tampered signature / payload → rejected.
    assert users.verify_session(tok[:-2] + ("00" if tok[-2:] != "00" else "11")) == ""
    assert users.verify_session("alice:9999999999:deadbeef") == ""
    # Expired → rejected.
    assert users.verify_session(users.make_session("alice", ttl=-5)) == ""
    # Unknown user (no account) → rejected even with a valid signature.
    assert users.verify_session(users.make_session("ghost")) == ""


# ── Workspace isolation (the core privacy guarantee) ────────────────────────────

def test_results_kanban_isolated_between_users(mu):
    ta = _bind("alice")
    try:
        server._results.append({"vendor": "A-Corp", "amount": 1.0})
        server._kanban["a.jpg"] = {"status": "done"}
    finally:
        multiuser.reset(ta)

    tb = _bind("bob")
    try:
        assert list(server._results) == []           # bob sees none of alice's data
        assert "a.jpg" not in server._kanban
        server._results.append({"vendor": "B-Corp", "amount": 2.0})
    finally:
        multiuser.reset(tb)

    ta = _bind("alice")
    try:
        assert [r["vendor"] for r in server._results] == ["A-Corp"]   # unchanged
    finally:
        multiuser.reset(ta)


def test_per_user_folders_and_state_paths(mu):
    wa = multiuser.get_workspace("alice")
    wb = multiuser.get_workspace("bob")
    assert wa.images_folder != wb.images_folder
    assert wa.state_file != wb.state_file
    # Each user's root is contained under the users base (no traversal escape).
    base = (mu / "users").resolve()
    assert base in wa.root.resolve().parents or wa.root.resolve().parent == base
    # An invalid id never escapes — it resolves to the shared default workspace.
    assert multiuser.get_workspace("../escape") is multiuser.default_workspace()


def test_default_workspace_when_flag_off():
    # With the flag off, every id collapses to the single default workspace.
    assert not multiuser.ENABLED
    assert multiuser.get_workspace("alice") is multiuser.default_workspace()


# ── Auth endpoints ──────────────────────────────────────────────────────────────

def test_multiuser_status_and_setup_flow(mu):
    c = TestClient(server.app)
    st = c.get("/multiuser/status").json()
    assert st == {"enabled": True, "needs_setup": True}

    # Before setup, protected endpoints are blocked.
    assert c.get("/stats").status_code == 401

    # Create the first admin via /setup → auto-logged-in (cookie set).
    r = c.post("/setup", json={"username": "alice", "password": "secret1"})
    assert r.status_code == 200 and r.json()["is_admin"] is True
    assert c.get("/multiuser/status").json()["needs_setup"] is False
    # Now authenticated → protected endpoint works.
    assert c.get("/stats").status_code == 200
    me = c.get("/me").json()
    assert me["authenticated"] and me["user_id"] == "alice" and me["is_admin"]

    # Setup can't be replayed once a user exists.
    assert c.post("/setup", json={"username": "x", "password": "yyyyyy"}).status_code == 400


def test_login_logout(mu):
    users.create_user("alice", "secret1")
    c = TestClient(server.app)
    assert c.get("/me").json()["authenticated"] is False
    assert c.post("/login", json={"username": "alice", "password": "nope"}).status_code == 401
    assert c.post("/login", json={"username": "alice", "password": "secret1"}).status_code == 200
    assert c.get("/me").json()["user_id"] == "alice"
    assert c.post("/logout").status_code == 200
    assert c.get("/me").json()["authenticated"] is False


def test_admin_only_user_management(mu):
    users.create_user("alice", "secret1", is_admin=True)
    users.create_user("bob", "secret1", is_admin=False)

    ca = TestClient(server.app)
    ca.post("/login", json={"username": "alice", "password": "secret1"})
    cb = TestClient(server.app)
    cb.post("/login", json={"username": "bob", "password": "secret1"})

    # Admin can list + create; non-admin is forbidden.
    assert ca.get("/users").status_code == 200
    assert cb.get("/users").status_code == 403
    assert cb.post("/users", json={"username": "carol", "password": "secret1"}).status_code == 403
    assert ca.post("/users", json={"username": "carol", "password": "secret1"}).status_code == 200
    assert {u["user_id"] for u in ca.get("/users").json()["users"]} == {"alice", "bob", "carol"}

    # A user may change their own password; not someone else's (unless admin).
    assert cb.post("/users/bob/password", json={"password": "newpass1"}).status_code == 200
    assert cb.post("/users/alice/password", json={"password": "hacked1"}).status_code == 403
    assert ca.post("/users/bob/password", json={"password": "reset12"}).status_code == 200


def test_end_to_end_data_isolation_over_http(mu):
    users.create_user("alice", "secret1", is_admin=True)
    users.create_user("bob", "secret1")
    ca = TestClient(server.app)
    ca.post("/login", json={"username": "alice", "password": "secret1"})
    cb = TestClient(server.app)
    cb.post("/login", json={"username": "bob", "password": "secret1"})

    assert ca.post("/results/add-manual", json={"filename": "alice.jpg", "vendor": "A"}).status_code == 200
    assert cb.post("/results/add-manual", json={"filename": "bob1.jpg", "vendor": "B"}).status_code == 200
    assert cb.post("/results/add-manual", json={"filename": "bob2.jpg", "vendor": "B"}).status_code == 200

    # Each user's own view (inspected via the bound workspace) holds only its data.
    ta = _bind("alice")
    try:
        assert [r["_file"] for r in server._results] == ["alice.jpg"]
    finally:
        multiuser.reset(ta)
    tb = _bind("bob")
    try:
        assert sorted(r["_file"] for r in server._results) == ["bob1.jpg", "bob2.jpg"]
    finally:
        multiuser.reset(tb)


def test_single_user_mode_needs_no_login():
    # Flag off (conftest pins it off): /me reports a synthetic admin, nothing gated.
    # NB: production now DEFAULTS multi-user ON — see test_multiuser_default_on.
    c = TestClient(server.app)
    me = c.get("/me").json()
    assert me == {"multiuser": False, "authenticated": True,
                  "user_id": "default", "display": "", "is_admin": True}
    assert c.get("/stats").status_code == 200
    assert c.get("/multiuser/status").json()["enabled"] is False


@pytest.mark.parametrize("env_val,expected", [
    (None,    "True"),    # unset → ON (the new default)
    ("",      "True"),    # empty → ON
    ("true",  "True"),
    ("1",     "True"),
    ("false", "False"),   # explicit opt-out
    ("0",     "False"),
    ("off",   "False"),
])
def test_multiuser_default_on(env_val, expected):
    """Import-time parsing: MULTIUSER_ENABLED now defaults ON. Run in a subprocess
    so reloading the module can't disturb the in-process registry/proxies."""
    import os
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if k != "MULTIUSER_ENABLED"}
    if env_val is not None:
        env["MULTIUSER_ENABLED"] = env_val
    out = subprocess.run(
        [sys.executable, "-c", "import multiuser; print(multiuser.ENABLED)"],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected
