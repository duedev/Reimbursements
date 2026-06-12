"""Structural tests for the tabbed UI layout (Phase 3 settings/UI cleanup)."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def page(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    with TestClient(server.app) as c:
        return c.get("/").text


def test_has_two_tabs(page):
    assert 'class="tab-bar"' in page
    assert "switchTab('workspace')" in page
    assert "switchTab('settings')" in page
    assert 'id="tab-workspace"' in page
    assert 'id="tab-settings"' in page


def test_save_button_relabelled(page):
    # The ambiguous generic "Save" is clarified to its real scope.
    assert "Save Folder Paths &amp; Schedule" in page
    assert ">Save<" not in page  # no bare "Save" button label remains


def test_processing_and_errors_combined(page):
    # Single combined card; the old standalone error-card is gone.
    assert "Processing &amp; Errors" in page
    assert 'id="error-section"' in page
    assert 'id="error-card"' not in page
    # progress-card is below kanban-card (user-requested layout order)
    kanban = page.find('id="kanban-card"')
    prog = page.find('id="progress-card"')
    err = page.find('id="error-section"')
    assert kanban < prog < err  # kanban first, then progress card containing error section


def test_insights_at_top_of_workspace(page):
    ws = page.find('id="tab-workspace"')
    ins = page.find('id="insights-card"')
    up = page.find('id="upload-card"')
    assert ws < ins < up  # insights is the first card, before upload
    # Still hidden until data exists
    insights_div = page[ins - 40:ins]
    assert "hidden" in insights_div


def test_details_and_models_in_settings_tab(page):
    st = page.find('id="tab-settings"')
    cfg = page.find('id="config-card"')
    setc = page.find('id="settings-card"')
    wsend = page.find("/tab-workspace")
    assert wsend < st < cfg < setc  # config + settings live in the settings tab
    assert "Details &amp; Models" in page


def test_no_duplicate_ids(page):
    import re, collections
    ids = re.findall(r'id="([^"]+)"', page)
    dups = [k for k, v in collections.Counter(ids).items() if v > 1]
    assert not dups, f"duplicate ids: {dups}"
