"""Candidate endpoints: list, detail, stack PNG, frame observations.

Purpose: Read-only HTTP surface for unconfirmed candidates (SDD §8.2b,
§8.5). ``status`` is always ``unconfirmed``; ``model_score`` is always the
``{value, model_version, kind}`` object or null.
Inputs: ``/api/v1/candidates`` and ``/fields/{id}/candidates`` requests.
Outputs: ``{data, meta}`` envelopes, PNG binary.
Called by: api.router.
"""

from __future__ import annotations

import asyncio
import io
import math
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import APIRouter, Depends, Response
from PIL import Image

from orbitalis.api.deps import Services, services
from orbitalis.api.fields import _require_field
from orbitalis.api.photometry import (
    measure_points,
    method_dict,
    pixel_solid_angle_sr,
    spectrum_bins,
    wcs_from_grid_spec,
)
from orbitalis.api.schemas import (
    CandidateDetail,
    CandidateSummary,
    LightCurveView,
    ModelScoreView,
    PositionView,
    RankValueView,
    SpectrumView,
    ok,
)
from orbitalis.errors import APIError
from orbitalis.store import (
    decode_frame_array,
    get_candidate,
    get_field,
    get_frame,
    get_job,
    list_candidates,
    list_jobs,
)
from orbitalis_core.stretch import (
    StretchParams,
    apply_stretch,
    cutout_array,
    fit_stretch,
)

router = APIRouter(tags=["candidates"])
fields_router = APIRouter(prefix="/fields", tags=["candidates"])

SortKey = Literal["model_score", "stack_snr"]

# Display-only cutout half-size in pixels (SDD §8.2b renders on demand).
_CUTOUT_HALF_PX = 7


def _model_score(row: dict[str, Any]) -> ModelScoreView | None:
    if row.get("model_score") is None:
        return None
    return ModelScoreView(
        value=float(row["model_score"]),
        model_version=row.get("model_version"),
    )


def _rank_value(row: dict[str, Any]) -> RankValueView | None:
    """U-02: one ordering number — model_score when ranked, else stack_snr."""
    ms = _model_score(row)
    if ms is not None:
        return RankValueView(value=ms.value, basis="model_score")
    if row.get("stack_snr") is not None:
        return RankValueView(value=float(row["stack_snr"]), basis="stack_snr")
    return None


def _job_ordinals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """U-12: stable 1-based ordinal per job (stack_snr order, id tiebreak).

    Independent of the requested list ``sort`` so ``#042``-style display ids
    do not renumber when the user changes ordering.
    """
    by_job: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_job.setdefault(str(r.get("job_id") or ""), []).append(r)
    out: dict[str, int] = {}
    for rs in by_job.values():
        rs.sort(
            key=lambda r: (
                -(float(r["stack_snr"]) if r.get("stack_snr") is not None else -1e30),
                str(r["candidate_id"]),
            )
        )
        for i, r in enumerate(rs, 1):
            out[str(r["candidate_id"])] = i
    return out


def _summary(
    row: dict[str, Any], *, ordinal: int | None = None, background_variant: str | None = None
) -> CandidateSummary:
    return CandidateSummary(
        candidate_id=row["candidate_id"],
        position=PositionView(ra=float(row["ra"]), dec=float(row["dec"])),
        motion=row.get("motion"),
        stack_snr=row.get("stack_snr"),
        model_score=_model_score(row),
        rank_value=_rank_value(row),
        ordinal=ordinal,
        background_variant=background_variant,
        predicted_category=row.get("predicted_category"),
        static_source=(
            bool(row["static_source"]) if row.get("static_source") is not None else None
        ),
        created_at=row.get("created_at"),
    )


def _grid_wcs(spec: dict[str, Any] | None):
    """Rebuild the common-grid WCS from the field's stored grid spec.

    Same axis-aligned TAN convention as ``pipeline.runner._target_grid`` /
    ``research.search._wcs_from_grid``: no SIP or rotation terms (the grid
    is defined without them).
    """
    if not spec:
        return None
    from astropy.wcs import WCS

    scale_deg = float(spec["pixel_scale_arcsec"]) / 3600.0
    h, w = (int(v) for v in spec["shape"])
    ww = WCS(naxis=2)
    ww.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    ww.wcs.crpix = [w / 2 + 0.5, h / 2 + 0.5]
    ww.wcs.crval = [float(spec["ra"]), float(spec["dec"])]
    ww.wcs.cdelt = [-scale_deg, scale_deg]
    return ww


