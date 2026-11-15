"""Job-runner error types carrying SDD Appendix B codes.

Purpose: Typed errors for the in-process job runner.
Inputs: None at import.
Outputs: JobError subclasses; ``code`` is the ORB-JOB-* string stored on the row.
Called by: runner (raise), api/errors.py (map to HTTP later).
"""

from __future__ import annotations


class JobError(Exception):
    """Base job-runner error; ``code`` follows SDD Appendix B."""

    code = "ORB-JOB-001"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JobNotFoundError(JobError):
    code = "ORB-JOB-001"


class JobQueueFullError(JobError):
    code = "ORB-JOB-002"


class JobTimeoutError(JobError):
    code = "ORB-JOB-003"


class JobInterruptedError(JobError):
    code = "ORB-JOB-004"


class JobCancelled(Exception):
    """Internal control-flow signal; not a JobError (cancel is not a failure)."""


__all__ = [
    "JobCancelled",
    "JobError",
    "JobInterruptedError",
    "JobNotFoundError",
    "JobQueueFullError",
    "JobTimeoutError",
]
