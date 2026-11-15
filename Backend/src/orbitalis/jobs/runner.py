"""In-process job runner (SDD §6.8, FR-JOB-01).

Purpose: Queue, execute and persist long-running job kinds against the
``jobs`` table. Generic: knows statuses, progress, cancellation, timeout —
nothing about science. Handlers are registered by kind.
Inputs: ``Database`` handle, ``HandlerRegistry``, env-tunable limits.
Outputs: ``JobRef`` on submit; job rows via store repositories; SSE-shaped
event dicts via ``subscribe``.
Called by: api routers (submit/get/list/cancel/events), main.py (register).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from orbitalis.jobs.errors import (
    JobCancelled,
    JobError,
    JobNotFoundError,
    JobQueueFullError,
)
from orbitalis.jobs.registry import Handler, HandlerRegistry
from orbitalis.jobs.schemas import TERMINAL_STATUSES
from orbitalis.store import (
    Database,
    append_event,
    get_job,
    list_jobs,
    put_job,
    replay_events,
    replay_gap,
    requeue_job,
    update_job,
)

_PROGRESS_MIN_INTERVAL_S = 0.5  # ~2 persisted updates per second (JOB-07)


def make_job_id(kind: str, field_id: str | None, params: dict[str, Any], data_version: str) -> str:
    """Deterministic id: hash(kind, field_id, canonical params, data version)."""
    canonical = json.dumps(
        {"kind": kind, "field_id": field_id, "params": params, "data_version": data_version},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "j_" + hashlib.sha256(canonical.encode()).hexdigest()[:24]


@dataclass
class JobRef:
    """Handle returned by submit/get; ``result`` is populated for finished jobs."""

    job_id: str
    kind: str
    field_id: str | None
    status: str
    result: dict[str, Any] | None = None


class JobContext:
    """Handler-facing context: progress, cancellation, warnings."""

    def __init__(self, runner: JobRunner, job_id: str) -> None:
        self._runner = runner
        self.job_id = job_id
        self.params: dict[str, Any] = {}
        self._cancel = asyncio.Event()
        self._warnings: list[dict[str, str]] = []
        self._progress = {"done": 0, "total": 0, "stage": None}
        self._last_persist = 0.0
        self.persisted_count = 0  # test/observability hook

    @property
    def db(self) -> Database:
        """Database handle (explicit handler dependency; same as runner.db)."""
        return self._runner.db

    @property
    def settings(self) -> Any | None:
        """Optional runtime settings (explicit handler dependency)."""
        return getattr(self._runner, "settings", None)

    @property
    def warnings(self) -> list[dict[str, str]]:
        return list(self._warnings)

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    def warn(self, code: str, message: str) -> None:
        self._warnings.append({"code": code, "message": message})
        self._emit("warning", {"code": code, "message": message})

    async def progress(self, done: int, total: int, stage: str | None = None) -> None:
        self.check_cancelled()
        self._progress = {"done": done, "total": total, "stage": stage}
        payload = {**self._progress, "warnings": self._warnings}
        now = time.monotonic()
        if now - self._last_persist >= _PROGRESS_MIN_INTERVAL_S or self.persisted_count == 0:
            self._last_persist = now
            self.persisted_count += 1
            await update_job(self._runner.db, self.job_id, progress=payload)
            # events share the throttle: a search reporting per-trial progress
            # must not write an events row per call
            self._emit("progress", payload)

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        self._runner._publish(self.job_id, event_type, data)

    async def finish_progress(self) -> None:
        """Persist the latest progress once at job end (throttle may have skipped it)."""
        payload = {**self._progress, "warnings": self._warnings}
        await update_job(self._runner.db, self.job_id, progress=payload)


class JobRunner:
    """Asyncio-queue job runner persisted through the ``jobs`` table."""

    def __init__(
        self,
        db: Database,
        registry: HandlerRegistry | None = None,
        *,
        max_running: int | None = None,
        queue_max: int | None = None,
        timeout_s: float | None = None,
        settings: Any | None = None,
    ) -> None:
        self.db = db
        self.registry = registry or HandlerRegistry()
        self.max_running = max_running or int(os.environ.get("ORB_JOBS_MAX_RUNNING", "1"))
        self.queue_max = queue_max or int(os.environ.get("ORB_JOBS_QUEUE_MAX", "10"))
        self.timeout_s = timeout_s or float(os.environ.get("ORB_JOB_TIMEOUT_S", "600"))
        # Optional runtime settings (not used by jobs core); handlers read
        # e.g. search_max_work via ctx.settings without coupling jobs
        # to research modules.
        self.settings = settings
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, JobContext] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # -- registration -----------------------------------------------------

    def register(self, kind: str, handler: Handler) -> None:
        self.registry.register(kind, handler)

    # -- public API --------------------------------------------------------

    async def submit(
        self, kind: str, field_id: str | None, params: dict[str, Any], *, data_version: str = "v1"
    ) -> JobRef:
        job_id = make_job_id(kind, field_id, params, data_version)
        existing = await get_job(self.db, job_id)
        if existing is not None:
            if existing["status"] in ("failed", "cancelled"):
                # SDD 6.8: resubmitting identical params re-queues the job.
                await requeue_job(self.db, job_id)
                self._ensure_workers()
                self._queue.put_nowait(job_id)
                return JobRef(job_id, kind, field_id, "queued")
            return JobRef(job_id, kind, field_id, existing["status"], existing.get("result"))
        queued = sum(1 for j in await list_jobs(self.db) if j["status"] == "queued")
        if queued >= self.queue_max:
            raise JobQueueFullError(f"job queue full ({self.queue_max})")
        await put_job(self.db, job_id, kind, params, field_id=field_id)
        self._ensure_workers()
        self._queue.put_nowait(job_id)
        return JobRef(job_id, kind, field_id, "queued")

    async def get(self, job_id: str) -> JobRef:
        row = await get_job(self.db, job_id)
        if row is None:
            raise JobNotFoundError(f"job {job_id!r} not found")
        return JobRef(job_id, row["kind"], row["field_id"], row["status"], row.get("result"))

    async def list(self, field_id: str | None = None) -> list[JobRef]:
        return [
            JobRef(j["job_id"], j["kind"], j["field_id"], j["status"], j.get("result"))
            for j in await list_jobs(self.db, field_id)
        ]

    async def cancel(self, job_id: str) -> JobRef:
        row = await get_job(self.db, job_id)
        if row is None:
            raise JobNotFoundError(f"job {job_id!r} not found")
        status = row["status"]
        if status in TERMINAL_STATUSES:
            return JobRef(job_id, row["kind"], row["field_id"], status, row.get("result"))
        if status == "queued":
            # Worker re-reads the row and skips cancelled jobs.
            await self._publish_async(job_id, "done", {"status": "cancelled"})
            await update_job(self.db, job_id, status="cancelled")
            return JobRef(job_id, row["kind"], row["field_id"], "cancelled")
        ctx = self._running.get(job_id)
        if ctx is not None:
            ctx._cancel.set()
        return JobRef(job_id, row["kind"], row["field_id"], status)

    async def subscribe(self, job_id: str, *, after_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Event stream for one job: persisted replay, then live events.

        Ends after the terminal event; a job that already finished replays
        its stored events and returns immediately (Last-Event-ID style).
        """
        row = await get_job(self.db, job_id)
        if row is None:
            raise JobNotFoundError(f"job {job_id!r} not found")
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.setdefault(job_id, set()).add(q)
        seen: set[int] = set()
        try:
            last_type = None
            if await replay_gap(self.db, job_id, after_seq=after_id):
                yield {
                    "id": after_id,
                    "type": "warning",
                    "data": {
                        "code": "ORB-ST-003",
                        "message": "event history was pruned; resync via GET "
                        f"/api/v1/jobs/{job_id} for current state",
                    },
                }
            for ev in await replay_events(self.db, job_id, after_seq=after_id):
                seen.add(ev["seq"])
                last_type = ev["type"]
                yield {"id": ev["seq"], "type": ev["type"], "data": ev["data"]}
            if last_type in ("done", "error"):
                return
            status = (await get_job(self.db, job_id))["status"]
            if status in TERMINAL_STATUSES:
                return
            while True:
                event = await q.get()
                if event["id"] in seen:
                    continue
                yield event
                if event["type"] in ("done", "error"):
                    return
        finally:
            self._subscribers.get(job_id, set()).discard(q)

    async def sweep_stale(self) -> int:
        """Startup sweep: queued/running rows from a dead process -> failed ORB-JOB-004."""
        marked = 0
        for job in await list_jobs(self.db):
            if job["status"] in ("queued", "running"):
                await self._publish_async(job["job_id"], "error", {"code": "ORB-JOB-004"})
                await update_job(self.db, job["job_id"], status="failed", error_code="ORB-JOB-004")
                marked += 1
        return marked

    async def close(self) -> None:
        """Shut down workers; unfinished jobs are failed ORB-JOB-004 (restart sweep)."""
        self._closed = True
        for ctx in list(self._running.values()):
            ctx._cancel.set()
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        # Anything still non-terminal (in-flight or never dequeued) is interrupted.
        for job in await list_jobs(self.db):
            if job["status"] in ("queued", "running"):
                await self._publish_async(job["job_id"], "error", {"code": "ORB-JOB-004"})
                await update_job(self.db, job["job_id"], status="failed", error_code="ORB-JOB-004")
        self._running.clear()

    # -- internals ---------------------------------------------------------

    def _ensure_workers(self) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        while len(self._workers) < self.max_running:
            self._workers.append(asyncio.create_task(self._worker()))

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep the worker alive on unexpected dispatch errors (JOB-09).
                continue

    async def _execute(self, job_id: str) -> None:
        row = await get_job(self.db, job_id)
        if row is None or row["status"] != "queued":
            return  # cancelled while queued, or swept
        kind = row["kind"]
        try:
            handler = self.registry.get(kind)
        except KeyError as exc:
            await self._finish(
                job_id,
                "failed",
                error_code="ORB-SYS-001",
                event={"type": "error", "data": {"code": "ORB-SYS-001", "message": str(exc)}},
            )
            return

        await update_job(self.db, job_id, status="running")
        ctx = JobContext(self, job_id)
        ctx.params = row.get("params") or {}
        self._running[job_id] = ctx
        try:
            result = await asyncio.wait_for(self._invoke(handler, ctx), timeout=self.timeout_s)
            if ctx._cancel.is_set():
                await self._finish(
                    job_id, "cancelled", event={"type": "done", "data": {"status": "cancelled"}}
                )
            else:
                await ctx.finish_progress()
                await self._finish(
                    job_id,
                    "completed",
                    result=result or {},
                    event={"type": "done", "data": {"status": "completed"}},
                )
        except TimeoutError:
            # A sync handler runs in a thread that cannot be killed; ask it to
            # stop cooperatively at its next check_cancelled() and rely on the
            # store terminal guard to reject any late writes.
            ctx._cancel.set()
            await self._finish(
                job_id,
                "failed",
                error_code="ORB-JOB-003",
                event={"type": "error", "data": {"code": "ORB-JOB-003"}},
            )
        except JobCancelled:
            await self._finish(
                job_id, "cancelled", event={"type": "done", "data": {"status": "cancelled"}}
            )
        except JobError as exc:
            await self._finish(
                job_id,
                "failed",
                error_code=exc.code,
                event={"type": "error", "data": {"code": exc.code, "message": exc.message}},
            )
        except Exception as exc:  # handler bug: fail the job, keep the queue alive
            code = getattr(exc, "code", "ORB-SYS-001")
            await self._finish(
                job_id,
                "failed",
                error_code=code,
                event={"type": "error", "data": {"code": code, "message": str(exc)}},
            )
        finally:
            self._running.pop(job_id, None)

    @staticmethod
    async def _invoke(handler: Handler, ctx: JobContext) -> dict[str, Any]:
        if inspect.iscoroutinefunction(handler):
            return await handler(ctx)
        return await asyncio.to_thread(handler, ctx)

    async def _finish(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        event: dict[str, Any],
    ) -> None:
        # persist + broadcast the terminal event first: a subscriber that
        # replays between the two writes still sees done/error
        await self._publish_async(job_id, event["type"], event["data"])
        await update_job(self.db, job_id, status=status, result=result, error_code=error_code)

    def _publish(self, job_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Persist then broadcast; safe to call from handler threads."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._publish_async(job_id, event_type, data), loop)

    async def _publish_async(self, job_id: str, event_type: str, data: dict[str, Any]) -> None:
        seq = await append_event(self.db, job_id, event_type, data)
        event = {"id": seq, "type": event_type, "data": data}
        for q in list(self._subscribers.get(job_id, ())):
            q.put_nowait(event)


__all__ = ["JobContext", "JobRef", "JobRunner", "make_job_id"]