def _detail(
    row: dict[str, Any],
    grid_spec: dict[str, Any] | None = None,
    *,
    ordinal: int | None = None,
    background_variant: str | None = None,
) -> CandidateDetail:
    base = _summary(row, ordinal=ordinal, background_variant=background_variant)
    frames = [dict(f) for f in (row.get("frames") or [])]
    # U-04: sky positions per frame on the common grid + reference epoch.
    wcs = _grid_wcs(grid_spec)
    mjds = [float(f["mjd"]) for f in frames if f.get("mjd") is not None]
    # X-13: prefer the search's stored reference epoch; the min-frame-MJD
    # fallback only covers rows written before migration 0005.
    reference_mjd = (
        row.get("reference_mjd")
        if row.get("reference_mjd") is not None
        else (min(mjds) if mjds else None)
    )
    if wcs is not None:
        for f in frames:
            # Stored search-time sky positions win; compute only when absent.
            if f.get("predicted_ra") is not None and f.get("predicted_dec") is not None:
                continue
            x, y = f.get("predicted_x"), f.get("predicted_y")
            if x is None or y is None:
                continue
            try:
                ra, dec = wcs.all_pix2world(float(x), float(y), 0)
            except Exception:
                continue
            if np.isfinite(ra) and np.isfinite(dec):
                f["predicted_ra"] = float(ra)
                f["predicted_dec"] = float(dec)
    return CandidateDetail(
        **base.model_dump(),
        job_id=row.get("job_id"),
        field_id=row["field_id"],
        reference_mjd=reference_mjd,
        frames=frames,
    )


def _sort_key(row: dict[str, Any], sort: SortKey):
    if sort == "model_score" and row.get("model_score") is not None:
        return (0, -float(row["model_score"]))
    snr = row.get("stack_snr")
    return (1 if sort == "model_score" else 0, -(snr if snr is not None else -1e30))


@fields_router.get("/{field_id}/candidates")
async def list_field_candidates(
    field_id: str,
    svc: Annotated[Services, Depends(services)],
    job_id: str | None = None,
    sort: SortKey | None = None,
) -> dict:
    _require_field(await get_field(svc.db, field_id))
    rows = await list_candidates(svc.db, field_id)
    if job_id is not None:
        rows = [r for r in rows if r.get("job_id") == job_id]
    # SDD §8.2b: default is model_score when the field is ranked, else
    # stack_snr. sort=None means "apply the default"; an explicit value wins.
    if sort is None:
        sort = "model_score" if any(r.get("model_score") is not None for r in rows) else "stack_snr"
    rows.sort(key=lambda r: _sort_key(r, sort))
    ordinals = _job_ordinals(rows)
    # D-24 caveat travels with stack_snr: variant + calibrated flag.
    job_rows = await list_jobs(svc.db, field_id)
    bgw = {j["job_id"]: (j.get("result") or {}).get("background_variant") for j in job_rows}
    summaries = [
        _summary(
            r,
            ordinal=ordinals.get(str(r["candidate_id"])),
            background_variant=bgw.get(str(r.get("job_id") or "")),
        ).model_dump(mode="json")
        for r in rows
    ]
    return ok({"candidates": summaries}, meta={"sorted_by": sort})


async def _require_candidate(svc: Services, candidate_id: str) -> dict[str, Any]:
    row = await get_candidate(svc.db, candidate_id)
    if row is None:
        raise APIError("ORB-API-002", "candidate not found")
    return row


@router.get("/candidates/{candidate_id}")
async def get_candidate_detail(
    candidate_id: str, svc: Annotated[Services, Depends(services)]
) -> dict:
    row = await _require_candidate(svc, candidate_id)
    field = await get_field(svc.db, row["field_id"])
    siblings = [
        r
        for r in await list_candidates(svc.db, row["field_id"])
        if str(r.get("job_id") or "") == str(row.get("job_id") or "")
    ]
    ordinal = _job_ordinals(siblings).get(str(row["candidate_id"]))
    bgw = None
    if row.get("job_id"):
        job = await get_job(svc.db, str(row["job_id"]))
        bgw = ((job or {}).get("result") or {}).get("background_variant")
    return ok(
        _detail(
            row,
            (field or {}).get("grid"),
            ordinal=ordinal,
            background_variant=bgw,
        ).model_dump(mode="json")
    )


