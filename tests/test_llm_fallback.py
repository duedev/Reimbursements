"""Tests for the cloud LLM provider fallback chain (Gemini → Mistral → LM Studio).

The chain is a drop-in replacement for the OpenAI client used by the extraction
functions: it exposes ``.chat.completions.create(...)`` and tries each configured
provider in order, substituting that provider's own model id, until one succeeds.
"""
import copy

import pytest

import process_receipts as pr


@pytest.fixture(autouse=True)
def _restore_providers():
    """Snapshot/restore the mutable provider chain + active model around each test."""
    saved = copy.deepcopy(pr._CLOUD_PROVIDERS)
    saved_model = pr._active_distill_model
    yield
    pr._CLOUD_PROVIDERS[:] = saved
    pr._active_distill_model = saved_model


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeCompletions:
    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._fn(kwargs)


class _FakeChat:
    def __init__(self, comp):
        self.completions = comp


class _FakeClient:
    """Minimal client whose create() runs `fn` (return a sentinel or raise)."""
    def __init__(self, fn):
        self.completions = _FakeCompletions(fn)
        self.chat = _FakeChat(self.completions)


def _provider(name, kind, model, fn):
    return {"name": name, "kind": kind, "model": model, "client": _FakeClient(fn)}


# ── Provider selection / configuration ────────────────────────────────────────

def test_active_requires_key_and_model():
    pr.configure_providers([
        {"name": "gemini",  "api_key": "k", "model": "gemini-2.5-flash-lite", "enabled": True},
        {"name": "mistral", "api_key": "",  "model": "pixtral-12b-latest",    "enabled": True},
    ])
    assert pr.active_provider_names() == ["gemini", "lmstudio"]


def test_enabled_flag_removes_from_chain():
    pr.configure_providers([
        {"name": "gemini",  "api_key": "k", "model": "g", "enabled": False},
        {"name": "mistral", "api_key": "k", "model": "m", "enabled": True},
    ])
    assert pr.active_provider_names() == ["mistral", "lmstudio"]


def test_no_keys_means_local_only():
    pr.configure_providers([
        {"name": "gemini",  "api_key": "", "enabled": True},
        {"name": "mistral", "api_key": "", "enabled": True},
    ])
    assert pr.active_provider_names() == ["lmstudio"]


def test_configure_providers_merges_partial_fields():
    pr.configure_providers([{"name": "gemini", "api_key": "k", "model": "m1"}])
    pr.configure_providers([{"name": "gemini", "model": "m2"}])   # key preserved
    gemini = next(p for p in pr._CLOUD_PROVIDERS if p["name"] == "gemini")
    assert gemini["api_key"] == "k"
    assert gemini["model"] == "m2"


def test_unknown_provider_ignored():
    pr.configure_providers([{"name": "bogus", "api_key": "k", "model": "m"}])
    assert "bogus" not in pr.active_provider_names()


def test_provider_status_hides_keys():
    pr.configure_providers([
        {"name": "gemini",  "api_key": "secret", "model": "g", "enabled": True},
        {"name": "mistral", "api_key": "",       "model": "m", "enabled": True},
    ])
    status = pr.provider_status()
    assert [s["name"] for s in status] == ["gemini", "mistral", "lmstudio"]
    assert all("api_key" not in s for s in status)         # never leak the key
    gemini = next(s for s in status if s["name"] == "gemini")
    mistral = next(s for s in status if s["name"] == "mistral")
    assert gemini["has_key"] is True and gemini["active"] is True
    assert mistral["has_key"] is False and mistral["active"] is False
    assert status[-1]["name"] == "lmstudio" and status[-1]["active"] is True


# ── Request sanitisation ──────────────────────────────────────────────────────

def test_sanitize_passes_everything_for_lmstudio():
    kwargs = {"messages": [], "temperature": 0.0, "max_tokens": 10,
              "frequency_penalty": 0.15, "extra_body": {"repeat_penalty": 1.1}}
    assert pr._sanitize_create_kwargs("lmstudio", dict(kwargs)) == kwargs


def test_sanitize_strips_lmstudio_extras_for_cloud():
    kwargs = {"messages": [], "temperature": 0.0, "max_tokens": 10,
              "frequency_penalty": 0.15, "extra_body": {"repeat_penalty": 1.1}}
    out = pr._sanitize_create_kwargs("openai", dict(kwargs))
    assert out == {"messages": [], "temperature": 0.0, "max_tokens": 10}
    assert "extra_body" not in out and "frequency_penalty" not in out


# ── Fallback behaviour ────────────────────────────────────────────────────────

