"""Tests for the secrets store (kept out of the cloud-synced config folder)."""
import json
import os
import stat
from pathlib import Path

import pytest

import app_secrets
import process_receipts


def test_save_secret_round_trip():
    app_secrets.save_secret("smtp_password", "hunter2")
    assert app_secrets.get_secret("smtp_password") == "hunter2"
    assert app_secrets.load_secrets()["smtp_password"] == "hunter2"


def test_save_secret_blank_clears():
    app_secrets.save_secret("k", "v")
    app_secrets.save_secret("k", "")
    assert app_secrets.get_secret("k") == ""
    assert "k" not in app_secrets.load_secrets()


def test_save_secret_update_preserves_other_keys():
    app_secrets.save_secret("a", "1")
    app_secrets.save_secret("b", "2")
    app_secrets.save_secret("a", "3")
    data = app_secrets.load_secrets()
    assert data == {"a": "3", "b": "2"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_secrets_file_is_0600():
    app_secrets.save_secret("smtp_password", "topsecret")
    mode = stat.S_IMODE(os.stat(app_secrets.SECRETS_FILE).st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_secrets_file_stays_0600_after_update():
    app_secrets.save_secret("smtp_password", "one")
    app_secrets.save_secret("smtp_password", "two")
    mode = stat.S_IMODE(os.stat(app_secrets.SECRETS_FILE).st_mode)
    assert mode == 0o600, oct(mode)


def test_no_leftover_tmp_files():
    app_secrets.save_secret("smtp_password", "x")
    leftovers = list(Path(app_secrets.SECRETS_FILE).parent.glob(".app_secrets.json.*"))
    assert leftovers == []


def test_get_secret_env_fallback(monkeypatch):
    monkeypatch.setenv("MY_ENV_SECRET", "fromenv")
    assert app_secrets.get_secret("absent_key", env="MY_ENV_SECRET") == "fromenv"


def test_get_secret_prefers_store_over_env(monkeypatch):
    monkeypatch.setenv("MY_ENV_SECRET", "fromenv")
    app_secrets.save_secret("k", "fromstore")
    assert app_secrets.get_secret("k", env="MY_ENV_SECRET") == "fromstore"


def test_get_secret_legacy_config_migration():
    # A secret an older version left inside .app_config.json is read as a fallback.
    Path(process_receipts.CONFIG_FILE).write_text(
        json.dumps({"email": {"smtp_password": "legacypw"}}))
    assert app_secrets.get_secret(
        "smtp_password", legacy_block="email", legacy_key="smtp_password") == "legacypw"


def test_load_secrets_tolerates_corrupt_file():
    Path(app_secrets.SECRETS_FILE).write_text("{ not json")
    assert app_secrets.load_secrets() == {}
