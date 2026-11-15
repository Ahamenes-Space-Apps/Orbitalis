"""Job endpoints: submit (idempotent), poll, list, SSE, cancel.

Purpose: HTTP surface for the in-process job runner (SDD §8.2b, §8.6).
Routers validate, call services, shape responses — no science, no SQL.
Inputs: ``/api/v1/jobs`` requests.
Outputs: ``{data, meta}`` envelopes, SSE streams.
Called by: api.router.
"""

from __future__ import annotations

from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Request

from orbitalis.api.deps import Services, services
from orbitalis.api.schemas import JobSubmit, JobView, ok
from orbitalis.api.stream import last_event_id, sse_response
from orbitalis.errors import APIError
from orbitalis.research.search import select_frame_rows
from orbitalis.store import get_field, get_job, list_frames, list_jobs
from orbitalis_core.shiftstack import generate_sky_motion_grid

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _job_view(row: dict) -> JobView:
    return JobView(
        job_id=row["job_id"],
        kind=row["kind"],
        field_id=row.get("field_id"),
        status=row["status"],
        progress=row.get("progress"),
        result=row.get("result"),
        error_code=row.get("error_code"),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
    )


async def _require_job(svc: Services, job_id: str) -> dict:
    row = await get_job(svc.db, job_id)
    if row is None:
        raise APIError("ORB-API-002", "job not found")
    return row


@router.post("")
async def submit_job(body: JobSubmit, svc: Annotated[Services, Depends(services)]) -> dict:
    """Submit a job. Idempotent over (kind, field_id, canonical params)."""
    if body.kind not in svc.jobs.registry.kinds():
        raise APIError(
            "ORB-API-001",
            f"unknown job kind {body.kind!r}",
            detail={"kind": body.kind, "registered": svc.jobs.registry.kinds()},
        )
    if body.field_id is not None and await get_field(svc.db, body.field_id) is None:
        raise APIError("ORB-API-002", "field not found", detail={"field_id": body.field_id})
    ref = await svc.jobs.submit(body.kind, body.field_id, body.params)
    row = await _require_job(svc, ref.job_id)
    return ok(_job_view(row).model_dump(mode="json"))


@router.get("")
async def list_field_jobs(
    svc: Annotated[Services, Depends(services)], field_id: str | None = None
) -> dict:
    rows = await list_jobs(svc.db, field_id)
    return ok({"jobs": [_job_view(r).model_dump(mode="json") for r in rows]})


@router.post("/estimate")
async def estimate_job(body: JobSubmit, svc: Annotated[Services, Depends(services)]) -> dict:
    """U-07: pre-flight work estimate so the UI can warn before submit.

    Only defined for ``moving_object_search``; the estimate uses the same
    formula as the handler (``n_motion * n_frames * grid_h * grid_w``)
    after applying the handler's own ``frame_window`` selection
    (``select_frame_rows``). The background-variant epoch floor may still
    reduce the count further, so ``work`` remains an estimate.
    """
    if body.kind != "moving_object_search":
        raise APIError(
            "ORB-API-001",
            "work estimate is only defined for moving_object_search",
            detail={"kind": body.kind},
        )
    params: dict[str, Any] = body.params or {}
    field_id = body.field_id or params.get("field_id")
    if not field_id:
        raise APIError("ORB-API-001", "field_id is required for an estimate")
    field = await get_field(svc.db, field_id)
    if field is None:
        raise APIError("ORB-API-002", "field not found", detail={"field_id": field_id})

    refs = params.get("frame_refs")
    if isinstance(refs, list) and refs:
        n_frames = len(refs)
    else:
        # Same selection the handler runs (frame_window mjd/wavelength
        # bounds, epoch dedup, even thinning) so the estimate tracks the
        # real frame count on dense fields.
        rows = await list_frames(svc.db, field_id)
        selected, _applied = select_frame_rows(rows, params)
        n_frames = len(selected)

    motion_vectors = params.get("motion_vectors")
    if isinstance(motion_vectors, list) and motion_vectors:
        n_motion = len(motion_vectors)
    else:
        policy_path = svc.settings.config_dir / "stack_policy.yaml"
        policy = yaml.safe_load(policy_path.read_text()) if policy_path.exists() else {}
        span = float((policy or {}).get("max_time_span_days", 7.0))
        n_motion = len(generate_sky_motion_grid(span))

    spec = field.get("grid") or {}
    shape = spec.get("shape") or [0, 0]
    h, w = int(shape[0]), int(shape[1])
    work = n_motion * n_frames * h * w
    budget = int(svc.settings.search_max_work)
    return ok(
        {
            "kind": body.kind,
            "field_id": field_id,
            "work": work,
            "budget": budget,
            "ok": work <= budget,
            "frames": n_frames,
            "motion_vectors": n_motion,
        }
    )


@router.get("/{job_id}")
async def get_job_view(job_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    return ok(_job_view(await _require_job(svc, job_id)).model_dump(mode="json"))


@router.get("/{job_id}/events")
async def job_events(request: Request, job_id: str, svc: Annotated[Services, Depends(services)]):
    await _require_job(svc, job_id)
    return sse_response(svc.jobs.subscribe(job_id, after_id=last_event_id(request.headers)))


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    row = await _require_job(svc, job_id)
    if row["status"] in ("completed", "failed", "cancelled"):
        raise APIError("ORB-API-003", f"job already {row['status']}")
    await svc.jobs.cancel(job_id)
    return ok(_job_view(await _require_job(svc, job_id)).model_dump(mode="json"))


__all__ = ["router"]
