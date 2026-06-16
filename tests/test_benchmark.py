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
