"""Per-batch benchmark log (compare LLM speed across runs)."""
import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(server, "initialize_models", lambda: None)
    monkeypatch.setattr(server, "_run_watcher", lambda: None)
    monkeypatch.setattr(server, "_run_stall_checker", lambda: None)
    monkeypatch.setattr(server, "_ensure_worker_alive", lambda: False)
    server._benchmarks.clear()
    server._worker_cancel.clear()
    with TestClient(server.app) as c:
        yield c
    server._benchmarks.clear()


def test_record_benchmark_fields_and_order():
    server._benchmarks.clear()
    server._worker_cancel.clear()
    e1 = server._record_benchmark(4, 8.0)
    assert e1["count"] == 4 and e1["total_seconds"] == 8.0 and e1["avg_seconds"] == 2.0
    assert {"ts", "distill_model", "ocr_model"} <= set(e1)
    e2 = server._record_benchmark(2, 10.0)
    assert server._benchmarks[0] is e2          # newest first
    server._benchmarks.clear()


def test_record_benchmark_ignores_empty_batch():
    server._benchmarks.clear()
    server._worker_cancel.clear()
    assert server._record_benchmark(0, 5.0) is None
    assert server._benchmarks == []


def test_record_benchmark_caps_history():
    server._benchmarks.clear()
    server._worker_cancel.clear()
    for _ in range(server.BENCH_MAX_ENTRIES + 25):
        server._record_benchmark(1, 1.0)
    assert len(server._benchmarks) == server.BENCH_MAX_ENTRIES
    server._benchmarks.clear()


def test_benchmarks_endpoint_and_clear(client):
    server._record_benchmark(3, 6.0)
    d = client.get("/benchmarks").json()
    assert d["benchmarks"] and d["benchmarks"][0]["count"] == 3
    assert client.post("/benchmarks/clear").json()["ok"] is True
    assert client.get("/benchmarks").json()["benchmarks"] == []


def test_benchmarks_persist_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUT_FOLDER", tmp_path)
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / ".app_state.json")
    server._benchmarks.clear()
    server._worker_cancel.clear()
    server._record_benchmark(2, 4.0)
    server._persist_state()
    server._benchmarks.clear()
    server._restore_state()
    assert len(server._benchmarks) == 1 and server._benchmarks[0]["count"] == 2
    server._benchmarks.clear()


# ── Insights ────────────────────────────────────────────────────────────────

def test_benchmark_insights_none_when_empty():
    assert server._benchmark_insights([]) is None
    assert server._benchmark_insights([{"count": 0, "total_seconds": 5}]) is None


def test_benchmark_insights_aggregates_totals_and_throughput():
    # 4 receipts in 8s (avg 2.0) + 2 receipts in 10s (avg 5.0) → 6 in 18s.
    entries = [
        {"count": 2, "total_seconds": 10.0, "avg_seconds": 5.0, "distill_model": "m"},
        {"count": 4, "total_seconds": 8.0, "avg_seconds": 2.0, "distill_model": "m"},
    ]
    ins = server._benchmark_insights(entries)
    assert ins["batches"] == 2
    assert ins["receipts"] == 6
    assert ins["total_seconds"] == 18.0
    assert ins["avg_per_receipt"] == 3.0          # 18 / 6
    assert ins["throughput_per_min"] == 20.0      # 6 * 60 / 18
    assert ins["fastest_batch_avg"] == 2.0
    assert ins["slowest_batch_avg"] == 5.0
    # Newest entry (first) has avg 5.0 vs overall 3.0 → +2.0 (slower)
    assert ins["recent_avg"] == 5.0
    assert ins["trend"] == 2.0


def test_benchmark_insights_per_model_comparison_marks_fastest():
    entries = [
        {"count": 2, "total_seconds": 20.0, "avg_seconds": 10.0, "distill_model": "slow-llm"},
        {"count": 2, "total_seconds": 4.0, "avg_seconds": 2.0, "distill_model": "fast-llm"},
    ]
    ins = server._benchmark_insights(entries)
    models = {m["model"]: m for m in ins["models"]}
    assert models["fast-llm"]["avg_seconds"] == 2.0
    assert models["slow-llm"]["avg_seconds"] == 10.0
    assert ins["fastest_model"] == "fast-llm"


def test_benchmarks_endpoint_includes_insights(client):
    server._record_benchmark(3, 6.0)
    d = client.get("/benchmarks").json()
    assert d["insights"] is not None
    assert d["insights"]["receipts"] == 3
    assert d["insights"]["avg_per_receipt"] == 2.0


# ── Per-step breakdown + CSV download ─────────────────────────────────────────

_RECEIPTS = [
    {"steps": [
        {"step": "local_ocr", "label": "OCR (built-in)", "ok": True,  "duration_s": 2.0},
        {"step": "distillation", "label": "Distillation", "ok": False, "duration_s": 0.1},
    ]},
    {"steps": [
        {"step": "local_ocr", "label": "OCR (built-in)", "ok": True,  "duration_s": 3.0},
    ]},
]


def test_aggregate_step_durations():
    rows = server._aggregate_step_durations(_RECEIPTS)
    by = {r["step"]: r for r in rows}
    assert by["local_ocr"]["count"] == 2
    assert by["local_ocr"]["total_seconds"] == 5.0
    assert by["local_ocr"]["failures"] == 0
    assert by["distillation"]["count"] == 1
    assert by["distillation"]["failures"] == 1
    assert by["distillation"]["total_seconds"] == 0.1


def test_record_benchmark_captures_steps():
    server._benchmarks.clear()
    server._worker_cancel.clear()
    e = server._record_benchmark(2, 5.1, _RECEIPTS)
    steps = {s["step"]: s for s in e["steps"]}
    assert steps["local_ocr"]["total_seconds"] == 5.0
    server._benchmarks.clear()


def test_benchmark_insights_step_totals():
    entries = [server._record_benchmark(2, 5.1, _RECEIPTS)]
    ins = server._benchmark_insights(entries)
    by = {s["step"]: s for s in ins["step_totals"]}
    assert by["local_ocr"]["total_seconds"] == 5.0
    # Sorted slowest-first → OCR (5.0s) before distillation (0.1s).
    assert ins["step_totals"][0]["step"] == "local_ocr"
    server._benchmarks.clear()


def test_benchmarks_download_csv(client):
    server._record_benchmark(2, 5.1, _RECEIPTS)
    r = client.get("/benchmarks/download")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert "step_total_seconds" in body          # header present
    assert "local_ocr" in body                   # a per-step row is included
    server._benchmarks.clear()
