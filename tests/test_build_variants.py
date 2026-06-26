"""Bundled vs. lite build variants: the compose overlays + env presets exist and
encode the right LLM wiring. Pure YAML/text checks — no Docker required.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    return yaml.safe_load((ROOT / name).read_text())


def test_overlay_files_exist():
    for f in ("docker-compose.bundled.yml", "docker-compose.lite.yml",
              ".env.bundled.example", ".env.lite.example"):
        assert (ROOT / f).exists(), f"missing {f}"


def test_bundled_overlay_points_app_at_model_server():
    d = _load("docker-compose.bundled.yml")
    app = d["services"]["receipt-processor"]
    env = app["environment"]
    assert any("model-server:1234" in str(e) for e in env)
    # waits for the bundled model before serving
    assert "model-server" in app.get("depends_on", {})


def test_lite_overlay_has_no_model_server():
    d = _load("docker-compose.lite.yml")
    assert "model-server" not in d.get("services", {})


def test_bundled_preset_enables_profile():
    txt = (ROOT / ".env.bundled.example").read_text()
    assert "COMPOSE_PROFILES=bundled-llm" in txt
    assert "LMSTUDIO_BASE_URL=http://model-server:1234/v1" in txt
    assert "docker-compose.bundled.yml" in txt


def test_lite_preset_does_not_enable_bundled_profile():
    txt = (ROOT / ".env.lite.example").read_text()
    assert "COMPOSE_PROFILES=bundled-llm" not in txt
    assert "docker-compose.lite.yml" in txt


def test_base_compose_still_profile_gates_model_server():
    """Backward-compat: the base file keeps model-server behind the profile."""
    d = _load("docker-compose.yml")
    assert d["services"]["model-server"]["profiles"] == ["bundled-llm"]
