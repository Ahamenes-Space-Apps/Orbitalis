"""Generic in-process job runner public surface.

Purpose: Exports for the job runner, handler registry, context and errors.
Inputs: None at import.
Outputs: JobRunner, JobContext, JobRef, HandlerRegistry, job errors.
Called by: api routers, main.py (composition root), research handlers.
"""

from orbitalis.jobs.errors import (
    JobCancelled,
    JobError,
    JobInterruptedError,
    JobNotFoundError,
    JobQueueFullError,
    JobTimeoutError,
)
from orbitalis.jobs.registry import Handler, HandlerRegistry
from orbitalis.jobs.runner import JobContext, JobRef, JobRunner, make_job_id
from orbitalis.jobs.schemas import JobEvent, JobProgress, JobStatus

__all__ = [
    "Handler",
    "HandlerRegistry",
    "JobCancelled",
    "JobContext",
    "JobError",
    "JobEvent",
    "JobInterruptedError",
    "JobNotFoundError",
    "JobProgress",
    "JobQueueFullError",
    "JobRef",
    "JobRunner",
    "JobStatus",
    "JobTimeoutError",
    "make_job_id",
]
