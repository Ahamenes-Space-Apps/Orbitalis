"""Per-stream broadcaster with replay (SDD §6.4, §6.8).

Purpose: In-memory pub/sub for field, job and assistant streams. Assigns
monotonic ``id`` per stream for ``Last-Event-ID`` replay, keeps a bounded
ring buffer, and optionally persists each event through an injected callback
(the store's ``events`` table; this module imports nothing from the app).
Inputs: stream id, event type, data dict.
Outputs: event dicts ``{id, type, data}`` to subscribers.
Called by: pipeline runner, jobs runner (later), api SSE endpoints.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

PersistFn = Callable[[str, str, dict[str, Any]], "Awaitable[int | None] | int | None"]


class EventBus:
    """Broadcast events to per-stream subscribers with seq replay."""

    def __init__(self, *, ring_size: int = 1000, persist: PersistFn | None = None) -> None:
        self._ring_size = ring_size
        self._persist = persist
        self._seq: dict[str, int] = defaultdict(int)
        self._log: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=ring_size))
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    async def publish(
        self, stream_id: str, event_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Append, persist (if wired) and broadcast one event."""
        self._seq[stream_id] += 1
        event_id = self._seq[stream_id]
        if self._persist is not None:
            result = self._persist(stream_id, event_type, data)
            persisted = await result if asyncio.iscoroutine(result) else result
            # a persisted seq (store.append_event) becomes the event id so
            # Last-Event-ID replay lines up with the events table
            if persisted is not None:
                event_id = int(persisted)
        event = {"id": event_id, "type": event_type, "data": data}
        self._log[stream_id].append(event)
        for q in list(self._subs.get(stream_id, ())):
            q.put_nowait(event)
        return event

    def subscriber_count(self, stream_id: str) -> int:
        return len(self._subs.get(stream_id, ()))

    async def subscribe(
        self, stream_id: str, *, after_id: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield buffered events with id > after_id, then live events."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs[stream_id].add(q)
        try:
            for event in self._log.get(stream_id, ()):
                if event["id"] > after_id:
                    q.put_nowait(event)
            while True:
                yield await q.get()
        finally:
            self._subs.get(stream_id, set()).discard(q)


__all__ = ["EventBus"]
