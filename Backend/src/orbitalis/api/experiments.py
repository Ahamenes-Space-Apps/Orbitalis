"""Experiment endpoints: list per field, detail with trials.

Purpose: Read-only HTTP surface for injection-and-recovery experiments
(SDD §8.2b, §8.5). Results always carry params, metrics, seed and the
four outcome classes — never bare numbers.
Inputs: ``/api/v1/experiments`` and ``/fields/{id}/experiments`` requests.
Outputs: ``{data, meta}`` envelopes.
Called by: api.router.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from orbitalis.api.deps import Services, services
from orbitalis.api.fields import _require_field
from orbitalis.api.schemas import ExperimentView, ok
from orbitalis.errors import APIError
from orbitalis.store import get_experiment, get_field, list_experiments

router = APIRouter(tags=["experiments"])
fields_router = APIRouter(prefix="/fields", tags=["experiments"])


def _view(row: dict[str, Any], *, with_trials: bool = False) -> ExperimentView:
    return ExperimentView(
        experiment_id=row["experiment_id"],
        job_id=row.get("job_id"),
        field_id=row["field_id"],
        params=row.get("params") or {},
        metrics=row.get("metrics"),
        label=row.get("label"),
        created_at=row.get("created_at"),
        trials=row.get("trials") if with_trials else None,
    )


@fields_router.get("/{field_id}/experiments")
async def list_field_experiments(
    field_id: str, svc: Annotated[Services, Depends(services)]
) -> dict:
    _require_field(await get_field(svc.db, field_id))
    rows = await list_experiments(svc.db, field_id)
    return ok({"experiments": [_view(r).model_dump(mode="json") for r in rows]})


@router.get("/experiments/{experiment_id}")
async def get_experiment_detail(
    experiment_id: str, svc: Annotated[Services, Depends(services)]
) -> dict:
    row = await get_experiment(svc.db, experiment_id)
    if row is None:
        raise APIError("ORB-API-002", "experiment not found")
    return ok(_view(row, with_trials=True).model_dump(mode="json"))


__all__ = ["fields_router", "router"]
