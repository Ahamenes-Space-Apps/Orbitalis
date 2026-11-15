"""Job-runner models.

Purpose: Status vocabulary and event/progress payload shapes for jobs.
Inputs: None at import.
Outputs: Pydantic models used by the runner and (later) the API layer.
Called by: jobs.runner, api routers.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class JobProgress(BaseModel):
    """Persisted progress payload (jobs.progress_json)."""

    done: int = 0
    total: int = 0
    stage: str | None = None
    warnings: list[dict[str, str]] = Field(default_factory=list)


class JobEvent(BaseModel):
    """One SSE event on a job stream (id supports Last-Event-ID later)."""

    id: int
    type: Literal["snapshot", "progress", "warning", "done", "error"]
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = ["JobEvent", "JobProgress", "JobStatus", "TERMINAL_STATUSES"]
