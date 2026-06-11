"""Tests for watch_mode email-config resolution (UI config overrides env)."""
import json

import watch_mode


def test_env_fallback_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", tmp_path / ".app_config.json")  # absent
    monkeypatch.setenv("SMTP_HOST", "env.smtp")
    monkeypatch.setenv("SMTP_PORT", "2500")
    monkeypatch.setenv("EMAIL_TO", "env@to.com")
    cfg = watch_mode.load_email_config()
    assert cfg["host"] == "env.smtp"
    assert cfg["port"] == 2500
    assert cfg["to"] == "env@to.com"
    assert cfg["subject"] == "Weekly Reimbursement Report"


def test_config_overrides_env(tmp_path, monkeypatch):
    cf = tmp_path / ".app_config.json"
    cf.write_text(json.dumps({"email": {
        "smtp_host": "ui.smtp", "smtp_port": 2525,
        "smtp_user": "u", "smtp_pass": "p", "email_to": "ui@to.com",
    }}))
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", cf)
    monkeypatch.setenv("SMTP_HOST", "env.smtp")
    monkeypatch.setenv("EMAIL_TO", "env@to.com")
    cfg = watch_mode.load_email_config()
    assert cfg["host"] == "ui.smtp"      # UI value wins
    assert cfg["port"] == 2525
    assert cfg["to"] == "ui@to.com"


def test_blank_config_value_falls_back_to_env(tmp_path, monkeypatch):
    cf = tmp_path / ".app_config.json"
    cf.write_text(json.dumps({"email": {"smtp_host": "", "email_to": "ui@to.com"}}))
    monkeypatch.setattr(watch_mode, "CONFIG_FILE", cf)
    monkeypatch.setenv("SMTP_HOST", "env.smtp")
    cfg = watch_mode.load_email_config()
    assert cfg["host"] == "env.smtp"     # empty UI value yields to env
    assert cfg["to"] == "ui@to.com"


def test_recipients_split():
    assert watch_mode._recipients("a@x.com, b@y.com ,") == ["a@x.com", "b@y.com"]
    assert watch_mode._recipients("") == []