def test_first_success_wins_and_uses_provider_model():
    gemini = _provider("gemini", "openai", "gemini-2.5-flash-lite",
                       lambda kw: "GEMINI_RESP")
    local = _provider("lmstudio", "lmstudio", None,
                      lambda kw: pytest.fail("local should not be called"))
    comp = pr._FallbackCompletions([gemini, local])

    out = comp.create(model="local-llm", messages=[{"x": 1}], extra_body={"a": 1})
    assert out == "GEMINI_RESP"
    # Gemini was called with ITS model, and the LM-Studio-only extra_body stripped.
    assert gemini["client"].completions.calls[0]["model"] == "gemini-2.5-flash-lite"
    assert "extra_body" not in gemini["client"].completions.calls[0]


def test_falls_through_to_local_on_cloud_errors():
    def boom(kw):
        raise RuntimeError("rate limited")
    gemini = _provider("gemini", "openai", "g", boom)
    mistral = _provider("mistral", "openai", "m", boom)
    local = _provider("lmstudio", "lmstudio", None, lambda kw: "LOCAL_RESP")
    comp = pr._FallbackCompletions([gemini, mistral, local])

    out = comp.create(model="local-llm", messages=[], extra_body={"repeat_penalty": 1.1})
    assert out == "LOCAL_RESP"
    # The local provider used the CALLER's model (its own model is None) and kept
    # the LM Studio extras.
    local_call = local["client"].completions.calls[0]
    assert local_call["model"] == "local-llm"
    assert local_call["extra_body"] == {"repeat_penalty": 1.1}


def test_reraises_last_error_when_all_fail():
    def boom(kw):
        raise RuntimeError("down")
    gemini = _provider("gemini", "openai", "g", boom)
    local = _provider("lmstudio", "lmstudio", None, boom)
    comp = pr._FallbackCompletions([gemini, local])
    with pytest.raises(RuntimeError, match="down"):
        comp.create(model="local-llm", messages=[])


def test_provider_skipped_when_no_model_resolvable():
    # lmstudio with model=None and caller passes no model → skip, try next.
    local = _provider("lmstudio", "lmstudio", None,
                      lambda kw: pytest.fail("should be skipped"))
    after = _provider("gemini", "openai", "g", lambda kw: "OK")
    comp = pr._FallbackCompletions([local, after])
    assert comp.create(messages=[]) == "OK"


# ── Client construction ───────────────────────────────────────────────────────

def test_make_llm_client_plain_when_no_cloud():
    pr.configure_providers([
        {"name": "gemini", "api_key": "", "enabled": True},
        {"name": "mistral", "api_key": "", "enabled": True},
    ])
    client = pr.make_llm_client()
    assert not isinstance(client, pr._FallbackClient)
    assert hasattr(client.chat.completions, "create")  # still OpenAI-shaped


def test_make_llm_client_builds_chain_with_cloud():
    pr.configure_providers([
        {"name": "gemini",  "api_key": "k", "model": "gemini-2.5-flash-lite", "enabled": True},
        {"name": "mistral", "api_key": "", "enabled": True},
    ])
    client = pr.make_llm_client()
    assert isinstance(client, pr._FallbackClient)
    names = [p["name"] for p in client.chat.completions._providers]
    assert names == ["gemini", "lmstudio"]


# ── Server endpoint round-trip ────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(server, "_startup_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    with TestClient(server.app) as c:
        yield c


def test_endpoint_get_lists_chain(client):
    names = [p["name"] for p in client.get("/settings/llm-providers").json()["providers"]]
    assert names[-1] == "lmstudio"
    assert "gemini" in names and "mistral" in names


def test_endpoint_post_persists_and_restores(client):
    import app_secrets
    import server
    r = client.post("/settings/llm-providers", json={
        "gemini":  {"api_key": "sekret", "model": "gemini-2.5-flash-lite", "enabled": True},
        "mistral": {"enabled": False},
    })
    assert r.status_code == 200 and r.json()["ok"]
    status = {p["name"]: p for p in r.json()["providers"]}
    assert status["gemini"]["active"] is True and status["gemini"]["has_key"] is True
    assert status["mistral"]["enabled"] is False
    # The key lives in the (non-synced) secrets store, never the config file.
    assert app_secrets.get_secret("gemini_api_key") == "sekret"
    assert "sekret" not in (server.CONFIG_FILE.read_text() if server.CONFIG_FILE.exists() else "")
    # Wipe runtime state, then restoring from disk + secrets rebuilds the chain.
    pr.configure_providers([{"name": "gemini", "api_key": "", "enabled": True},
                            {"name": "mistral", "api_key": "", "enabled": True}])
    server._apply_provider_config()
    assert pr.active_provider_names() == ["gemini", "lmstudio"]


def test_endpoint_blank_key_keeps_existing(client):
    import app_secrets
    client.post("/settings/llm-providers", json={"gemini": {"api_key": "abc", "model": "g"}})
    client.post("/settings/llm-providers", json={"gemini": {"model": "g2"}})  # blank key
    assert app_secrets.get_secret("gemini_api_key") == "abc"
