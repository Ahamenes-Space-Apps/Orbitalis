"""Kind-to-handler registry.

Purpose: Maps job kinds to handler callables; filled by main.py (composition root).
Inputs: kind string, handler callable.
Outputs: Registered handlers looked up by the runner.
Called by: main.py at startup; jobs.runner at dispatch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orbitalis.jobs.runner import JobContext

Handler = Callable[["JobContext"], Awaitable[dict[str, Any]] | dict[str, Any]]


class HandlerRegistry:
    """Registry of job-kind handlers. ``jobs`` stays science-agnostic."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        if kind in self._handlers:
            raise ValueError(f"job kind {kind!r} already registered")
        self._handlers[kind] = handler

    def get(self, kind: str) -> Handler:
        try:
            return self._handlers[kind]
        except KeyError:
            raise KeyError(f"no handler registered for job kind {kind!r}") from None

    def kinds(self) -> list[str]:
        return sorted(self._handlers)


__all__ = ["Handler", "HandlerRegistry"]
