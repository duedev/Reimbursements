"""The live-resizable worker concurrency gate.

Unlike a fixed Semaphore, the gate re-reads MAX_PARALLEL_REQUESTS on every
acquire, so the "process N at a time" slider takes effect within a running
batch.
"""
import threading
import time

import process_receipts as _pr
import server


def test_gate_caps_concurrent_acquires_at_current_limit(monkeypatch):
    monkeypatch.setattr(_pr, "MAX_PARALLEL_REQUESTS", 2)
    gate = server._ConcurrencyGate()

    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def worker():
        nonlocal active, peak
        gate.acquire()
        try:
            with lock:
                active += 1
                peak = max(peak, active)
            release.wait(2.0)
        finally:
            with lock:
                active -= 1
            gate.release()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.3)            # let the gate settle at its cap
    with lock:
        assert peak <= 2       # never more than the live limit run at once
    release.set()
    for t in threads:
        t.join(2.0)


def test_gate_admits_more_when_limit_raised(monkeypatch):
    monkeypatch.setattr(_pr, "MAX_PARALLEL_REQUESTS", 1)
    gate = server._ConcurrencyGate()

    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def worker():
        nonlocal active, peak
        gate.acquire()
        try:
            with lock:
                active += 1
                peak = max(peak, active)
            release.wait(2.0)
        finally:
            with lock:
                active -= 1
            gate.release()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    with lock:
        assert peak == 1       # only one admitted under the original cap

    # Raise the live limit mid-flight, just like the slider does.
    _pr.MAX_PARALLEL_REQUESTS = 3
    gate.bump()
    time.sleep(0.3)
    with lock:
        assert peak >= 3       # the raised cap admitted more without a restart

    release.set()
    for t in threads:
        t.join(2.0)


def test_gate_release_is_floored_at_zero():
    gate = server._ConcurrencyGate()
    gate.release()             # extra release must not drive the counter negative
    assert gate._active == 0
