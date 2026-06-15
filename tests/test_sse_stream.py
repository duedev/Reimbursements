"""SSE stream delivery — the live board/log channel must snapshot on connect and
hand off broadcast events immediately. The heartbeat/poll cadence was decoupled
so events no longer wait up to a full heartbeat before delivery.

These drive the stream's async generator directly (rather than over a real
socket) so they're deterministic and never block on the never-ending stream.
"""
import asyncio
import json

import server


def test_sse_tuning_is_sane():
    # Poll must be shorter than the heartbeat, else an idle stream would hold a
    # freshly-queued event for a whole heartbeat — the regression this fixes.
    assert 0 < server.SSE_POLL_SECS < server.SSE_HEARTBEAT_SECS


def _frame(raw: str) -> dict:
    assert raw.startswith("data:")
    return json.loads(raw[len("data:"):].strip())


def test_sse_snapshot_then_event_delivery():
    async def drive():
        before = list(server._subscribers)
        resp = await server.events_global()
        agen = resp.body_iterator
        try:
            # First frame on connect is always the full-state snapshot.
            snapshot = _frame(await agen.__anext__())
            assert snapshot["type"] == "full_state"
            assert "kanban" in snapshot and "pending" in snapshot

            # The connect registered exactly one new subscriber queue.
            new = [q for q in server._subscribers if q not in before]
            assert len(new) == 1
            q = new[0]

            # A queued event is delivered on the very next pull — the success
            # path never touches asyncio.sleep, so this can't hang.
            q.put_nowait({"type": "log", "message": "hello-from-test"})
            event = _frame(await agen.__anext__())
            assert event == {"type": "log", "message": "hello-from-test"}
        finally:
            await agen.aclose()

        # Closing the stream must unregister the subscriber (no leak).
        assert list(server._subscribers) == before

    asyncio.run(drive())
