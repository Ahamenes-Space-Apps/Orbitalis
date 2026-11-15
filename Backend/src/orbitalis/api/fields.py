"""Field endpoints: create/attach, summary, SSE, cancel, frames, groups.

Purpose: HTTP surface for the field builder (SDD §8.2, §8.3). Routers
validate, call services, shape responses — no science, no SQL, no IRSA.
Inputs: ``/api/v1/fields`` requests.
Outputs: ``{data, meta}`` envelopes, PNG/NPY binaries, SSE streams.
Called by: api.router.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import yaml
from astropy.time import Time
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from PIL import Image

from orbitalis.api.deps import Services, services
from orbitalis.api.photometry import (
    measure_points,
    method_dict,
    pixel_solid_angle_sr,
    spectrum_bins,
    wcs_from_grid_spec,
)
from orbitalis.api.schemas import (
    FieldCreate,
    FieldView,
    FrameMetaView,
    GridView,
    GroupView,
    LightCurveView,
    SpectrumView,
    ok,
)
from orbitalis.api.stream import last_event_id, sse_response
from orbitalis.errors import APIError
from orbitalis.pipeline import FieldParams
from orbitalis.store import (
    decode_frame_array,
    get_derived,
    get_field,
    get_frame,
    list_frames,
    list_groups,
    replay_events,
    replay_gap,
)
from orbitalis_core.stretch import StretchParams, apply_stretch, fit_stretch

router = APIRouter(prefix="/fields", tags=["fields"])


def _grid_view(spec: dict | None) -> GridView | None:
    """U-05: expose the stored common-grid spec as a typed view."""
    if not spec:
        return None
    shape = spec.get("shape") or [None, None]
    return GridView(
        ra=float(spec["ra"]),
        dec=float(spec["dec"]),
        pixel_scale_arcsec=float(spec["pixel_scale_arcsec"]),
        rotation_deg=0.0,
        width=int(shape[1]),
        height=int(shape[0]),
    )


def _field_view(row: dict, frames_done: int = 0) -> FieldView:
    return FieldView(
        field_id=row["field_id"],
        status=row["status"],
        params=row.get("params") or {},
        grid=_grid_view(row.get("grid")),
        frames_done=frames_done,
        error_code=row.get("error_code"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _require_field(row: dict | None) -> dict:
    if row is None:
        raise APIError("ORB-API-002", "field not found")
    return row


def _load_presets(config_dir: Path) -> dict[str, dict[str, Any]]:
    path = config_dir / "presets.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {p["name"]: p for p in data.get("presets", []) if isinstance(p, dict) and "name" in p}


def _preset_params(
    svc: Services, name: str | None
) -> tuple[float, float, float | None, str | None, dict[str, Any]] | None:
    """Resolve a named preset to (ra, dec, radius_deg, release, field_over)."""
    if name is None:
        return None
    presets = _load_presets(svc.settings.config_dir)
    preset = presets.get(name)
    if preset is None:
        raise APIError("ORB-API-001", f"unknown preset {name!r}", detail={"preset": name})
    return (
        float(preset["ra"]),
        float(preset["dec"]),
        preset.get("radius_deg"),
        preset.get("release"),
        preset.get("field") or {},
    )


def _mjd_range(time_range: list[str] | None) -> tuple[float | None, float | None]:
    """ISO-8601 ``[start, end]`` dates -> MJD days for the TAP query."""
    if not time_range:
        return None, None
    try:
        t = Time(time_range, format="isot", scale="utc")
    except ValueError as exc:
        raise APIError(
            "ORB-API-001", "time_range must be ISO-8601 dates", detail={"time_range": time_range}
        ) from exc
    return float(t[0].mjd), float(t[1].mjd)


@router.post("")
async def create_field(body: FieldCreate, svc: Annotated[Services, Depends(services)]) -> dict:
    """Create or attach a field. Idempotent: same params -> same field_id."""
    preset_coords = _preset_params(svc, body.preset)
    preset_ra = preset_dec = None
    preset_field: dict[str, Any] = {}
    radius_deg, release = body.radius_deg, body.release
    if preset_coords is not None:
        preset_ra, preset_dec, pr, pre, preset_field = preset_coords
        if pr is not None:
            radius_deg = pr
        release = pre or release
    # explicit ra/dec win over the preset; a preset alone fills both
    ra = body.ra if body.ra is not None else preset_ra
    dec = body.dec if body.dec is not None else preset_dec
    if ra is None or dec is None:
        raise APIError("ORB-API-001", "ra and dec are required unless a preset is given")
    mjd_start, mjd_end = _mjd_range(body.time_range)
    # The preset's ``field:`` block carries curated detector/MJD filters that
    # are part of the field's identity (they hash into field_id, so the baked
    # demo bundle only attaches when they are applied). It only applies when
    # the caller did not override the preset's coordinates — those filters
    # are meaningless at a different sky position. An explicit time_range
    # wins over the preset's mjd bounds.
    coords_overridden = body.ra is not None or body.dec is not None
    if preset_field and not coords_overridden:
        if body.time_range is None:
            mjd_start = preset_field.get("mjd_start", mjd_start)
            mjd_end = preset_field.get("mjd_end", mjd_end)
        preset_detectors = preset_field.get("detectors")
    else:
        preset_detectors = None
    params = FieldParams(
        ra=ra,
        dec=dec,
        radius_deg=radius_deg,
        release=release,
        preset=preset_coords is not None,
        mjd_start=mjd_start,
        mjd_end=mjd_end,
        detectors=preset_detectors,
        search_pad=body.search_pad,
    )
    field_id = await svc.pipeline.ensure_field(params)
    row = _require_field(await get_field(svc.db, field_id))
    return ok(_field_view(row).model_dump(mode="json"))


@router.get("/{field_id}")
async def get_field_view(field_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    row = _require_field(await get_field(svc.db, field_id))
    frames = await list_frames(svc.db, field_id)
    return ok(_field_view(row, frames_done=len(frames)).model_dump(mode="json"))


async def _field_events(svc: Services, field_id: str, after_id: int):
    """DB replay first (Last-Event-ID), then the live runner stream.

    Event ids are store seqs (EventBus persist hook), so replayed and live
    events share one id space; anything at or below the replayed tail is
    skipped to avoid duplicates. The caller has already verified the field.
    """
    last = after_id
    terminal = False
    if await replay_gap(svc.db, field_id, after_seq=after_id):
        yield {
            "id": after_id,
            "type": "warning",
            "data": {
                "code": "ORB-ST-003",
                "message": "event history was pruned; resync via GET "
                f"/api/v1/fields/{field_id} for current state",
            },
        }
    for ev in await replay_events(svc.db, field_id, after_seq=after_id, limit=10000):
        last = max(last, ev["seq"])
        terminal = ev["type"] in ("done", "error")
        yield {"id": ev["seq"], "type": ev["type"], "data": ev["data"]}
    if terminal:
        return
    async for ev in svc.pipeline.subscribe(field_id, after_id=last):
        if ev["id"] <= last:
            continue
        last = ev["id"]
        data = ev["data"]
        if ev["type"] == "frame" and "frame_ref" in data:
            ref = data["frame_ref"]
            data = {
                **data,
                "png_url": f"/api/v1/fields/{field_id}/frames/{ref}.png",
                "meta_url": f"/api/v1/fields/{field_id}/frames/{ref}/meta",
            }
        yield {"id": ev["id"], "type": ev["type"], "data": data}
        if ev["type"] in ("done", "error"):
            return


@router.get("/{field_id}/events")
async def field_events(
    request: Request, field_id: str, svc: Annotated[Services, Depends(services)]
):
    # check before opening the stream: errors inside SSE can't change status
    _require_field(await get_field(svc.db, field_id))
    return sse_response(_field_events(svc, field_id, last_event_id(request.headers)))


@router.post("/{field_id}/cancel")
async def cancel_field(field_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    row = _require_field(await get_field(svc.db, field_id))
    if row["status"] in ("completed", "failed", "cancelled"):
        raise APIError("ORB-API-003", f"field already {row['status']}")
    await svc.pipeline.cancel(field_id)
    row = _require_field(await get_field(svc.db, field_id))
    return ok(_field_view(row).model_dump(mode="json"))


# -- frames -----------------------------------------------------------------


async def _require_frame(svc: Services, field_id: str, frame_ref: str) -> dict:
    _require_field(await get_field(svc.db, field_id))
    row = await get_frame(svc.db, field_id, frame_ref)
    if row is None:
        raise APIError("ORB-API-002", "frame not found")
    return row


@router.get("/{field_id}/frames")
async def list_field_frames(field_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    _require_field(await get_field(svc.db, field_id))
    frames = await list_frames(svc.db, field_id)
    views = [
        FrameMetaView(
            frame_ref=f["frame_ref"],
            field_id=field_id,
            wavelength_um=f.get("wavelength_um"),
            mjd=f.get("mjd"),
            detector=f.get("detector"),
            release=(f.get("meta") or {}).get("release"),
            has_png=f.get("png_blob") is not None or f.get("array_blob") is not None,
            meta=f.get("meta"),
        ).model_dump(mode="json")
        for f in frames
    ]
    return ok({"frames": views})


@router.get("/{field_id}/frames/{frame_ref}/meta")
async def frame_meta(
    field_id: str, frame_ref: str, svc: Annotated[Services, Depends(services)]
) -> dict:
    row = await _require_frame(svc, field_id, frame_ref)
    return ok(
        FrameMetaView(
            frame_ref=row["frame_ref"],
            field_id=field_id,
            wavelength_um=row.get("wavelength_um"),
            mjd=row.get("mjd"),
            detector=row.get("detector"),
            meta=row.get("meta"),
        ).model_dump(mode="json")
    )


def _render_frame_png(row: dict) -> bytes:
    """Render a stored frame's array_blob to PNG on demand.

    Used when ``png_blob`` was never stored (offline demo bundle): applies
    the same display transform the pipeline used — subtract the recorded
    ``background_level_mjy_sr``, stretch with the persisted params (or a
    deterministic fit for legacy rows), alpha 0 where masked.
    """
    blob_raw = row.get("array_blob")
    if blob_raw is None:
        raise APIError("ORB-API-002", "frame image not available yet")
    try:
        decoded = decode_frame_array(bytes(blob_raw))
    except Exception as exc:
        raise APIError("ORB-API-002", "frame array is corrupt or unreadable") from exc
    image = np.asarray(decoded.data, dtype=np.float64)
    if decoded.mask is not None:
        mask = np.asarray(decoded.mask, dtype=bool)
        if mask.shape == image.shape:
            image = np.where(mask, image, np.nan)
    meta = row.get("meta") or {}
    bg = meta.get("background_level_mjy_sr")
    if bg is not None:
        image = image - float(bg)
    params = StretchParams.from_dict(meta.get("stretch")) or fit_stretch(image)
    stretched = apply_stretch(image, params)
    valid = np.isfinite(image)
    gray = np.zeros(image.shape, np.uint8)
    gray[valid] = (np.clip(stretched[valid], 0.0, 1.0) * 255).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([gray, alpha]), mode="LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/{field_id}/frames/{frame_ref}.png")
async def frame_png(
    field_id: str, frame_ref: str, svc: Annotated[Services, Depends(services)]
) -> Response:
    row = await _require_frame(svc, field_id, frame_ref)
    headers = {"Cache-Control": "public, max-age=3600"}
    if row.get("png_blob"):
        return Response(content=bytes(row["png_blob"]), media_type="image/png", headers=headers)
    return Response(content=_render_frame_png(row), media_type="image/png", headers=headers)


@router.get("/{field_id}/frames/{frame_ref}/array")
async def frame_array(
    field_id: str, frame_ref: str, svc: Annotated[Services, Depends(services)]
) -> Response:
    """Raw aligned float32 array as ``.npy`` (NaN where masked, §8.4)."""
    row = await _require_frame(svc, field_id, frame_ref)
    blob_raw = row.get("array_blob")
    if blob_raw is None:
        raise APIError("ORB-API-002", "frame array not available yet")
    try:
        blob = decode_frame_array(bytes(blob_raw))
    except Exception as exc:
        raise APIError("ORB-API-002", "frame array is corrupt or unreadable") from exc
    buf = io.BytesIO()
    np.save(buf, blob.data, allow_pickle=False)
    return Response(
        content=buf.getvalue(),
        media_type="application/x-npy",
        headers={"Content-Disposition": f'inline; filename="{frame_ref}.npy"'},
    )


@router.get("/{field_id}/frames/{frame_ref}/flags.png")
async def frame_flags(
    field_id: str, frame_ref: str, svc: Annotated[Services, Depends(services)]
) -> Response:
    # Flags are stored inside array_blob but no visualisation is rendered yet.
    await _require_frame(svc, field_id, frame_ref)
    raise APIError("ORB-API-002", "flags visualisation is not available")


# -- spectrum, diff, group images --------------------------------------------


@router.get("/{field_id}/spectrum")
async def field_spectrum(
    field_id: str,
    svc: Annotated[Services, Depends(services)],
    ra: float | None = None,
    dec: float | None = None,
) -> dict:
    """Quick-look spectrum at a position: per-wavelength aperture photometry.

    Reuses the lightcurve measurement path and bins flux by ``wavelength_um``
    — an estimate with supporting data (§8.5), ``insufficient_data`` when too
    few bands carry flux. Not a calibrated spectrum (``method.calibrated``).
    """
    row = _require_field(await get_field(svc.db, field_id))
    spec = row.get("grid")
    wcs = wcs_from_grid_spec(spec)
    if wcs is None or not spec or ra is None or dec is None:
        view = SpectrumView(
            status="insufficient_data",
            reason="ra and dec are required and the field grid must be available",
        )
        return ok(view.model_dump(mode="json"))
    shape = tuple(int(v) for v in spec["shape"])
    try:
        x, y = wcs.all_world2pix(ra, dec, 0)
    except Exception:
        view = SpectrumView(status="insufficient_data", reason="position cannot be projected")
        return ok(view.model_dump(mode="json"))
    if not (0.0 <= float(x) < shape[1] and 0.0 <= float(y) < shape[0]):
        view = SpectrumView(status="insufficient_data", reason="position is outside the field grid")
        return ok(view.model_dump(mode="json"))
    frames = await list_frames(svc.db, field_id)
    sr_per_pixel = await asyncio.to_thread(pixel_solid_angle_sr, wcs, shape)
    items = [{"frame": f, "x": float(x), "y": float(y)} for f in frames]
    points, _skipped = await asyncio.to_thread(measure_points, items, wcs, sr_per_pixel)
    status, reason, bins = spectrum_bins(points)
    method = method_dict("fixed")
    method["kind"] = "spectrum"
    mjds = [f.get("mjd") for f in frames if f.get("mjd") is not None]
    span = f"frames span {max(mjds) - min(mjds):.1f} days; " if len(mjds) > 1 else ""
    method["note"] = (
        span + "quick-look SED: median aperture flux per wavelength band; "
        "not a calibrated spectrum; masked bands report null flux + masked_bits"
    )
    view = SpectrumView(status=status, reason=reason, points=bins, method=method)
    return ok(view.model_dump(mode="json"))


@router.get("/{field_id}/lightcurve")
async def field_lightcurve(
    field_id: str,
    svc: Annotated[Services, Depends(services)],
    ra: float,
    dec: float,
) -> dict:
    """Per-frame centroid + aperture photometry at a fixed sky position.

    The quick-look view behind "brightness/position vs time" for named
    targets and clicked sky positions. All frames share the common grid,
    so ``ra``/``dec`` maps to one pixel position measured on every frame.
    """
    row = _require_field(await get_field(svc.db, field_id))
    spec = row.get("grid")
    wcs = wcs_from_grid_spec(spec)
    if wcs is None or not spec:
        raise APIError("ORB-API-002", "field grid is not available yet")
    shape = tuple(int(v) for v in spec["shape"])
    try:
        x, y = wcs.all_world2pix(ra, dec, 0)
    except Exception as exc:
        raise APIError("ORB-API-001", "position cannot be projected onto the field grid") from exc
    if not (0.0 <= float(x) < shape[1] and 0.0 <= float(y) < shape[0]):
        raise APIError(
            "ORB-API-001",
            "position is outside the field grid",
            detail={"ra": ra, "dec": dec},
        )
    frames = await list_frames(svc.db, field_id)
    sr_per_pixel = await asyncio.to_thread(pixel_solid_angle_sr, wcs, shape)
    items = [{"frame": f, "x": float(x), "y": float(y)} for f in frames]
    points, skipped = await asyncio.to_thread(measure_points, items, wcs, sr_per_pixel)
    view = LightCurveView(
        kind="fixed",
        reference={"ra": ra, "dec": dec},
        method=method_dict("fixed"),
        points=points,
    )
    return ok(
        view.model_dump(mode="json"),
        meta={"frames_measured": len(points), "frames_skipped": skipped},
    )


@router.get("/{field_id}/diff.png")
async def field_diff_png(field_id: str, svc: Annotated[Services, Depends(services)]) -> Response:
    """Difference image; served from ``derived`` when a research job has
    produced one. ``derived`` is keyed by (field_id, kind) only, so v1
    serves the single stored diff — group-pair diffs need a schema change."""
    _require_field(await get_field(svc.db, field_id))
    blob = await get_derived(svc.db, field_id, "diff")
    if blob is None:
        raise APIError("ORB-API-002", "no difference image for this field")
    return Response(content=bytes(blob), media_type="image/png")


# -- groups -----------------------------------------------------------------


@router.get("/{field_id}/groups")
async def list_field_groups(field_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    _require_field(await get_field(svc.db, field_id))
    groups = await list_groups(svc.db, field_id)
    return ok(
        {
            "groups": [
                GroupView(
                    group_id=g["group_id"],
                    rule=g.get("rule"),
                    frame_refs=g.get("frame_refs") or [],
                    meta=g.get("meta"),
                ).model_dump(mode="json")
                for g in groups
            ]
        }
    )


@router.get("/{field_id}/groups/{group_id}.png")
async def group_png(
    field_id: str, group_id: str, svc: Annotated[Services, Depends(services)]
) -> Response:
    _require_field(await get_field(svc.db, field_id))
    for g in await list_groups(svc.db, field_id):
        if g["group_id"] == group_id and g.get("png_blob"):
            return Response(content=bytes(g["png_blob"]), media_type="image/png")
    raise APIError("ORB-API-002", "group image not available")


__all__ = ["router"]
