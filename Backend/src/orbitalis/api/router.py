"""Top-level API router mounting the /api/v1 routers.

Purpose: Single mount point for the versioned API surface.
Inputs: None at import.
Outputs: ``api_router`` included by ``main.create_app``.
Called by: orbitalis.main.
"""

from __future__ import annotations

from fastapi import APIRouter

from orbitalis.api import assistant, candidates, experiments, fields, jobs, meta, overlays

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(meta.router)
api_router.include_router(fields.router)
# field-scoped sub-routers (candidates, experiments) mount under /fields
api_router.include_router(candidates.fields_router)
api_router.include_router(experiments.fields_router)
api_router.include_router(candidates.router)
api_router.include_router(experiments.router)
api_router.include_router(overlays.router)
api_router.include_router(jobs.router)
api_router.include_router(assistant.router)

__all__ = ["api_router"]
