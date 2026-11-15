"""SSE helpers: event shaping and Last-Event-ID parsing.

Purpose: One place turns ``{id, type, data}`` events into
``text/event-stream`` frames (SDD §8.3, §8.6). Keep-alive pings are
handled by ``EventSourceResponse(ping=15)``.
Inputs: async iterators of event dicts from pipeline/jobs runners.
Outputs: ``EventSourceResponse`` ready to return from a route.
Called by: api routers (fields, jobs).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from sse_starlette import EventSourceResponse
from sse_starlette.sse import ServerSentEvent

KEEP_ALIVE_S = 15


def last_event_id(request_headers: Any) -> int:
    """Parse the ``Last-Event-ID`` header into a seq (0 = replay all)."""
    raw = request_headers.get("last-event-id")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


async def event_stream(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[ServerSentEvent]:
    """Yield SSE frames; closes after a ``done``/``error`` event."""
    async for ev in events:
        yield ServerSentEvent(
            data=json.dumps(ev["data"], separators=(",", ":")),
            event=ev["type"],
            id=str(ev["id"]),
        )
        if ev["type"] in ("done", "error"):
            return


def sse_response(events: AsyncIterator[dict[str, Any]]) -> EventSourceResponse:
    return EventSourceResponse(event_stream(events), ping=KEEP_ALIVE_S)


__all__ = ["KEEP_ALIVE_S", "event_stream", "last_event_id", "sse_response"]
