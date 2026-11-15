"""App factory, lifespan and job-handler registration (composition root).

Purpose: The only place concrete services are wired together (SDD §5.3).
Routers receive ``Services`` via dependency; nothing else constructs
gateway, pipeline, jobs or store objects.
Inputs: optional ``Settings``/``Services`` overrides (tests inject fakes).
Outputs: ``create_app()`` FastAPI application.
Called by: uvicorn ``orbitalis.main:app``, tests.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orbitalis.api.deps import Services
from orbitalis.api.router import api_router
from orbitalis.demo import load_bundle
from orbitalis.errors import register_error_handlers
from orbitalis.eventbus import EventBus
from orbitalis.gateway import IrsaGateway
from orbitalis.jobs import JobRunner
from orbitalis.pipeline import PipelineRunner
from orbitalis.research.search import moving_object_search
from orbitalis.research.validation import validation
from orbitalis.settings import Settings
from orbitalis.store import append_event, open_database


async def _build_services(settings: Settings) -> Services:
    """Wire the real stack. Only called when no Services were injected."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = await open_database(settings.db_path)
    # SDD §6.7/§7.7: pre-baked preset fields load through the same
    # repositories; a missing or malformed bundle must never block startup.
    bundle_dir = settings.demo_bundle_dir
    if not bundle_dir.is_absolute():
        # Prefer the CWD-relative path (repo root locally, /app in the
        # image); parents[2] only works for a src-layout checkout because
        # the Docker image installs the package non-editable.
        cwd_candidate = Path.cwd() / bundle_dir
        bundle_dir = (
            cwd_candidate
            if cwd_candidate.exists()
            else Path(__file__).resolve().parents[2] / bundle_dir
        )
    try:
        await load_bundle(db, bundle_dir)
    except Exception:
        logging.getLogger(__name__).exception("demo bundle load failed; continuing without it")
    gateway = IrsaGateway(
        concurrency=settings.irsa_concurrency,
        tap_timeout_s=settings.irsa_tap_timeout_s,
        tap_url=settings.irsa_tap_url,
    )
    # field events persist to the events table so SSE replay survives
    # restarts; the returned seq becomes the event id (SDD §8.3)
    bus = EventBus(persist=lambda sid, t, d: append_event(db, sid, t, d))
    pipeline = PipelineRunner(db, gateway, bus)
    jobs = JobRunner(
        db,
        max_running=settings.jobs_max_running,
        queue_max=settings.jobs_queue_max,
        timeout_s=settings.job_timeout_s,
        settings=settings,
    )
    # Research handlers registered at composition root; jobs module stays generic.
    jobs.register("moving_object_search", moving_object_search)
    jobs.register("validation", validation)
    return Services(
        settings=settings,
        db=db,
        pipeline=pipeline,
        jobs=jobs,
        gateway=gateway,
    )


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = app.state.services
        owned = svc is None
        if svc is None:
            svc = await _build_services(settings)
            app.state.services = svc
        await svc.jobs.sweep_stale()
        try:
            yield
        finally:
            # Close the lazily-created assistant service (LLM HTTP client)
            # if a chat request ever built one (AS.04 review).
            assistant_service = getattr(app.state, "assistant_service", None)
            if assistant_service is not None:
                await assistant_service.aclose()
            if owned:
                await svc.pipeline.close()
                await svc.jobs.close()
                await svc.db.close()
                if svc.gateway is not None:
                    await svc.gateway.aclose()

    app = FastAPI(title="ORBITALIS", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    # Always set the attribute: Starlette raises AttributeError on unset
    # state keys, so the lifespan must see ``None`` — not a missing attr.
    app.state.services = services
    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    return app


app = create_app()


__all__ = ["app", "create_app"]