@router.get("/candidates/{candidate_id}/stack.png")
async def candidate_stack_png(
    candidate_id: str, svc: Annotated[Services, Depends(services)]
) -> Response:
    row = await _require_candidate(svc, candidate_id)
    if not row.get("stack_png_blob"):
        raise APIError("ORB-API-002", "stack image not available")
    return Response(content=bytes(row["stack_png_blob"]), media_type="image/png")


@router.get("/candidates/{candidate_id}/frames")
async def candidate_frames(candidate_id: str, svc: Annotated[Services, Depends(services)]) -> dict:
    """Observations used by the search, with predicted positions (§8.2b)."""
    row = await _require_candidate(svc, candidate_id)
    return ok({"frames": row.get("frames") or []})


@router.get("/candidates/{candidate_id}/lightcurve")
async def candidate_lightcurve(
    candidate_id: str, svc: Annotated[Services, Depends(services)]
) -> dict:
    """Per-frame centroid + aperture photometry along the candidate's track.

    Uses each stored frame entry's search-time ``predicted_x``/``predicted_y``
    as the prior and re-measures the centroid on the stored array — the
    observed positions are quick-look measurements, still ``unconfirmed``.
    """
    row = await _require_candidate(svc, candidate_id)
    field = await get_field(svc.db, row["field_id"])
    spec = (field or {}).get("grid")
    wcs = wcs_from_grid_spec(spec)
    if wcs is None or not spec:
        raise APIError("ORB-API-002", "field grid is not available yet")
    shape = tuple(int(v) for v in spec["shape"])
    items: list[dict[str, Any]] = []
    for entry in row.get("frames") or []:
        frame_ref = entry.get("frame_ref")
        px, py = entry.get("predicted_x"), entry.get("predicted_y")
        if not frame_ref or px is None or py is None:
            continue
        frame = await get_frame(svc.db, row["field_id"], frame_ref)
        if frame is None:
            continue
        items.append(
            {
                "frame": frame,
                "x": float(px),
                "y": float(py),
                "predicted_x": float(px),
                "predicted_y": float(py),
                "mjd": entry.get("mjd"),
            }
        )
    sr_per_pixel = await asyncio.to_thread(pixel_solid_angle_sr, wcs, shape)
    points, skipped = await asyncio.to_thread(measure_points, items, wcs, sr_per_pixel)
    view = LightCurveView(
        kind="candidate",
        reference={
            "candidate_id": row["candidate_id"],
            "position": {"ra": float(row["ra"]), "dec": float(row["dec"])},
            "reference_mjd": row.get("reference_mjd"),
            "motion": row.get("motion"),
        },
        method=method_dict("candidate"),
        points=points,
    )
    return ok(
        view.model_dump(mode="json"),
        meta={"frames_measured": len(points), "frames_skipped": skipped},
    )


@router.get("/candidates/{candidate_id}/spectrum")
async def candidate_spectrum(
    candidate_id: str, svc: Annotated[Services, Depends(services)]
) -> dict:
    """Quick-look spectrum along the candidate's track: per-wavelength bins of
    the same aperture photometry used by the candidate lightcurve."""
    row = await _require_candidate(svc, candidate_id)
    field = await get_field(svc.db, row["field_id"])
    spec = (field or {}).get("grid")
    wcs = wcs_from_grid_spec(spec)
    if wcs is None or not spec:
        view = SpectrumView(status="insufficient_data", reason="field grid is not available yet")
        return ok(view.model_dump(mode="json"))
    shape = tuple(int(v) for v in spec["shape"])
    items: list[dict[str, Any]] = []
    for entry in row.get("frames") or []:
        frame_ref = entry.get("frame_ref")
        px, py = entry.get("predicted_x"), entry.get("predicted_y")
        if not frame_ref or px is None or py is None:
            continue
        frame = await get_frame(svc.db, row["field_id"], frame_ref)
        if frame is None:
            continue
        items.append({"frame": frame, "x": float(px), "y": float(py)})
    sr_per_pixel = await asyncio.to_thread(pixel_solid_angle_sr, wcs, shape)
    points, _skipped = await asyncio.to_thread(measure_points, items, wcs, sr_per_pixel)
    status, reason, bins = spectrum_bins(points)
    method = method_dict("candidate")
    method["kind"] = "spectrum"
    method["note"] = (
        "quick-look SED along the candidate track: median aperture flux per "
        "wavelength band; not a calibrated spectrum; candidate is unconfirmed; "
        "masked bands report null flux + masked_bits"
    )
    view = SpectrumView(status=status, reason=reason, points=bins, method=method)
    return ok(view.model_dump(mode="json"))


