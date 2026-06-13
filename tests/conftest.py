import sys
from pathlib import Path

import pytest

# Make the project root importable when pytest is run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_path_isolation: skip the autouse config/secrets/state path redirect",
    )


@pytest.fixture(autouse=True)
def _isolate_app_paths(request, tmp_path, monkeypatch):
    """Redirect every config / secrets / state file to a per-test temp dir.

    Without this, endpoints that call ``_save_config`` (and the secrets store)
    write to the real ``output/.app_config.json`` in the repo — polluting the
    developer's actual config and making tests order-dependent (a clean checkout
    with no ``output/`` dir would even fail). Tests that need a specific path
    still override these in their own fixtures (last setattr wins).
    """
    if request.node.get_closest_marker("no_path_isolation"):
        yield
        return
    cfg = tmp_path / ".app_config.json"
    state = tmp_path / ".app_state.json"
    secrets = tmp_path / ".app_secrets.json"
    for mod_name, attr, value in (
        ("process_receipts", "CONFIG_FILE", cfg),
        ("server",           "CONFIG_FILE", cfg),
        ("server",           "STATE_FILE",  state),
        ("watch_mode",       "CONFIG_FILE", cfg),
        ("app_secrets",      "SECRETS_FILE", secrets),
        ("scheduler",        "EXPORT_FOLDER", tmp_path / "export"),
    ):
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                mod = __import__(mod_name)
            except Exception:
                continue
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, value, raising=False)
    yield
