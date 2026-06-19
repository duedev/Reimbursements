"""Tests for the unified LLM-provider layer (local server vs. OpenRouter cloud).

Covers three things at once:
  1. The redesigned local-server resolution and the regression it fixes — an
     EXPLICIT "custom" selection must never silently resolve to the bundled
     docker :11434 URL (the "stuck on Docker URL" bug), even when a stale legacy
     ``llm_model_config`` docker entry is still on disk.
  2. The OpenRouter integration: free + vision model filtering/ranking, the
     provider apply/dispatch, the API-key seam, and the endpoints.
  3. The "send OCR text only" privacy gate (LLM_ALLOW_IMAGE) that suppresses any
     transmission of the receipt IMAGE to the model.

Network is mocked throughout (no real sockets), matching the rest of the suite.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import app_secrets
import server
import process_receipts as pr


# ── restore module globals so provider tests don't leak into each other ────────

@pytest.fixture(autouse=True)
def _restore_pr_globals():
    saved = {
        "LMSTUDIO_BASE_URL":    pr.LMSTUDIO_BASE_URL,
        "LLM_API_KEY":          pr.LLM_API_KEY,
        "LLM_EXTRA_HEADERS":    dict(pr.LLM_EXTRA_HEADERS),
        "LLM_EXTRA_BODY":       dict(pr.LLM_EXTRA_BODY),
        "LLM_ALLOW_IMAGE":      pr.LLM_ALLOW_IMAGE,
        "_active_distill_model": pr._active_distill_model,
        "_active_ocr_model":    pr._active_ocr_model,
        "_llm_ocr_enabled":     pr._llm_ocr_enabled,
    }
    yield
    for k, v in saved.items():
        setattr(pr, k, v)


@pytest.fixture(autouse=True)
def _no_openrouter_network(monkeypatch):
    """Default: no real OpenRouter catalogue calls. Tests that need a catalogue
    (or a specific fallback) override _fetch_openrouter_models / the helpers."""
    monkeypatch.setattr(server, "_fetch_openrouter_models", lambda *a, **k: [])


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "_startup_models", lambda: None)
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(pr, "initialize_models", lambda *a, **k: None)
    monkeypatch.setattr(pr, "warm_up_model", lambda *a, **k: True)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    with TestClient(server.app) as c:
        yield c


# ── local-server resolution + the "stuck on docker URL" regression ─────────────

def test_explicit_custom_blank_url_never_falls_through_to_docker(monkeypatch):
    """THE regression: a stale legacy docker entry must NOT win when the user has
    explicitly selected 'custom'. A blank custom URL → localhost, never :11434."""
    monkeypatch.setattr(server, "_in_docker", lambda: False)
    cfg = {
        "llm_model_config": {"server_type": "docker", "base_url": ""},   # stale legacy
        "llm_server":       {"server_type": "custom", "base_url": ""},   # user's choice
    }
    server._apply_llm_server_config(cfg)
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:1234/v1"
    assert "11434" not in pr.LMSTUDIO_BASE_URL


def test_explicit_custom_url_wins_over_legacy_docker(monkeypatch):
    cfg = {
        "llm_model_config": {"server_type": "docker"},
        "llm_server":       {"server_type": "custom", "base_url": "http://192.168.0.5:1234"},
    }
    server._apply_llm_server_config(cfg)
    assert pr.LMSTUDIO_BASE_URL == "http://192.168.0.5:1234/v1"


def test_docker_selection_still_resolves(monkeypatch):
    """Docker recovery behaviour is preserved for users who actually want it."""
    monkeypatch.setattr(server, "_in_docker", lambda: False)
    server._apply_llm_server_config({"llm_server": {"server_type": "docker", "base_url": ""}})
    assert pr.LMSTUDIO_BASE_URL == "http://127.0.0.1:11434/v1"


def test_local_apply_resets_cloud_runtime(monkeypatch):
    pr.LLM_API_KEY = "sk-or-stale"
    pr.LLM_EXTRA_HEADERS = {"X-Title": "x"}
    pr.LLM_ALLOW_IMAGE = False
    server._apply_llm_server_config(
        {"provider": "local",
         "llm_server": {"server_type": "custom", "base_url": "http://127.0.0.1:1234"}})
    assert pr.LLM_API_KEY == "lmstudio"
    assert pr.LLM_EXTRA_HEADERS == {}
    assert pr.LLM_ALLOW_IMAGE is True


# ── OpenRouter free/vision filtering + ranking ─────────────────────────────────

_OR_SAMPLE = [
    {"id": "google/gemini-2.0-flash-exp:free",
     "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text", "image"]},
     "context_length": 1000000},
    {"id": "meta-llama/llama-3.1-8b-instruct:free",            # text-only → excluded
     "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"]},
     "context_length": 131072},
    {"id": "openai/gpt-4o",                                    # paid → excluded
     "pricing": {"prompt": "0.005", "completion": "0.015"},
     "architecture": {"input_modalities": ["text", "image"]},
     "context_length": 128000},
    {"id": "qwen/qwen2.5-vl-7b-instruct:free",
     "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"modality": "text+image->text"},
     "context_length": 32000},
]


def test_model_free_and_vision_helpers():
    assert server._model_is_free({"pricing": {"prompt": "0", "completion": "0"}})
    assert not server._model_is_free({"pricing": {"prompt": "0.001", "completion": "0"}})
    assert not server._model_is_free({"pricing": {}})
    assert server._model_is_vision({"architecture": {"input_modalities": ["text", "image"]}})
    assert server._model_is_vision({"architecture": {"modality": "text+image->text"}})
    assert not server._model_is_vision({"architecture": {"input_modalities": ["text"]}})


def test_free_vision_filter_rank_and_autopick(monkeypatch):
    monkeypatch.setattr(server, "_fetch_openrouter_models", lambda *a, **k: list(_OR_SAMPLE))
    out = server._openrouter_free_vision_models()
    ids = [m["id"] for m in out]
    assert "meta-llama/llama-3.1-8b-instruct:free" not in ids   # text-only dropped
    assert "openai/gpt-4o" not in ids                           # paid dropped
    assert set(ids) == {"google/gemini-2.0-flash-exp:free",
                        "qwen/qwen2.5-vl-7b-instruct:free"}
    # gemini ranks first (preferred family + larger context)
    assert ids[0] == "google/gemini-2.0-flash-exp:free"
    assert server._openrouter_autopick() == "google/gemini-2.0-flash-exp:free"


def test_autopick_empty_when_offline(monkeypatch):
    monkeypatch.setattr(server, "_fetch_openrouter_models", lambda *a, **k: [])
    assert server._openrouter_autopick() == ""


# ── OpenRouter provider apply ──────────────────────────────────────────────────

def test_apply_openrouter_points_client_at_cloud(monkeypatch):
    app_secrets.save_secret("openrouter_api_key", "sk-or-test")
    cfg = {"provider": "openrouter",
           "openrouter": {"model": "auto",
                          "resolved_model": "google/gemini-2.0-flash-exp:free",
                          "send_image": True}}
    server._apply_llm_server_config(cfg)
    assert pr.LMSTUDIO_BASE_URL == pr.OPENROUTER_BASE_URL
    assert pr.LLM_API_KEY == "sk-or-test"
    assert pr.LLM_EXTRA_HEADERS.get("X-Title")
    assert pr.LLM_ALLOW_IMAGE is True
    assert pr._active_distill_model == "google/gemini-2.0-flash-exp:free"


def test_apply_openrouter_ocr_text_only_disables_image():
    server._apply_openrouter_config(
        {"openrouter": {"resolved_model": "m", "send_image": False}})
    assert pr.LLM_ALLOW_IMAGE is False


def test_ensure_reachable_never_overrides_openrouter(monkeypatch):
    server._save_config({"provider": "openrouter"})
    pr.LMSTUDIO_BASE_URL = pr.OPENROUTER_BASE_URL
    monkeypatch.setattr(server, "_probe_llm_url", lambda *a, **k: (False, 0))
    monkeypatch.setattr(server, "_autodetect_llm_url",
                        lambda *a, **k: "http://127.0.0.1:1234/v1")
    server._ensure_llm_reachable()
    assert pr.LMSTUDIO_BASE_URL == pr.OPENROUTER_BASE_URL   # untouched


# ── make_client honours the active provider key/headers ────────────────────────

def test_make_client_uses_active_key_and_base_url():
    pr.LMSTUDIO_BASE_URL = "https://openrouter.ai/api/v1"
    pr.LLM_API_KEY = "sk-or-zzz"
    pr.LLM_EXTRA_HEADERS = {"X-Title": "Reimbursements"}
    c = pr.make_client()
    assert c.api_key == "sk-or-zzz"
    assert str(c.base_url).rstrip("/").endswith("/v1")


# ── endpoints ──────────────────────────────────────────────────────────────────

def test_post_provider_openrouter_auto_resolves_and_persists(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_autopick",
                        lambda: "google/gemini-2.0-flash-exp:free")
    r = client.post("/settings/llm-provider",
                    json={"provider": "openrouter", "api_key": "sk-or-x",
                          "model": "auto", "send_image": False})
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and b["provider"] == "openrouter"
    assert b["resolved_model"] == "google/gemini-2.0-flash-exp:free"
    assert b["send_image"] is False
    assert pr.LMSTUDIO_BASE_URL == pr.OPENROUTER_BASE_URL
    assert pr.LLM_ALLOW_IMAGE is False

    g = client.get("/settings/llm-provider").json()
    assert g["provider"] == "openrouter"
    assert g["openrouter"]["resolved_model"] == "google/gemini-2.0-flash-exp:free"
    assert g["openrouter"]["has_key"] is True
    assert g["openrouter"]["send_image"] is False


def test_post_provider_openrouter_without_key_warns(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(server, "_openrouter_autopick", lambda: "")
    r = client.post("/settings/llm-provider",
                    json={"provider": "openrouter", "model": "auto"})
    b = r.json()
    assert b["ok"] is False
    assert b["warning"]


def test_set_llm_server_switches_provider_back_to_local(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_autopick", lambda: "m")
    client.post("/settings/llm-provider",
                json={"provider": "openrouter", "api_key": "k"})
    assert server._load_config()["provider"] == "openrouter"

    monkeypatch.setattr(server, "_probe_llm_url", lambda *a, **k: (True, 1))
    r = client.post("/settings/llm-server",
                    json={"server_type": "custom", "base_url": "http://127.0.0.1:1234"})
    b = r.json()
    assert b["ok"] is True
    assert b["base_url"] == "http://127.0.0.1:1234/v1"
    assert b["reachable"] is True
    assert server._load_config()["provider"] == "local"
    assert pr.LLM_API_KEY == "lmstudio"


def test_get_llm_server_returns_configured_not_effective(client):
    client.post("/settings/llm-server",
                json={"server_type": "custom", "base_url": "http://10.0.0.9:1234"})
    # Simulate a startup fallback having changed the LIVE url after the user saved.
    pr.LMSTUDIO_BASE_URL = "http://127.0.0.1:11434/v1"
    d = client.get("/settings/llm-server").json()
    assert d["base_url"] == "http://10.0.0.9:1234/v1"            # the user's choice
    assert d["effective_base_url"] == "http://127.0.0.1:11434/v1"


def test_models_openrouter_endpoint(client, monkeypatch):
    monkeypatch.setattr(server, "_fetch_openrouter_models", lambda *a, **k: list(_OR_SAMPLE))
    d = client.get("/models/openrouter").json()
    assert d["ok"] is True
    ids = [m["id"] for m in d["models"]]
    assert ids[0] == "google/gemini-2.0-flash-exp:free"
    assert all("gpt-4o" not in i for i in ids)


def test_models_available_openrouter_does_not_flood(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_autopick", lambda: "google/gemini:free")
    client.post("/settings/llm-provider",
                json={"provider": "openrouter", "api_key": "k", "model": "auto"})
    d = client.get("/models/available").json()
    assert d["provider"] == "openrouter"
    assert d["models"] == ["google/gemini:free"]          # just the resolved one


# ── OCR-text-only privacy gate in the pipeline ─────────────────────────────────

def test_ocr_text_only_skips_vision_rescue(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill")
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", False)
    monkeypatch.setattr(pr, "_extract_local_ocr", MagicMock(return_value=""))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=None))
    vision = MagicMock(return_value={"vendor": "x"})
    monkeypatch.setattr(pr, "_extract_with_model", vision)

    out = pr._extract_receipt_with_status(MagicMock(), img, None)
    assert out is None
    vision.assert_not_called()          # image never sent to the model


def test_image_mode_uses_vision_rescue(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "")
    monkeypatch.setattr(pr, "_active_distill_model", "distill")
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", True)
    monkeypatch.setattr(pr, "_extract_local_ocr", MagicMock(return_value=""))
    monkeypatch.setattr(pr, "_unified_distillation", MagicMock(return_value=None))
    vision = MagicMock(return_value={"vendor": "Shell", "amount": 10.0,
                                     "date": "2026-01-01", "category": "misc",
                                     "flags": []})
    monkeypatch.setattr(pr, "_extract_with_model", vision)

    out = pr._extract_receipt_with_status(MagicMock(), img, None)
    vision.assert_called_once()
    assert out is not None


def test_ocr_text_only_skips_llm_ocr_pass(monkeypatch, tmp_path):
    img = tmp_path / "r.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(pr, "_active_ocr_model", "ocrmodel")   # LLM-OCR cross-ref ON
    monkeypatch.setattr(pr, "_active_distill_model", "distill")
    monkeypatch.setattr(pr, "LLM_ALLOW_IMAGE", False)
    monkeypatch.setattr(pr, "_extract_local_ocr",
                        MagicMock(return_value="SHELL\nTOTAL $5.00\n01/01/2026"))
    raw = MagicMock(return_value="LLM TRANSCRIPTION")
    monkeypatch.setattr(pr, "_extract_raw_ocr", raw)
    monkeypatch.setattr(pr, "_unified_distillation",
                        MagicMock(return_value={"vendor": "Shell", "amount": 5.0,
                                                "date": "2026-01-01",
                                                "category": "fuel", "flags": []}))
    monkeypatch.setattr(pr, "_extract_with_model", MagicMock())

    out = pr._extract_receipt_with_status(MagicMock(), img, None)
    raw.assert_not_called()                       # LLM-OCR image pass suppressed
    assert out is not None
    assert out["_ocr_engine"] == "rapidocr"       # only the built-in OCR was used


# ── free router (openrouter/free) default + quick/reliable/vision steering ─────

def test_default_openrouter_model_is_free_router():
    assert server.OPENROUTER_FREE_ROUTER == "openrouter/free"
    assert server._openrouter_default_cfg()["model"] == "openrouter/free"


def test_extra_body_biases_quick_reliable_and_pins_vision_fallback():
    body = server._openrouter_extra_body({"models_fallback": ["a/x:free", "b/y:free"]})
    assert body["provider"]["sort"] == "throughput"       # "quick" — fastest providers
    assert body["provider"]["allow_fallbacks"] is True    # reliability — fail over
    assert body["models"] == ["a/x:free", "b/y:free"]      # vision fallback pinned
    assert "models" not in server._openrouter_extra_body({})   # none → no key


def test_extra_body_caps_models_at_openrouter_limit():
    """OpenRouter 400s a `models` array longer than 3 — cap it (also fixes an
    older config that persisted more) so requests don't silently 400 → offline."""
    over = [f"m{i}/v:free" for i in range(6)]
    body = server._openrouter_extra_body({"models_fallback": over})
    assert len(body["models"]) <= 3
    assert body["models"] == over[:3]


