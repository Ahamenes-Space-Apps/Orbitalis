"""Composition-root service container and FastAPI dependencies.

Purpose: Routers never construct services; ``main.create_app`` fills
``app.state.services`` and routers pull it via :func:`services`.
Inputs: None at import.
Outputs: ``Services`` dataclass + ``services`` dependency.
Called by: api routers, main.py, tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Request

if TYPE_CHECKING:
    from orbitalis.jobs.runner import JobRunner
    from orbitalis.pipeline.runner import PipelineRunner
    from orbitalis.settings import Settings
    from orbitalis.store.db import Database


@dataclass
class Services:
    """Everything a router may touch. ``gateway`` is reachable only through
    pipeline/jobs/health — routers must not call IRSA themselves."""

    settings: Settings
    db: Database
    pipeline: PipelineRunner
    jobs: JobRunner
    # The IRSA client, held only so /health can report its state. Routers
    # must not call it for data — that is the pipeline's job.
    gateway: Any = None


def services(request: Request) -> Services:
    return request.app.state.services


__all__ = ["Services", "services"]
