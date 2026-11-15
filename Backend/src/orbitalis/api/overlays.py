"""Overlay endpoints: known objects and Planet X hypothesis positions.

Purpose: Read-only overlay lists (SDD §8.2). The ``known`` overlay is
served from curated ``known_object`` blocks in ``config/presets.yaml``
when the field's coordinates match a preset (X-13); other kinds have no
source wired yet and return an honestly empty list.
Inputs: ``/api/v1/fields/{id}/overlays/{kind}`` requests.
Outputs: ``{data, meta}`` envelopes.
Called by: api.router.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends

from orbitalis.api.deps import Services, services
from orbitalis.api.fields import _require_field
from orbitalis.api.schemas import OverlayItem, OverlayView, ok
from orbitalis.overlays import contains_point, load_hypothesis, shape_center
from orbitalis.store import get_field

router = APIRouter(prefix="/fields", tags=["overlays"])

OverlayKind = Literal["known", "hypothesis"]

_COORD_TOL_DEG = 1e-6


def _load_presets(config_dir: Path) -> list[dict[str, Any]]:
    path = config_dir / "presets.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [p for p in data.get("presets", []) if isinstance(p, dict)]


def _known_items(svc: Services, field_row: dict[str, Any]) -> list[OverlayItem]:
    """Curated known objects for fields whose coordinates match a preset.

    The preset's ra/dec are stored verbatim as the field's params, so an
    exact match identifies the preset even though params do not record its
    name. A caller who overrode ra/dec gets different params and therefore
    no overlay — honest, not fabricated.
    """
    params = field_row.get("params") or {}
    ra, dec = params.get("ra"), params.get("dec")
    if ra is None or dec is None:
        return []
    for preset in _load_presets(svc.settings.config_dir):
        ko = preset.get("known_object")
        if not isinstance(ko, dict):
            continue
        if (
            abs(float(preset["ra"]) - float(ra)) < _COORD_TOL_DEG
            and abs(float(preset["dec"]) - float(dec)) < _COORD_TOL_DEG
        ):
            return [
                OverlayItem(
                    id=preset["name"],
                    name=ko.get("name"),
                    type=ko.get("type"),
                    ra=float(preset["ra"]),
                    dec=float(preset["dec"]),
                )
            ]
    return []


@router.get("/{field_id}/overlays/{kind}")
async def field_overlays(
    field_id: str,
    kind: OverlayKind,
    svc: Annotated[Services, Depends(services)],
    mjd: float | None = None,
) -> dict:
    row = _require_field(await get_field(svc.db, field_id))
    if kind == "known":
        items = _known_items(svc, row)
        source = "config/presets.yaml (curated known_object)" if items else None
        return ok(
            OverlayView(kind=kind, items=items, source=source, as_of=None).model_dump(mode="json")
        )
    if kind == "hypothesis":
        return _hypothesis_items(svc, row)
    # 'candidates' is retired as an overlay kind (SDD §8.2): candidates are
    # detections, shown via /candidates, not an overlay.
    return ok(OverlayView(kind=kind, items=[], source=None, as_of=None).model_dump(mode="json"))


def _hypothesis_items(svc: Services, field_row: dict[str, Any]) -> dict:
    """Published Planet-X probability regions from config/hypothesis.yaml.

    Each item is a hypothesis region (box/circle) or a text note — never an
    observation. ``contains_field`` reports whether the field centre lies
    inside the region; it is ``None`` for note-only items. A missing config
    keeps the honest empty response.
    """
    cfg = load_hypothesis(svc.settings.config_dir)
    if not cfg["regions"]:
        empty = OverlayView(kind="hypothesis", items=[], source=None, as_of=None)
        return ok(empty.model_dump(mode="json"))
    params = field_row.get("params") or {}
    fra, fdec = params.get("ra"), params.get("dec")
    fra = float(fra) if fra is not None else None
    fdec = float(fdec) if fdec is not None else None
    note = cfg.get("note") or (
        "hypothesis, not an observation; this tool makes no claim of detecting such an object"
    )
    items: list[OverlayItem] = []
    for region in cfg["regions"]:
        shape = region.get("shape")
        center = shape_center(shape)
        contained = (
            contains_point(shape, fra, fdec)
            if shape is not None and fra is not None and fdec is not None
            else None
        )
        items.append(
            OverlayItem(
                id=str(region["id"]),
                name=region.get("label"),
                type="hypothesis_region",
                ra=center[0] if center else None,
                dec=center[1] if center else None,
                shape=shape,
                contains_field=contained,
                citation=region.get("citation"),
                as_of=region.get("as_of"),
                detail=region.get("detail"),
            )
        )
    view = OverlayView(
        kind="hypothesis",
        items=items,
        source="config/hypothesis.yaml (published regions)",
        as_of=cfg.get("as_of"),
    )
    meta: dict[str, Any] = {"disclaimer": note}
    if cfg.get("scope_note"):
        meta["scope_note"] = cfg["scope_note"]
    return ok(view.model_dump(mode="json"), meta=meta)


__all__ = ["router"]