def test_score_prefers_quick_variant_same_family(monkeypatch):
    sample = [
        {"id": "google/gemini-pro-vision:free", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text", "image"]}, "context_length": 900000},
        {"id": "google/gemini-2.0-flash-exp:free", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text", "image"]}, "context_length": 1000000},
    ]
    monkeypatch.setattr(server, "_fetch_openrouter_models", lambda *a, **k: sample)
    fb = server._openrouter_vision_fallback()
    assert fb[0] == "google/gemini-2.0-flash-exp:free"     # the quick (flash) one wins


def test_apply_openrouter_sets_routing_body_and_router_model():
    cfg = {"provider": "openrouter",
           "openrouter": {"model": "openrouter/free", "resolved_model": "openrouter/free",
                          "send_image": True, "models_fallback": ["a/x:free"]}}
    server._apply_llm_server_config(cfg)
    assert pr._active_distill_model == "openrouter/free"
    assert pr.LLM_EXTRA_BODY["provider"]["sort"] == "throughput"
    assert pr.LLM_EXTRA_BODY["models"] == ["a/x:free"]
    # switching back to local clears the cloud routing body
    server._apply_llm_server_config({"provider": "local",
        "llm_server": {"server_type": "custom", "base_url": "http://127.0.0.1:1234"}})
    assert pr.LLM_EXTRA_BODY == {}


def test_post_provider_defaults_to_free_router(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_vision_fallback",
                        lambda: ["a/x:free", "b/y:free"])
    r = client.post("/settings/llm-provider",
                    json={"provider": "openrouter", "api_key": "k"})  # no model → default
    b = r.json()
    assert b["model"] == "openrouter/free"
    assert b["resolved_model"] == "openrouter/free"
    assert b["fallback_count"] == 2
    assert pr._active_distill_model == "openrouter/free"
    assert pr.LLM_EXTRA_BODY["models"] == ["a/x:free", "b/y:free"]
    assert pr.LLM_EXTRA_BODY["provider"]["sort"] == "throughput"


# ── first-run zero-click default (OPENROUTER_API_KEY present) ──────────────────

def test_first_run_defaults_to_free_router_when_env_key_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    server._first_run_provider_default()
    cfg = server._load_config()
    assert cfg["provider"] == "openrouter"
    assert cfg["openrouter"]["model"] == "openrouter/free"


def test_first_run_noop_without_env_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    server._first_run_provider_default()
    assert server._load_config() == {}            # nothing written


def test_first_run_does_not_override_explicit_local_choice(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    server._save_config({"provider": "local",
                         "llm_server": {"server_type": "custom",
                                        "base_url": "http://127.0.0.1:1234"}})
    server._first_run_provider_default()
    cfg = server._load_config()
    assert cfg["provider"] == "local"             # untouched
    assert "openrouter" not in cfg


def test_first_run_skips_when_local_server_already_configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    server._save_config({"llm_server": {"server_type": "custom", "base_url": ""}})
    server._first_run_provider_default()
    cfg = server._load_config()
    assert cfg.get("provider") != "openrouter"
    assert "openrouter" not in cfg


def test_startup_skips_initialize_models_for_openrouter(monkeypatch):
    monkeypatch.setattr(server, "_ensure_llm_reachable", lambda: None)
    server._save_config({"provider": "openrouter",
                         "openrouter": {"model": "openrouter/free",
                                        "models_fallback": ["x/y:free"]}})
    called = []
    monkeypatch.setattr(server, "initialize_models", lambda *a, **k: called.append(1))
    server._startup_models()
    assert called == []                           # local auto-select skipped


def test_startup_runs_initialize_models_for_local(monkeypatch):
    monkeypatch.setattr(server, "_ensure_llm_reachable", lambda: None)
    server._save_config({"provider": "local"})
    called = []
    monkeypatch.setattr(server, "initialize_models", lambda *a, **k: called.append(1))
    server._startup_models()
    assert called == [1]


# ── get_llm_provider "configured" flag (UI defaults the mode to OpenRouter) ─────

def test_provider_reports_unconfigured_on_fresh_config(client):
    """A fresh config has no explicit choice → the UI defaults to OpenRouter."""
    d = client.get("/settings/llm-provider").json()
    assert d["configured"] is False


def test_provider_reports_configured_after_explicit_choice(client):
    server._save_config({"provider": "local",
                         "llm_server": {"server_type": "custom", "base_url": ""}})
    d = client.get("/settings/llm-provider").json()
    assert d["configured"] is True


# ── /llm-server/availability — per-mode probes for the UI indicators + chip ─────

def test_availability_probes_each_mode(client, monkeypatch):
    monkeypatch.setattr(server, "_in_docker", lambda: False)

    def fake_probe(url, *a, **k):
        return (True, 2) if "1234" in url else (False, 0)
    monkeypatch.setattr(server, "_probe_llm_url", fake_probe)
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "sk-or-k")

    d = client.get("/llm-server/availability").json()
    assert d["host"]["reachable"] is True and d["host"]["models"] == 2
    assert d["docker"]["reachable"] is False
    assert d["openrouter"]["has_key"] is True
    assert d["active_mode"] in ("host", "docker", "openrouter")


def test_availability_active_mode_follows_provider(client, monkeypatch):
    monkeypatch.setattr(server, "_probe_llm_url", lambda *a, **k: (False, 0))
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "")
    server._save_config({"provider": "openrouter"})
    d = client.get("/llm-server/availability").json()
    assert d["active_mode"] == "openrouter"


# ── /settings/openrouter/test — full send → receive round-trip + logs ──────────

def test_openrouter_test_requires_active_provider(client):
    server._save_config({"provider": "local"})
    d = client.post("/settings/openrouter/test").json()
    assert d["ok"] is False
    assert "not the active provider" in d["error"].lower()


def test_openrouter_test_requires_key(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "")
    server._save_config({"provider": "openrouter",
                         "openrouter": {"model": "openrouter/free"}})
    d = client.post("/settings/openrouter/test").json()
    assert d["ok"] is False
    assert "key" in d["error"].lower()


def test_openrouter_test_round_trip_ok(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "sk-or-k")
    server._save_config({"provider": "openrouter",
                         "openrouter": {"model": "openrouter/free",
                                        "resolved_model": "openrouter/free"}})

    fake_client = MagicMock()
    msg = MagicMock(); msg.content = "OK"
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.model = "x/y:free"
    fake_client.chat.completions.create.return_value = resp
    monkeypatch.setattr(pr, "make_client", lambda: fake_client)

    d = client.post("/settings/openrouter/test").json()
    assert d["ok"] is True
    assert d["response_text"] == "OK"
    assert d["model_used"] == "x/y:free"
    assert any("round-trip succeeded" in line.lower() for line in d["logs"])


def test_openrouter_test_reports_failure_with_hint(client, monkeypatch):
    monkeypatch.setattr(server, "_openrouter_api_key", lambda: "sk-or-bad")
    server._save_config({"provider": "openrouter",
                         "openrouter": {"model": "openrouter/free"}})

    def boom():
        raise RuntimeError("Error code: 401 - invalid api key")
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = lambda **k: boom()
    monkeypatch.setattr(pr, "make_client", lambda: fake_client)

    d = client.post("/settings/openrouter/test").json()
    assert d["ok"] is False
    assert "401" in d["error"]
    assert "authentication" in (d.get("hint") or "").lower()