def _stretch_params(meta: dict[str, Any], image: np.ndarray) -> StretchParams:
    """Stretch for on-demand renders: the params persisted with the frame's
    png_blob when present, else a deterministic fit on the full frame."""
    params = StretchParams.from_dict((meta or {}).get("stretch"))
    if params is not None:
        return params
    return fit_stretch(image)


def _cutout_png(image: np.ndarray, cx: float, cy: float, params: StretchParams) -> bytes:
    """Render a (2*half+1)^2 px cutout centred on (cx, cy) as an LA PNG.

    Pixels outside the frame are NaN (alpha 0), matching the masked-pixel
    convention. ``cx``/``cy`` are 0-based array (x, y) coordinates.
    """
    out = cutout_array(image, cx, cy, _CUTOUT_HALF_PX)
    stretched = apply_stretch(out, params)
    valid = np.isfinite(out)
    gray = np.zeros(out.shape, np.uint8)
    gray[valid] = (np.clip(stretched[valid], 0.0, 1.0) * 255).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([gray, alpha]), mode="LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/candidates/{candidate_id}/frames/{frame_ref}.png")
async def candidate_frame_png(
    candidate_id: str, frame_ref: str, svc: Annotated[Services, Depends(services)]
) -> Response:
    """Original frame cutout at the candidate's predicted position (§8.2b)."""
    row = await _require_candidate(svc, candidate_id)
    entry = next(
        (f for f in (row.get("frames") or []) if f.get("frame_ref") == frame_ref),
        None,
    )
    if entry is None:
        raise APIError("ORB-API-002", "frame is not part of this candidate's search")
    px, py = entry.get("predicted_x"), entry.get("predicted_y")
    if px is None or py is None:
        raise APIError("ORB-API-002", "predicted position is not available for this frame")
    frame = await get_frame(svc.db, row["field_id"], frame_ref)
    if frame is None or not frame.get("array_blob"):
        raise APIError("ORB-API-002", "frame array not available")
    try:
        decoded = decode_frame_array(bytes(frame["array_blob"]))
    except Exception as exc:
        raise APIError("ORB-API-002", "frame array is corrupt or unreadable") from exc
    image = np.asarray(decoded.data, dtype=np.float64)
    # Masked pixels render transparent even if the stored array kept a value.
    if decoded.mask is not None:
        mask = np.asarray(decoded.mask, dtype=bool)
        if mask.shape == image.shape:
            image = np.where(mask, image, np.nan)
    # D-26/CORE.06a: frames baked after the background-subtracted stretch
    # carry ``background_level_mjy_sr``; subtract it so the cutout matches
    # the frame PNG (which is rendered on residuals). Frames without the
    # key keep their raw-data stretch untouched.
    bg = (frame.get("meta") or {}).get("background_level_mjy_sr")
    if bg is not None:
        image = image - float(bg)
    params = _stretch_params(frame.get("meta") or {}, image)
    # A fully off-frame position is an honest 404, not a transparent PNG.
    h, w = image.shape
    half = _CUTOUT_HALF_PX
    x0, y0 = int(math.floor(float(px) + 0.5)), int(math.floor(float(py) + 0.5))
    if x0 + half < 0 or y0 + half < 0 or x0 - half >= w or y0 - half >= h:
        raise APIError("ORB-API-002", "predicted position is outside the stored frame")
    png = await asyncio.to_thread(_cutout_png, image, float(px), float(py), params)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


__all__ = ["fields_router", "router"]
