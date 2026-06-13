"""Tests for the optional shared-secret auth gate and secret relocation."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    with TestClient(server.app) as c:
        yield c


def test_open_when_no_token(client, monkeypatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert client.get("/version").status_code == 200


def test_token_required_when_set(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "sekret")
    assert client.get("/version").status_code == 401                     # no token
    assert client.get("/version", headers={"X-Auth-Token": "nope"}).status_code == 401
    assert client.get("/version", headers={"X-Auth-Token": "sekret"}).status_code == 200
    assert client.get("/version?token=sekret").status_code == 200        # query param works


def test_shell_page_is_exempt_and_sets_cookie(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "sekret")
    # The page itself must load (so the token can be supplied via ?token=)…
    r = client.get("/?token=sekret")
    assert r.status_code == 200
    # …and dropping the cookie means subsequent API calls authenticate automatically.
    assert client.get("/version").status_code == 200


def test_admin_restart_is_protected(client, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "sekret")
    assert client.post("/admin/restart").status_code == 401


def test_email_secret_not_written_to_config(client, monkeypatch):
    import json
    import app_secrets
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    r = client.post("/settings/email", json={
        "smtp_host": "smtp.x", "smtp_user": "u", "smtp_pass": "topsecret", "email_to": "t@x.com"})
    assert r.status_code == 200
    cfg_email = json.loads(server.CONFIG_FILE.read_text()).get("email", {})
    assert "smtp_pass" not in cfg_email                       # never in the synced config
    assert app_secrets.load_secrets()["smtp_pass"] == "topsecret"
