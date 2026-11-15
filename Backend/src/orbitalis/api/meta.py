"""Health, releases, presets and capabilities endpoints.

Purpose: Service discovery surface (SDD §8.2, §8.2b, §11.4). Optional
features report ``enabled: false`` rather than half-working.
Inputs: ``/api/v1`` meta requests.
Outputs: ``{data, meta}`` envelopes.
Called by: api.router.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Request

from orbitalis.api.deps import Services, services
from orbitalis.api.schemas import CapabilitiesView, PresetView, ReleaseView, ok
from orbitalis.assistant import assistant_readiness

router = APIRouter(tags=["meta"])


def _yaml_list(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [item for item in data.get(key, []) if isinstance(item, dict)]


@router.get("/health")
async def health(svc: Annotated[Services, Depends(services)]) -> dict:
    """Liveness plus gateway state (SDD §8.2)."""
    if svc.settings.offline:
        return ok({"status": "offline", "gateway": {"irsa": "offline"}})
    gw = await svc.gateway.health()
    status = "ok" if gw.get("irsa") == "ok" else "degraded"
    return ok({"status": status, "gateway": gw})


@router.get("/releases")
async def releases(svc: Annotated[Services, Depends(services)]) -> dict:
    items = _yaml_list(svc.settings.config_dir / "releases.yaml", "releases")
    views = [
        ReleaseView(
            id=str(r["id"]),
            label=str(r.get("label", r["id"])),
            enabled=bool(r.get("enabled", True)),
        )
        for r in items
    ]
    return ok({"releases": [v.model_dump(mode="json") for v in views]})


@router.get("/presets")
async def presets(svc: Annotated[Services, Depends(services)]) -> dict:
    items = _yaml_list(svc.settings.config_dir / "presets.yaml", "presets")
    views = [
        PresetView(
            name=str(p["name"]),
            ra=float(p["ra"]),
            dec=float(p["dec"]),
            radius_deg=p.get("radius_deg"),
            release=p.get("release"),
            thumbnail_url=p.get("thumbnail_url"),
            why=p.get("why"),
        )
        for p in items
        if "name" in p and "ra" in p and "dec" in p
    ]
    return ok({"presets": [v.model_dump(mode="json") for v in views]})


@router.get("/methods")
async def methods(svc: Annotated[Services, Depends(services)]) -> dict:
    """U-10: methods content served from the same config the code uses."""
    policy_path = svc.settings.config_dir / "stack_policy.yaml"
    policy = (yaml.safe_load(policy_path.read_text()) or {}) if policy_path.exists() else {}
    return ok(
        {
            "stack_policy": policy,
            "flag_policy": {
                "masked_except": ["SOURCE", "FULLSAMPLE"],
                "note": "SDD §7 default mask; GHOST_FPA conflict logged as D-30.",
            },
            "model": {"available": Path(svc.settings.model_path).exists()},
        }
    )


@router.get("/capabilities")
async def capabilities(request: Request, svc: Annotated[Services, Depends(services)]) -> dict:
    """Which optional features are on (SDD §8.2b, §11.4)."""
    model_exists = Path(svc.settings.model_path).exists()
    ranking_ready = model_exists and "candidate_ranking" in svc.jobs.registry.kinds()
    # AS.05: the assistant module exists now — report real readiness
    # (provider/key/model/base-URL/knowledge pack) and breaker state, never
    # a stale "not implemented".
    assistant_ready, assistant_reason = assistant_readiness(svc.settings)
    service = getattr(request.app.state, "assistant_service", None)
    if service is not None and service.degraded:
        assistant = {"enabled": False, "reason": "provider degraded"}
    else:
        assistant = {"enabled": assistant_ready, "reason": assistant_reason}
    view = CapabilitiesView(
        ranking={
            "enabled": ranking_ready,
            "model_version": None,
            "reason": (
                None
                if ranking_ready
                else (
                    "no model file"
                    if not model_exists
                    else "no candidate_ranking job handler registered"
                )
            ),
        },
        assistant=assistant,
        classification={"enabled": False, "reason": "not implemented (CORE.08/ML.06)"},
        jobs={
            "max_running": svc.jobs.max_running,
            "queue_max": svc.jobs.queue_max,
            "timeout_s": svc.jobs.timeout_s,
        },
    )
    return ok(view.model_dump(mode="json"))


__all__ = ["router"]
