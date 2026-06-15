"""Edge-case hardening tests.

Covers the defensive safeguards added to keep one malformed input — a weird
LLM reply, a hand-corrupted config, a non-finite amount, a planted symlink, a
pathological filename collision, or an oversized/empty upload — from crashing
the pipeline, poisoning the totals, or leaking a file.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import process_receipts as _pr
import server


# ── 1. LLM JSON that parses but isn't an object ────────────────────────────────

@pytest.mark.parametrize("raw", ["null", "[1, 2, 3]", "42", '"just a string"',
                                 "not json at all", "", "true"])
def test_parse_llm_record_rejects_non_objects(raw):
    # A valid-but-non-object JSON payload must come back as None (caller retries
    # / falls back) instead of raising on result["flags"].
    assert _pr._parse_llm_record(raw) is None


def test_parse_llm_record_accepts_object_and_normalises():
    out = _pr._parse_llm_record('{"vendor": "Shell", "summary": "fuel", "flags": ["dupe"]}')
    assert out["vendor"] == "Shell"
    assert out["ai_summary"] == "fuel"                 # summary → ai_summary
    assert out["flags"] == [{"flag": "dupe"}]          # bare string → dict


def test_parse_llm_record_handles_markdown_fence():
    out = _pr._parse_llm_record('```json\n{"vendor": "BP"}\n```')
    assert out["vendor"] == "BP" and out["flags"] == []


def _fake_client(*contents):
    it = iter(contents)
    create = lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=next(it)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_unified_distillation_survives_non_dict_reply(monkeypatch):
    monkeypatch.setattr(_pr, "_active_distill_model", "fake-model")
    # First reply + retry are both bare "null" — must return None, never raise.
    client = _fake_client("null", "null")
    assert _pr._unified_distillation(client, "RECEIPT TEXT") is None


def test_unified_distillation_parses_object(monkeypatch):
    monkeypatch.setattr(_pr, "_active_distill_model", "fake-model")
    client = _fake_client('{"vendor": "Costco", "amount": 12.5}')
    out = _pr._unified_distillation(client, "RECEIPT TEXT")
    assert out["vendor"] == "Costco" and out["amount"] == 12.5


# ── 2. Corrupt config file ─────────────────────────────────────────────────────

@pytest.mark.parametrize("blob", ["null", "[1, 2]", "123", '"hi"', "{not valid"])
def test_load_config_never_returns_non_dict(blob):
    server.CONFIG_FILE.write_text(blob)
    assert server._load_config() == {}


def test_load_config_returns_valid_dict():
    server.CONFIG_FILE.write_text('{"default_employee": "Sam"}')
    assert server._load_config() == {"default_employee": "Sam"}


# ── 3. Non-finite amounts ──────────────────────────────────────────────────────

@pytest.fixture()
def results_client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    server._results.clear()
    server._kanban.clear()
    server._results.append({
        "vendor": "Shell", "date": "2026-05-01", "amount": 45.20,
        "category": "fuel", "_category": "fuel", "_file": "IMG_1.jpg",
        "_new_filename": "fuel_05-01-26_shell.jpg",
    })
    with TestClient(server.app) as c:
        yield c
    server._results.clear()
    server._kanban.clear()


@pytest.mark.parametrize("bad", ["inf", "-inf", "Infinity", "nan"])
def test_update_amount_rejects_non_finite(results_client, bad):
    r = results_client.post("/results/update",
                            json={"filename": "IMG_1.jpg", "field": "amount", "value": bad})
    assert r.status_code == 400
    assert server._results[0]["amount"] == 45.20        # unchanged


@pytest.mark.parametrize("bad", ["inf", "nan", "-Infinity"])
def test_add_manual_coerces_non_finite_to_zero(results_client, bad):
    r = results_client.post("/results/add-manual", json={
        "filename": "manual_x.jpg", "vendor": "Depot", "date": "2026-05-02",
        "amount": bad, "category": "misc", "summary": "",
        "review_required": False, "approved": False})
    assert r.status_code == 200
    rec = next(x for x in server._results if x["_file"] == "manual_x.jpg")
    assert rec["amount"] == 0.0


# ── 4 & 6. Symlink protection + upload limits (shared folder fixture) ──────────

@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    out        = tmp_path / "out"
    images     = out / "receipts"
    processing = out / "processing"
    intake     = tmp_path / "intake"
    for d in (images, processing, intake):
        d.mkdir(parents=True)
    monkeypatch.setattr(server, "OUT_FOLDER", out)
    monkeypatch.setattr(server, "IMAGES_FOLDER", images)
    monkeypatch.setattr(server, "PROCESSING_FOLDER", processing)
    monkeypatch.setattr(server, "INTAKE_FOLDER", intake)
    monkeypatch.setattr(server, "STATE_FILE", out / ".app_state.json")
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    server._work_queue.clear()
    server._item_cache.clear()
    server._seen_intake.clear()
    server._kanban.clear()
    with TestClient(server.app) as c:
        yield c, tmp_path, images
    server._work_queue.clear()
    server._item_cache.clear()
    server._seen_intake.clear()
    server._kanban.clear()


def test_receipt_image_serves_real_file(app_env):
    client, _root, images = app_env
    (images / "real.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    assert client.get("/receipt-image", params={"filename": "real.jpg"}).status_code == 200


def test_receipt_image_refuses_symlink_escape(app_env):
    client, root, images = app_env
    secret = root / "secret.txt"
    secret.write_text("top secret outside the working folders")
    link = images / "leak.jpg"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    # A planted symlink must not be served, even though it 'exists'.
    assert client.get("/receipt-image", params={"filename": "leak.jpg"}).status_code == 404


def test_queue_add_skips_empty_upload(app_env):
    client, _root, _images = app_env
    r = client.post("/queue/add",
                    files=[("files", ("blank.jpg", b"", "image/jpeg"))])
    body = r.json()
    assert "blank.jpg" in body["skipped"]
    assert "blank.jpg" not in body["queued"]


def test_queue_add_skips_oversized_upload(app_env, monkeypatch):
    client, _root, _images = app_env
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 8)
    r = client.post("/queue/add",
                    files=[("files", ("huge.jpg", b"x" * 64, "image/jpeg"))])
    body = r.json()
    assert "huge.jpg" in body["skipped"]
    assert "huge.jpg" not in body["queued"]


def test_queue_add_accepts_normal_upload(app_env, monkeypatch):
    client, _root, _images = app_env
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
    r = client.post("/queue/add",
                    files=[("files", ("ok.jpg", b"\xff\xd8\xff\xe0" + b"x" * 32, "image/jpeg"))])
    body = r.json()
    assert "ok.jpg" in body["queued"]
    assert "ok.jpg" not in body["skipped"]


# ── 5. Bounded filename-collision loop ─────────────────────────────────────────

def test_rename_resolves_collision_with_numbered_suffix(tmp_path):
    data = {"date": "2026-05-01", "vendor": "Shell"}
    # Occupy the primary name and the _2 variant so the loop must advance.
    (tmp_path / "fuel_05-01-26_shell.jpg").write_bytes(b"a")
    (tmp_path / "fuel_05-01-26_shell_2.jpg").write_bytes(b"b")
    src = tmp_path / "src.jpg"
    src.write_bytes(b"c")

    out = _pr.rename_receipt_image(src, data, "fuel", dest_dir=tmp_path)
    assert out.name == "fuel_05-01-26_shell_3.jpg"
    assert out.exists() and not src.exists()
