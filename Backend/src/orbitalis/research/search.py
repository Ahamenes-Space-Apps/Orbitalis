"""moving_object_search research handler.

Purpose: Run shift-and-stack search on stored field frames and persist candidates.
Inputs: JobContext with params {field_id, frame_refs?, motion_vectors?|motion grid,
        min_coverage_fraction?, detection_threshold_sigma?/detection_threshold?}.
        Motion inputs use sky-plane arcsec/day only (mu_east/mu_north or rate/PA);
        pixel/day belongs exclusively to the internal core.
Outputs: dict with counts, work estimate, and candidate ids.
Called by: jobs runner via HandlerRegistry (registered in main.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io.fits import Header
from astropy.wcs import WCS
from PIL import Image
from scipy.ndimage import median_filter

from orbitalis.errors import APIError
from orbitalis.jobs import JobContext
from orbitalis.research.policy import enforce_combinable, load_stack_policy
from orbitalis.settings import Settings
from orbitalis.store import decode_frame_array, get_field, get_job, list_frames, put_candidate
from orbitalis_core.errors import InvalidInputError
from orbitalis_core.shiftstack import Frame, MotionVector, search, shift_and_combine
from orbitalis_core.shiftstack.motion import (
    generate_sky_motion_grid,
    pixel_motion_to_sky,
    sky_motion_grid_to_motion_vectors,
    sky_motion_to_pixel,
)
from orbitalis_core.stretch import StretchParams, apply_stretch, cutout_array, fit_stretch

# Fallback when runtime settings are unavailable. Settings.search_max_work is the
# authoritative configured default (ORB_SEARCH_MAX_WORK, Appendix A).
_DEFAULT_SEARCH_MAX_WORK = float(Settings().search_max_work)
_GRID_PIXEL_SCALE_ARCSEC = 6.15

# PROVISIONAL(G1): extraction/constants pending ML/team-lead approval (D-24/D-17/RS.03).
EXTRACTION_DEFAULT_SIGMA_THRESHOLD = 5.0  # TODO(ML) PROVISIONAL(G1): sigma units
# Cap on persisted candidates per search: the max-over-trials score map can
# return dozens of unflagged noise/leak candidates on a 48x48 field (RS.09),
# which would flood the candidate UI. Keep the best by stack_snr; the true
# extracted count is still reported via candidates_found.
EXTRACTION_MAX_PERSISTED_CANDIDATES = 25  # PROVISIONAL(G1): TODO(ML) tune with VAL
EXTRACTION_NMS_RADIUS_PX = 3  # TODO(ML) PROVISIONAL(G1): spatial separation/NMS radius
EXTRACTION_EDGE_MARGIN_PX = 1  # TODO(ML) PROVISIONAL(G1): exclude border peaks
HIGHPASS_MEDIAN_SIZE_PX = 15  # TODO(ML) PROVISIONAL(G1): D-24 variant C
BACKGROUND_ZODI_MEDIAN_SIZE_PX = 31  # TODO(ML) PROVISIONAL(G1): variant B ZODI stand-in
BACKGROUND_VARIANTS = ("a", "b", "c", "d", "e", "f")  # D-24 comparison set
# Quantile of the per-pixel temporal coadd subtracted by difference imaging
# (variants E/F). PROVISIONAL(G1) — TODO(ML) recalibrate with VAL.02/D-24.
#
# D-24 finding (tutorial field, 2026-11): at the ~5 independent epochs a
# single-field stack actually has, q25 samples the sorted stack at index ~1,
# which is inside a mover that repeats across near-simultaneous exposures
# (the "ghost trail"). That self-subtracts ~60% of the mover peak and inflates
# the score-map MAD, so more frames made the field *less* sensitive.
# Measured on 20 injected truths (seed 20261115, SNR 5-40, speed 1-8 "/d,
# match 1.5 px): q25 -> recall 0.28 / 16 spurious, q10 -> 0.68 / 31,
# q5 -> 0.74 / 33 (point PSF). q5 needs only two mover-free epochs per pixel
# to land on background, which is the honest floor for a low-epoch stack.
DIFFERENCE_QUANTILE = 5.0
DIFFERENCE_MIN_VALID_EPOCHS = 3  # below this the residual is meaningless
STATIC_SOURCE_INDIVIDUAL_SIGMA = 5.0  # TODO(ML) PROVISIONAL(G1): deterministic heuristic
STATIC_SOURCE_MIN_FRAME_FRACTION = 0.5  # TODO(ML) PROVISIONAL(G1): heuristic
STATIC_MASK_MEDIAN_SIGMA = 8.0  # TODO(ML) PROVISIONAL(G1): variant D coadd threshold
STATIC_MASK_DILATION_PX = 2  # TODO(ML) PROVISIONAL(G1): cover PSF extent
STATIC_MASK_MIN_FRAMES = 5  # PROVISIONAL(G1): median coadd needs >=5 epochs to be robust
STATIC_SOURCE_MAX_DISPLACEMENT_PX = 1.0  # TODO(ML) PROVISIONAL(G1): ~0.4 FWHM
_VARIANT_LABELS = {
    "a": "a_none",
    "b": "b_zodi_subtract",
    "c": "c_highpass_median_15px",
    "d": "d_highpass_median_15px_static_mask",
    "e": f"e_difference_imaging_q{DIFFERENCE_QUANTILE:g}_coadd",
    "f": f"f_difference_imaging_q{DIFFERENCE_QUANTILE:g}_coadd_highpass_15px",
}
ZERO_MOTION_EPS = 1e-12

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecodedFrame:
    """Decoded stored frame inputs/metadata used by the handler."""

    frame_ref: str
    image: np.ndarray
    variance: np.ndarray
    mask: np.ndarray
    flags: np.ndarray | None
    mjd: float
    detector: Any
    wavelength_um: Any
    meta: dict[str, Any]


def _resolve_field_wcs(
    field: dict[str, Any],
    rows: list[dict[str, Any]],
    frame_shape: tuple[int, ...],
) -> WCS:
    """Resolve the field's common-grid WCS and check frame/grid shape match.

    Shared by ``moving_object_search`` and the VAL.04 validation handler.
    """
    grid_spec = field.get("grid")
    if not grid_spec:
        raise APIError("ORB-RS-001", "field grid/WCS is not available for candidate positions")
    spec_wcs, grid_shape = _wcs_from_grid(grid_spec)
    field_wcs = _wcs_from_stored(rows)
    if field_wcs is None:
        # Synthetic/legacy fields without a stored grid header: rebuild the
        # axis-aligned TAN approximation. Real SPHEREx fields carry SIP and
        # rotation, so positions are approximate until grid_wcs is present.
        field_wcs = spec_wcs
    if tuple(frame_shape) != tuple(grid_shape):
        raise APIError(
            "ORB-RS-001",
            "stored frame dimensions do not match field grid dimensions",
            detail={"frame_shape": list(frame_shape), "grid_shape": list(grid_shape)},
        )
    return field_wcs


def _grid_shape(spec: dict[str, Any]) -> tuple[int, int]:
    """Return (height, width) from stored grid metadata.

    Stored synthetic/common grids use [n, n]. Accept [height, width] for
    rectangular metadata when provided, but do not assume square if two values
    are present.
    """
    shape = spec.get("shape")
    if not isinstance(shape, (list, tuple)) or len(shape) < 2:
        raise APIError("ORB-RS-001", "field grid shape metadata is missing or invalid")
    try:
        h = int(shape[0])
        w = int(shape[1])
    except Exception as exc:
        raise APIError("ORB-RS-001", "field grid shape metadata is missing or invalid") from exc
    if h <= 0 or w <= 0:
        raise APIError("ORB-RS-001", "field grid shape metadata is missing or invalid")
    return h, w


def _wcs_from_stored(rows: list[dict[str, Any]]) -> WCS | None:
    """Rebuild the common-grid WCS from a stored frame's ``meta.grid_wcs``.

    CORE.04a persists the target grid header (D-29); it carries the real
    CD/PC/SIP terms, unlike the axis-aligned TAN approximation in
    ``_wcs_from_grid``. Returns None when no usable header is stored.
    """
    for row in rows:
        meta = row.get("meta") or {}
        header_str = meta.get("grid_wcs")
        if not header_str:
            continue
        try:
            w = WCS(Header.fromstring(str(header_str)))
        except (ValueError, TypeError):
            continue
        try:
            ctype = [str(c) for c in np.atleast_1d(w.wcs.ctype)]
        except (AttributeError, ValueError):
            continue
        if len(ctype) >= 2 and ctype[0].startswith("RA") and ctype[1].startswith("DEC"):
            return w
    return None


def _wcs_from_grid(spec: dict[str, Any]) -> tuple[WCS, tuple[int, int]]:
    scale_deg = float(spec.get("pixel_scale_arcsec", _GRID_PIXEL_SCALE_ARCSEC)) / 3600.0
    h, w = _grid_shape(spec)
    # Reconstruct a minimal axis-aligned TAN WCS from common-grid metadata.
    # This is sufficient for G1 synthetic fixtures that store ra/dec/scale/shape
    # only; it does not represent real-field SIP/distortion or rotation terms.
    # Follow-up (RS.02/KAN-51): test pixel-to-arcsec conversion against the real
    # cutout fixture with SIP distortion and rotation once available.
    ww = WCS(naxis=2)
    ww.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    ww.wcs.crpix = [w / 2 + 0.5, h / 2 + 0.5]
    ww.wcs.crval = [float(spec["ra"]), float(spec["dec"])]
    ww.wcs.cdelt = [-scale_deg, scale_deg]
    return ww, (h, w)


async def _get_job_field_id(ctx: JobContext) -> str | None:
    row = await get_job(ctx.db, ctx.job_id)
    return row.get("field_id") if row else None


def _pixel_per_day_to_mu_east_north(
    dx: float, dy: float, wcs: WCS, x: float, y: float
) -> tuple[float, float]:
    """Convert pixel/day motion to (mu_e, mu_n) arcsec/day (D-17).

    Delegates to ``orbitalis_core.shiftstack.motion.pixel_motion_to_sky`` —
    the single conversion implementation (RS.02). Kept as a wrapper so error
    semantics stay ORB-RS-001 at the job boundary.
    """
    try:
        return pixel_motion_to_sky(float(dx), float(dy), wcs, (float(x), float(y)))
    except InvalidInputError as exc:
        raise APIError("ORB-RS-001", f"motion conversion failed: {exc}") from exc


def _mu_east_north_to_pixel_per_day(
    mu_east: float, mu_north: float, wcs: WCS, x: float, y: float
) -> tuple[float, float]:
    """Convert (mu_e, mu_n) arcsec/day to pixel/day (D-17).

    Delegates to ``orbitalis_core.shiftstack.motion.sky_motion_to_pixel`` —
    the single conversion implementation (RS.02), including RA-wrap handling
    and the local-Jacobian reference pixel.
    """
    try:
        return sky_motion_to_pixel(float(mu_east), float(mu_north), wcs, (float(x), float(y)))
    except InvalidInputError as exc:
        raise APIError("ORB-RS-001", f"motion conversion failed: {exc}") from exc


def _default_motion_vectors(wcs: WCS, cx: float, cy: float, policy: dict) -> list[MotionVector]:
    """D-17 default grid: ``generate_sky_motion_grid`` out to the policy radius.

    ``max_time_span_days`` comes from ``stack_policy.yaml`` (D-14, PROVISIONAL).
    """
    try:
        span = float(policy.get("max_time_span_days"))
        grid = generate_sky_motion_grid(span)
        return sky_motion_grid_to_motion_vectors(grid, wcs, (cx, cy))
    except (InvalidInputError, ValueError, TypeError) as exc:
        raise APIError("ORB-RS-001", f"default motion grid failed: {exc}") from exc


def _coerce_finite_float(value: Any, *, name: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise APIError("ORB-API-001", f"{name} must be a finite number") from exc
    if not math.isfinite(out):
        raise APIError("ORB-API-001", f"{name} must be a finite number")
    return out


def _motion_to_pixel_rate(
    item: dict[str, Any], wcs: WCS, cx: float, cy: float
) -> tuple[float, float]:
    """Parse one sky-plane motion input into (dx,dy) px/day using actual WCS.

    Public inputs use sky-plane arcsec/day only (D-17). Pixel/day values belong
    exclusively to the internal shift-and-stack core and are rejected from public
    input even when accompanied by valid sky-plane fields.

    Only one representation may be supplied per vector: either
    (mu_east_arcsec_per_day, mu_north_arcsec_per_day) or
    (rate_arcsec_per_day, position_angle_deg). Mixing keys from both
    representations is rejected.
    """
    # Reject any pixel/day keys in public input.
    for px_key in ("dx_px_per_day", "dy_px_per_day"):
        if px_key in item:
            raise APIError(
                "ORB-API-001",
                f"{px_key} is internal to the core; public motion input uses "
                "sky-plane arcsec/day (mu_east_arcsec_per_day/mu_north_arcsec_per_day "
                "or rate_arcsec_per_day/position_angle_deg)",
            )

    has_mu_e = "mu_east_arcsec_per_day" in item
    has_mu_n = "mu_north_arcsec_per_day" in item
    has_rate = "rate_arcsec_per_day" in item
    has_pa = "position_angle_deg" in item

    # Reject mixing keys from both representations.
    has_mu_key = has_mu_e or has_mu_n
    has_rate_key = has_rate or has_pa
    if has_mu_key and has_rate_key:
        raise APIError(
            "ORB-API-001",
            "motion vector mixes mu_east/mu_north keys with rate/position_angle keys; "
            "supply only one representation",
        )

    # Require both components of the chosen representation.
    if has_mu_key:
        if not (has_mu_e and has_mu_n):
            missing = "mu_east_arcsec_per_day" if not has_mu_e else "mu_north_arcsec_per_day"
            raise APIError(
                "ORB-API-001",
                f"motion vector requires both mu_east_arcsec_per_day and "
                f"mu_north_arcsec_per_day; {missing} is missing",
            )
        mu_e = _coerce_finite_float(item["mu_east_arcsec_per_day"], name="mu_east_arcsec_per_day")
        mu_n = _coerce_finite_float(item["mu_north_arcsec_per_day"], name="mu_north_arcsec_per_day")
        return _mu_east_north_to_pixel_per_day(mu_e, mu_n, wcs, cx, cy)

    if has_rate_key:
        if not (has_rate and has_pa):
            missing = "rate_arcsec_per_day" if not has_rate else "position_angle_deg"
            raise APIError(
                "ORB-API-001",
                f"motion vector requires both rate_arcsec_per_day and "
                f"position_angle_deg; {missing} is missing",
            )
        rate = _coerce_finite_float(item["rate_arcsec_per_day"], name="rate_arcsec_per_day")
        if rate < 0:
            raise APIError(
                "ORB-API-001",
                "rate_arcsec_per_day must be non-negative",
            )
        pa = math.radians(
            _coerce_finite_float(item["position_angle_deg"], name="position_angle_deg")
        )
        # PA: 0=N, 90=E (D-17 provisional convention).
        mu_e = rate * math.sin(pa)
        mu_n = rate * math.cos(pa)
        return _mu_east_north_to_pixel_per_day(mu_e, mu_n, wcs, cx, cy)

    raise APIError(
        "ORB-API-001",
        "motion vector requires mu_east_arcsec_per_day/mu_north_arcsec_per_day, "
        "or rate_arcsec_per_day/position_angle_deg (sky-plane arcsec/day only; "
        "pixel/day belongs to the internal core)",
    )


def _expand_numeric_spec(spec: Any, *, default_steps: int) -> list[float]:
    if spec is None:
        return [0.0]
    if isinstance(spec, bool):
        raise APIError("ORB-API-001", "numeric motion spec must be number or list")
    if isinstance(spec, (int, float)):
        return [_coerce_finite_float(spec, name="numeric motion spec")]
    if isinstance(spec, list):
        if not spec:
            raise APIError("ORB-API-001", "numeric motion spec list must not be empty")
        is_pair = len(spec) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in spec
        )
        if is_pair:
            lo = _coerce_finite_float(spec[0], name="numeric motion spec")
            hi = _coerce_finite_float(spec[1], name="numeric motion spec")
            if default_steps <= 1 or math.isclose(lo, hi):
                return [lo] if default_steps <= 1 else [lo, hi]
            return [lo + (hi - lo) * i / (default_steps - 1) for i in range(default_steps)]
        return [_coerce_finite_float(v, name="numeric motion spec") for v in spec]
    raise APIError("ORB-API-001", "numeric motion spec must be number or list")


def _parse_motion_vectors(
    params: dict[str, Any], wcs: WCS, cx: float, cy: float, policy: dict[str, Any]
) -> list[MotionVector]:
    raw = params.get("motion_vectors")
    vectors: list[MotionVector] = []
    if raw is not None:
        if not isinstance(raw, list) or not raw:
            raise APIError("ORB-API-001", "motion_vectors must be a non-empty list")
        for item in raw:
            if not isinstance(item, dict):
                raise APIError("ORB-API-001", "each motion vector must be an object")
            dx, dy = _motion_to_pixel_rate(item, wcs, cx, cy)
            vectors.append(MotionVector(dx, dy))
    else:
        # Grid form (provisional): rate/PA or mu_east/mu_north specs.
        if "mu_east_arcsec_per_day" in params or "mu_north_arcsec_per_day" in params:
            mu_e_vals = _expand_numeric_spec(
                params.get("mu_east_arcsec_per_day"),
                default_steps=int(params.get("mu_east_steps", 1) or 1),
            )
            mu_n_vals = _expand_numeric_spec(
                params.get("mu_north_arcsec_per_day"),
                default_steps=int(params.get("mu_north_steps", 1) or 1),
            )
            for mu_e in mu_e_vals:
                for mu_n in mu_n_vals:
                    dx, dy = _mu_east_north_to_pixel_per_day(float(mu_e), float(mu_n), wcs, cx, cy)
                    vectors.append(MotionVector(dx, dy))
        if not vectors:
            rate_spec = params.get("rate_arcsec_per_day")
            pa_spec = params.get("position_angle_deg")
            if rate_spec is None and pa_spec is None:
                # D-17 default: generated sky-motion grid; step/radius come
                # from max_time_span_days and the D-17 prior in the policy.
                vectors = _default_motion_vectors(wcs, cx, cy, policy)
            else:
                rates = _expand_numeric_spec(
                    rate_spec, default_steps=int(params.get("rate_steps", 1) or 1)
                )
                pas = _expand_numeric_spec(
                    pa_spec, default_steps=int(params.get("pa_steps", 1) or 1)
                )
                for rate in rates:
                    for pa_deg in pas:
                        pa = math.radians(pa_deg)
                        mu_e = float(rate) * math.sin(pa)
                        mu_n = float(rate) * math.cos(pa)
                        dx, dy = _mu_east_north_to_pixel_per_day(mu_e, mu_n, wcs, cx, cy)
                        vectors.append(MotionVector(dx, dy))
        if not vectors:
            raise APIError("ORB-API-001", "motion grid produced no vectors")

    # D-17/D-24: zero-motion trial must remain in the motion grid.
    has_zero = any(
        abs(v.dx_px_per_day) <= ZERO_MOTION_EPS and abs(v.dy_px_per_day) <= ZERO_MOTION_EPS
        for v in vectors
    )
    if not has_zero:
        vectors.append(MotionVector(0.0, 0.0))
    return vectors


def _resolve_search_max_work(settings: Any | None) -> int:
    """Return authoritative search work budget; never silently ignore invalid config."""
    if settings is None:
        raw = _DEFAULT_SEARCH_MAX_WORK
    else:
        if not hasattr(settings, "search_max_work"):
            raise APIError("ORB-SYS-001", "search_max_work configuration is missing")
        raw = settings.search_max_work
        if raw is None:
            raise APIError("ORB-SYS-001", "search_max_work configuration is missing")
    try:
        budget_f = float(raw)
    except Exception as exc:
        raise APIError(
            "ORB-SYS-001",
            "search_max_work configuration is invalid",
            detail={"search_max_work": raw},
        ) from exc
    if not math.isfinite(budget_f) or budget_f <= 0:
        raise APIError(
            "ORB-SYS-001",
            "search_max_work configuration is invalid",
            detail={"search_max_work": raw},
        )
    return int(budget_f)


def _validate_min_coverage_fraction(value: Any) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise APIError("ORB-API-001", "min_coverage_fraction must be between 0 and 1") from exc
    if not math.isfinite(out) or out < 0.0 or out > 1.0:
        raise APIError("ORB-API-001", "min_coverage_fraction must be between 0 and 1")
    return out


def _validate_background_variant(value: Any) -> str:
    """D-24 background/static variant selector; default F (PROVISIONAL(G1)).

    Returns the requested variant only — the handler applies the >=5-frame
    guard and records the variant actually used. Variant rationale and
    measurements live in SDD D-24 (RS.09 study).
    """
    if value is None:
        return "f"
    v = str(value).strip().lower()
    if v not in BACKGROUND_VARIANTS:
        raise APIError(
            "ORB-API-001",
            "background_variant must be one of "
            + ", ".join(BACKGROUND_VARIANTS)
            + " (D-24: a=none, b=zodi-subtract, c=high-pass, d=c+static-mask, "
            + "e=difference-imaging, f=e+high-pass)",
        )
    return v


def _to_core_frame(fr: DecodedFrame, image: np.ndarray) -> Frame:
    """Shared tail: invalid pixels -> NaN in image and variance, then Frame."""
    var = np.asarray(fr.variance, dtype=np.float64).copy()
    valid = np.asarray(fr.mask, dtype=bool) & np.isfinite(image)
    invalid = ~valid | ~np.isfinite(var) | (var <= 0)
    image = np.where(invalid, np.nan, image)
    var = np.where(invalid, np.nan, var)
    return Frame(image=image, variance=var, time=float(fr.mjd), frame_id=fr.frame_ref)


def _difference_image_frames(frames: list[DecodedFrame]) -> list[Frame]:
    """Variant E/F core: subtract the per-pixel temporal coadd at a low quantile.

    Static sources sit in every epoch, so a low quantile removes them, while a
    mover present in only some epochs leaves background in the quantile sample
    and survives subtraction. At ``DIFFERENCE_QUANTILE`` (5) a pixel needs just
    two mover-free epochs to land on background — the honest floor for the
    ~5 independent epochs a single-field stack has. The higher q25 used before
    D-24 sampled a mover that repeats across near-simultaneous exposures,
    self-subtracting ~60% of its peak (the ghost-trail finding: duplicate
    exposures within ~2 min make the mover part of its own reference).

    Pixels with fewer than ``DIFFERENCE_MIN_VALID_EPOCHS`` valid epochs are set
    to NaN: with 1-2 epochs the "difference" is meaningless (often exactly
    zero) and would masquerade as a valid measurement. The handler refuses
    E/F outright below 5 frames (same guard class as variant D).
    """
    stack = []
    for fr in frames:
        img = np.asarray(fr.image, dtype=np.float64)
        valid = np.asarray(fr.mask, dtype=bool) & np.isfinite(img)
        stack.append(np.where(valid, img, np.nan))
    cube = np.stack(stack, axis=0)
    n_valid = np.isfinite(cube).sum(axis=0)
    # All-NaN slices are expected (pixels valid in <3 epochs are NaN'd
    # below); suppress the harmless RuntimeWarning flood.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        coadd = np.nanpercentile(cube, DIFFERENCE_QUANTILE, axis=0)
    coadd = np.where(n_valid >= DIFFERENCE_MIN_VALID_EPOCHS, coadd, np.nan)
    return [_to_core_frame(fr, img - coadd) for fr, img in zip(frames, stack, strict=True)]


def _background_frames(variant: str, frames: list[DecodedFrame]) -> list[Frame]:
    """Apply the D-24 background/static preprocessing variant (PROVISIONAL(G1)).

    A: no preprocessing. B: subtract a smooth background (large-median ZODI
    stand-in for synthetic fixtures; real ZODI is a model product).
    C: per-frame 15 px median high-pass. D is C plus static-source masking,
    applied by the caller after detection masks exist. E: difference imaging —
    subtract the per-pixel temporal coadd at ``DIFFERENCE_QUANTILE`` from every
    frame (removes static sources and smooth background without masking mover
    flux). F: E followed by the 15 px per-frame high-pass (absorbs
    epoch-varying smooth background that pure difference imaging leaves
    behind).

    E/F score calibration caveat: the low-quantile subtraction adds a small
    positive bias (~+0.5 sigma per pixel) and correlated extra noise
    (~0.3 sigma^2) that the stored variance map does not model, so stack scores
    under E/F are not calibrated sigma units — the median+MAD extraction
    threshold absorbs the bias but per-candidate stack_snr should be read as
    relative, not absolute, significance (RS.09/D-24).
    """
    if variant in ("e", "f"):
        diff_frames = _difference_image_frames(frames)
        if variant == "e":
            return diff_frames
        # F: per-frame 15 px median high-pass on the difference images.
        out_frames: list[Frame] = []
        for fr in diff_frames:
            img = np.asarray(fr.image, dtype=np.float64)
            valid = np.isfinite(img)
            if np.any(valid):
                fill = float(np.median(img[valid]))
                work = np.where(valid, img, fill)
                bkg = median_filter(work, size=HIGHPASS_MEDIAN_SIZE_PX, mode="nearest")
                out = np.where(valid, work - bkg, np.nan)
            else:
                out = np.full_like(img, np.nan, dtype=np.float64)
            var = np.asarray(fr.variance, dtype=np.float64)
            invalid = ~np.isfinite(out) | ~np.isfinite(var) | (var <= 0)
            out = np.where(invalid, np.nan, out)
            var = np.where(invalid, np.nan, var)
            out_frames.append(
                Frame(image=out, variance=var, time=float(fr.time), frame_id=fr.frame_id)
            )
        return out_frames

    core_frames: list[Frame] = []
    for fr in frames:
        img = np.asarray(fr.image, dtype=np.float64)
        valid = np.asarray(fr.mask, dtype=bool) & np.isfinite(img)
        if variant == "a":
            out = img.copy()
        else:
            work = img.copy()
            if np.any(valid):
                fill = float(np.median(work[valid]))
                work[~valid] = fill
                if variant == "b":
                    bkg = median_filter(work, size=BACKGROUND_ZODI_MEDIAN_SIZE_PX, mode="nearest")
                else:  # c and d share the 15 px high-pass
                    bkg = median_filter(work, size=HIGHPASS_MEDIAN_SIZE_PX, mode="nearest")
                out = work - bkg
                out = np.where(valid, out, np.nan)
            else:
                out = np.full_like(img, np.nan, dtype=np.float64)
        core_frames.append(_to_core_frame(fr, out))
    return core_frames


def _mask_static_sources(frames: list[Frame]) -> list[Frame]:
    """D-24 variant D: mask stationary sources detected on the median coadd.

    The frames share one grid, so a pixel-wise median across epochs preserves
    anything that does not move (static stars, persistence artefacts) while
    suppressing movers, which are present at any given pixel in only a
    minority of epochs. Sources above a robust MAD threshold on the median
    image are dilated by STATIC_MASK_DILATION_PX and masked to NaN in every
    frame. This replaces the earlier per-pixel detection-coincidence mask,
    which could not distinguish a static source from a slow mover whose PSF
    overlaps itself across epochs (RS.09 study finding).
    """
    from scipy import ndimage

    n = len(frames)
    if n < STATIC_MASK_MIN_FRAMES:
        # With fewer epochs the pixel-wise median is not robust enough to
        # separate persistent sources from movers (RS.09 review).
        return frames
    stack = np.stack([np.asarray(f.image, dtype=np.float64) for f in frames])
    with np.errstate(all="ignore"):
        med_img = np.nanmedian(stack, axis=0)
    valid = np.isfinite(med_img)
    if not valid.any():
        return frames
    vals = med_img[valid]
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med)))
    sigma = 1.4826 * mad
    if sigma <= 0:
        return frames
    # Compact-peak masking: only local maxima on the coadd are masked (small
    # disk), so an elongated streak left by a slow mover is not erased.
    peaks = valid & (med_img >= med + STATIC_MASK_MEDIAN_SIGMA * sigma)
    peaks = peaks & (med_img == ndimage.maximum_filter(med_img, size=3, mode="nearest"))
    static = ndimage.binary_dilation(peaks, iterations=STATIC_MASK_DILATION_PX)
    if not static.any():
        return frames
    out: list[Frame] = []
    for fr in frames:
        img = np.array(fr.image, dtype=np.float64, copy=True)
        var = None if fr.variance is None else np.array(fr.variance, dtype=np.float64, copy=True)
        img[static] = np.nan
        if var is not None:
            var[static] = np.nan
        out.append(Frame(image=img, variance=var, time=float(fr.time), frame_id=fr.frame_id))
    return out


def _frame_detection_masks(
    frames: list[Frame], *, sigma_threshold: float
) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
    """Deterministic per-frame detection masks for static-source heuristic (PROVISIONAL(G1)).

    Computes per-frame significance z = img / sqrt(var) on high-pass filtered
    frames and flags pixels above med + sigma_threshold * robust_sigma.
    The high-pass filter changes per-pixel noise properties, so the per-frame
    significance is NOT fully calibrated after filtering; this heuristic is
    provisional pending ML measurement (D-24).
    """
    masks: list[np.ndarray] = []
    stats: list[dict[str, Any]] = []
    for fr in frames:
        img = np.asarray(fr.image, dtype=np.float64)
        var = np.asarray(fr.variance, dtype=np.float64)
        valid = np.isfinite(img) & np.isfinite(var) & (var > 0)
        z = np.full(img.shape, np.nan, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            z[valid] = img[valid] / np.sqrt(var[valid])
        finite = np.isfinite(z)
        if finite.any():
            vals = z[finite]
            med = float(np.median(vals))
            mad = float(np.median(np.abs(vals - med)))
            sigma = 1.4826 * mad
            thr = med + sigma_threshold * sigma if sigma > 0 else med + sigma_threshold
            det = finite & (z >= thr)
            stats.append(
                {
                    "median": med,
                    "mad": mad,
                    "sigma": sigma,
                    "threshold": thr,
                    "detections": int(det.sum()),
                }
            )
        else:
            det = np.zeros(img.shape, dtype=bool)
            stats.append(
                {
                    "median": None,
                    "mad": None,
                    "sigma": None,
                    "threshold": None,
                    "detections": 0,
                }
            )
        masks.append(det)
    return masks, stats


def _find_zero_motion_trial_index(motion_vectors: Iterable[MotionVector]) -> int | None:
    for i, mv in enumerate(motion_vectors):
        if (
            abs(float(mv.dx_px_per_day)) <= ZERO_MOTION_EPS
            and abs(float(mv.dy_px_per_day)) <= ZERO_MOTION_EPS
        ):
            return i
    return None


def _static_source_evidence(
    *,
    x: int,
    y: int,
    zero_trial_index: int | None,
    best_trial_index: int,
    detection_masks: list[np.ndarray],
    winning_displacement_px: float | None = None,
    baseline_days: float | None = None,
) -> dict[str, Any]:
    in_bounds = (
        detection_masks
        and 0 <= y < detection_masks[0].shape[0]
        and 0 <= x < detection_masks[0].shape[1]
    )
    if in_bounds:
        detected_count = int(sum(bool(m[y, x]) for m in detection_masks))
    else:
        detected_count = 0
    n = len(detection_masks)
    fraction = (detected_count / n) if n else 0.0
    best_is_zero = zero_trial_index is not None and int(best_trial_index) == int(zero_trial_index)
    # D-24 static heuristic: the winning trial must correspond to a source that
    # effectively does not move over the observed baseline (< ~1 px total
    # displacement, i.e. well under the PSF) AND be detected in >=50% of
    # frames. The earlier exact-zero-trial test under-fired because the default
    # grid contains many near-zero trials and noise picks among them (RS.09).
    max_disp = STATIC_SOURCE_MAX_DISPLACEMENT_PX
    flagged = bool(
        winning_displacement_px is not None
        and winning_displacement_px < max_disp
        and fraction >= STATIC_SOURCE_MIN_FRAME_FRACTION
    )
    return {
        "heuristic": "d24_low_displacement_and_multiframe_detection_provisional_g1",
        "provisional": True,
        "best_trial_is_zero_motion": best_is_zero,
        "winning_displacement_px": winning_displacement_px,
        "baseline_days": baseline_days,
        "max_displacement_px": max_disp,
        "zero_motion_trial_index": zero_trial_index,
        "detected_in_frames": detected_count,
        "total_frames": n,
        "detected_fraction": fraction,
        "min_frame_fraction": STATIC_SOURCE_MIN_FRAME_FRACTION,
        "flagged_static_source": flagged,
    }


def _refine_centroid(score: np.ndarray, x: int, y: int, radius: int = 1) -> tuple[float, float]:
    """Deterministic local intensity-weighted centroid refinement on score map."""
    h, w = score.shape
    y0 = max(0, y - radius)
    y1 = min(h, y + radius + 1)
    x0 = max(0, x - radius)
    x1 = min(w, x + radius + 1)
    patch = np.asarray(score[y0:y1, x0:x1], dtype=np.float64)
    ys, xs = np.mgrid[y0:y1, x0:x1]
    weights = np.where(np.isfinite(patch), np.maximum(patch, 0.0), 0.0)
    s = float(weights.sum())
    if s <= 0.0 or not np.isfinite(s):
        return float(x), float(y)
    cx = float((xs * weights).sum() / s)
    cy = float((ys * weights).sum() / s)
    return cx, cy


def _extract_candidates(
    result: Any,
    *,
    detection_threshold_sigma: float | None,
    nms_radius_px: int,
    edge_margin_px: int,
    min_coverage_count: int,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Provisional G1 extraction from best-score maps (RS.03, D-24 caveats).

    Uses best_score_map for peak scores and best_trial_index_map at selected pixel
    for winning motion trial. Includes peak finding, centroid refinement, edge
    exclusion, and low-coverage exclusion. Threshold/NMS are PROVISIONAL(G1).

    Score-map units: surface-brightness signal divided by sqrt(variance), i.e.
    sigma units of detection significance for an individual trial. Because the
    best-score map is a maximum across multiple motion trials, its noise-only
    distribution is NOT the same as an individual trial; the observed per-seed
    standard deviation (~0.993 for seeded Gaussian noise) does not prove universal
    5-sigma calibration of the max-over-trials map. The default 5-sigma threshold
    is therefore explicitly PROVISIONAL(G1) pending ML measurement.

    RS.09 measured (2026-11-11, scripts/rs09_variant_study.py) that this
    normalization is also source-contaminated: matched fixture fields with
    injected movers showed score-map median 3.5 -> 22.8 and MAD-sigma
    3.3 -> 28.3, because source signal leaks into the max-over-trials map
    broadly. A fixed sigma threshold therefore means different cuts on
    different fields, and no constant sigma achieved ~1 unflagged false alarm
    per control field without zeroing mover recall. Proper calibration needs a
    per-trial noise model or an empirical null — tracked as an RS.03/D-24
    follow-up, not solved by tuning this constant.

    The MAD-based robust noise estimate (sigma_est = 1.4826 * MAD) is applied
    once; no double scaling occurs. When sigma_est is zero or invalid (for example
    a constant score map), raw score units cannot be interpreted as sigma, and no
    candidates are returned with an explicit diagnostic.
    """
    score = np.asarray(result.best_score_map, dtype=np.float64)
    trial_idx = np.asarray(result.best_trial_index_map, dtype=np.int64)
    coverage = np.asarray(result.best_coverage_map, dtype=np.int64)
    h, w = score.shape

    finite = np.isfinite(score) & (trial_idx >= 0) & (coverage >= int(min_coverage_count))
    if edge_margin_px > 0:
        yy, xx = np.indices(score.shape)
        finite &= (yy >= edge_margin_px) & (yy < h - edge_margin_px)
        finite &= (xx >= edge_margin_px) & (xx < w - edge_margin_px)

    if not finite.any():
        return [], "none", None

    vals = score[finite]
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med)))
    sigma_est = 1.4826 * mad
    if detection_threshold_sigma is None:
        sigma_thr = EXTRACTION_DEFAULT_SIGMA_THRESHOLD
        method = "provisional_sigma_threshold_nms"
    else:
        sigma_thr = float(detection_threshold_sigma)
        method = "explicit_sigma_threshold_nms"

    diagnostic: str | None = None
    if not (np.isfinite(sigma_est) and sigma_est > 0):
        # A zero or invalid robust noise estimate means raw score units cannot be
        # interpreted as sigma. Do not manufacture a fallback noise floor.
        diagnostic = (
            "zero_or_invalid_mad_sigma_estimate; no candidates extracted "
            "(robust noise estimate cannot support a sigma threshold)"
        )
        return [], method, diagnostic

    # Threshold in sigma units (PROVISIONAL(G1)).
    threshold = med + sigma_thr * sigma_est

    mask = finite & (score >= float(threshold))
    if not mask.any():
        return [], method, diagnostic

    ys, xs = np.where(mask)
    # Deterministic order: score desc, y asc, x asc.
    order = np.lexsort((xs, ys, -score[ys, xs]))
    taken = np.zeros(score.shape, dtype=bool)
    out: list[dict[str, Any]] = []
    for idx in order:
        y, x = int(ys[idx]), int(xs[idx])
        if taken[y, x]:
            continue
        t = int(trial_idx[y, x])
        if t < 0 or t >= len(result.motion_vectors):
            continue
        cx, cy = _refine_centroid(score, x, y, radius=1)
        out.append(
            {
                "x": int(x),
                "y": int(y),
                "x_centroid": float(cx),
                "y_centroid": float(cy),
                "trial_index": t,
                "stack_snr": float(score[y, x]),
                "coverage": int(coverage[y, x]),
                "threshold_sigma": float(sigma_thr),
                "score_threshold": float(threshold),
                "score_sigma_estimate": float(sigma_est),
            }
        )
        y0 = max(0, y - nms_radius_px)
        y1 = min(h, y + nms_radius_px + 1)
        x0 = max(0, x - nms_radius_px)
        x1 = min(w, x + nms_radius_px + 1)
        taken[y0:y1, x0:x1] = True
    return out, method, diagnostic


def _decode_stored_frames(
    rows: list[dict[str, Any]],
) -> tuple[list[DecodedFrame], list[dict[str, Any]]]:
    """Decode and validate stored frame blobs into ``DecodedFrame`` records.

    Shared by ``moving_object_search`` and the VAL.04 validation handler so
    both read the same stored payload contract (SDD §7.4, D-29).
    """
    decoded_frames: list[DecodedFrame] = []
    frame_meta: list[dict[str, Any]] = []
    for row in rows:
        blob = row.get("array_blob")
        if not blob:
            raise APIError(
                "ORB-RS-001",
                f"frame {row.get('frame_ref')} has no stored array payload",
            )
        try:
            decoded = decode_frame_array(bytes(blob))
        except Exception as exc:
            raise APIError(
                "ORB-RS-001",
                f"frame {row.get('frame_ref')} has an unreadable array payload",
            ) from exc
        image = np.asarray(decoded.data, dtype=np.float64)
        if decoded.variance is None:
            raise APIError(
                "ORB-RS-001",
                f"frame {row.get('frame_ref')} is missing valid variance",
            )
        variance = np.asarray(decoded.variance, dtype=np.float64)
        if variance.shape != image.shape:
            raise APIError(
                "ORB-RS-001",
                f"frame {row.get('frame_ref')} variance shape does not match image shape",
                detail={"image_shape": list(image.shape), "variance_shape": list(variance.shape)},
            )
        if not np.isfinite(variance).any():
            raise APIError(
                "ORB-RS-001",
                f"frame {row.get('frame_ref')} is missing valid variance",
            )
        mjd = row.get("mjd")
        if mjd is None:
            raise APIError("ORB-RS-001", f"frame {row.get('frame_ref')} is missing mjd/time")
        mask = np.asarray(decoded.mask, dtype=bool)
        if mask.shape != image.shape:
            raise APIError("ORB-RS-001", f"frame {row.get('frame_ref')} mask shape mismatch")
        flags = None if decoded.flags is None else np.asarray(decoded.flags)
        if flags is not None and flags.shape != image.shape:
            raise APIError("ORB-RS-001", f"frame {row.get('frame_ref')} flags shape mismatch")
        meta = dict(row.get("meta") or {})
        df = DecodedFrame(
            frame_ref=str(row.get("frame_ref")),
            image=image,
            variance=variance,
            mask=mask,
            flags=flags,
            mjd=float(mjd),
            detector=row.get("detector"),
            wavelength_um=row.get("wavelength_um"),
            meta=meta,
        )
        decoded_frames.append(df)
        frame_meta.append(
            {
                "frame_ref": df.frame_ref,
                "mjd": df.mjd,
                "detector": df.detector,
                "wavelength_um": df.wavelength_um,
                "wcs_cards": meta.get("wcs_cards"),
            }
        )
    return decoded_frames, frame_meta


_FRAME_WINDOW_KEYS = {
    "n_frames",
    "mjd_min",
    "mjd_max",
    "max_span_days",
    "min_time_separation_days",
    "wavelength_min_um",
    "wavelength_max_um",
}

# PROVISIONAL(G1): TODO(ML) DEFAULT_FRAME_MIN_TIME_SEPARATION_DAYS
# SPHEREx visits put exposures minutes apart. A mover at a few arcsec/day
# barely moves in that time, so they land on the same pixels and become part
# of their own difference-imaging reference (the ghost-trail finding, D-24);
# their wavelength also sweeps through the visit, so they are not repeats.
# 0.1 d (2.4 h) separates distinct epochs on the tutorial field while keeping
# genuine repeat visits; presets may override.
DEFAULT_FRAME_MIN_TIME_SEPARATION_DAYS = 0.1


def select_frame_rows(
    rows: list[dict[str, Any]], params: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Apply ``params['frame_window']`` to stored frame rows.

    ``frame_window`` is a dict with optional keys:

    - ``mjd_min``/``mjd_max``: keep frames whose MJD is inside the bounds.
    - ``max_span_days``: keep only the densest contiguous-in-time run whose
      MJD span is <= this value. Needed for multi-epoch fields (e.g. R Dra):
      the stack policy caps ``max_time_span_days`` at 7 d, so an even thinning
      across all epochs would still fail ORB-RS-002.
    - ``min_time_separation_days``: collapse one visit's exposures into a single
      effective epoch before thinning. Exposures less than this apart are one
      measurement of the same sky at nearly the same time: a mover barely
      moves between them, so they repeat the same pixels and become part of
      their own difference-imaging reference (the "ghost trail", D-24), and on
      SPHEREx their wavelength sweeps through the visit, so they are different
      spectral channels rather than repeats. Applied after the
      wavelength/MJD/``max_span_days`` filters and before ``n_frames`` so
      thinning selects distinct epochs. The **earliest** exposure of each
      cluster is kept (one channel per visit; ties -> earliest ``frame_ref``).
      Defaults to :data:`DEFAULT_FRAME_MIN_TIME_SEPARATION_DAYS`; presets may
      override.
    - ``n_frames``: thin the survivors to this many frames, evenly spaced in
      MJD (nearest unused frame to each evenly spaced target epoch).
    - ``wavelength_min_um``/``wavelength_max_um``: keep frames whose
      wavelength_um is inside the bounds. Needed because real fields span
      all six SPHEREx bands (~0.75-5 um) while the stack policy caps the
      combinable wavelength span at 0.5 um — a valid search almost always
      needs a single band.

    Mutually exclusive with ``frame_refs``. Returns ``(rows, applied-spec)``;
    the applied spec is None when no window was requested. The caller's
    params dict is never mutated.
    """
    window = params.get("frame_window")
    if window is None:
        return rows, None
    if params.get("frame_refs") is not None:
        raise APIError("ORB-API-001", "frame_window and frame_refs are mutually exclusive")
    if not isinstance(window, dict) or not window:
        raise APIError("ORB-API-001", "frame_window must be a non-empty object")
    spec = dict(window)  # never mutate the caller's params
    unknown = set(spec) - _FRAME_WINDOW_KEYS
    if unknown:
        raise APIError(
            "ORB-API-001",
            f"frame_window has unknown keys: {sorted(unknown)}",
            detail={"allowed": sorted(_FRAME_WINDOW_KEYS)},
        )

    n_frames = spec.get("n_frames")
    if n_frames is not None:
        raw_n = n_frames
        try:
            n_frames = int(raw_n)
            integral = float(raw_n).is_integer()
        except (TypeError, ValueError) as exc:
            raise APIError("ORB-API-001", "frame_window.n_frames must be an int >= 1") from exc
        if isinstance(raw_n, bool) or not integral or n_frames < 1:
            raise APIError("ORB-API-001", "frame_window.n_frames must be an int >= 1")
    bounds: dict[str, float] = {}
    for name in ("mjd_min", "mjd_max", "max_span_days", "wavelength_min_um", "wavelength_max_um"):
        val = spec.get(name)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError) as exc:
            raise APIError("ORB-API-001", f"frame_window.{name} must be numeric") from exc
        if not np.isfinite(val):
            raise APIError("ORB-API-001", f"frame_window.{name} must be finite")
        bounds[name] = val
    mjd_min = bounds.get("mjd_min")
    mjd_max = bounds.get("mjd_max")
    max_span = bounds.get("max_span_days")
    if max_span is not None and max_span <= 0:
        raise APIError("ORB-API-001", "frame_window.max_span_days must be > 0")
    if mjd_min is not None and mjd_max is not None and mjd_min > mjd_max:
        raise APIError("ORB-API-001", "frame_window.mjd_min must be <= mjd_max")
    wave_min = bounds.get("wavelength_min_um")
    wave_max = bounds.get("wavelength_max_um")
    if wave_min is not None and wave_max is not None and wave_min > wave_max:
        raise APIError("ORB-API-001", "frame_window.wavelength_min_um must be <= wavelength_max_um")
    raw_min_sep = spec.get("min_time_separation_days")
    min_sep = DEFAULT_FRAME_MIN_TIME_SEPARATION_DAYS
    if raw_min_sep is not None:
        try:
            min_sep = float(raw_min_sep)
        except (TypeError, ValueError) as exc:
            raise APIError(
                "ORB-API-001", "frame_window.min_time_separation_days must be numeric"
            ) from exc
        if not np.isfinite(min_sep) or min_sep < 0:
            raise APIError(
                "ORB-API-001",
                "frame_window.min_time_separation_days must be finite and >= 0",
            )

    # Windowed selection needs a time axis; _decode_stored_frames requires
    # mjd anyway, so fail early with a clearer message.
    for row in rows:
        mjd = row.get("mjd")
        if mjd is None or not np.isfinite(float(mjd)):
            raise APIError(
                "ORB-RS-001",
                f"frame {row.get('frame_ref')} is missing mjd; cannot apply frame_window",
            )

    selected = list(rows)
    n_wavelength_dropped = 0
    if wave_min is not None or wave_max is not None:
        # count frames lost to the wavelength filter, including any with
        # no wavelength_um (they cannot be proven inside the bounds)
        n_wavelength_dropped = sum(
            1
            for r in rows
            if r.get("wavelength_um") is None
            or (wave_min is not None and float(r["wavelength_um"]) < wave_min)
            or (wave_max is not None and float(r["wavelength_um"]) > wave_max)
        )
        selected = [
            r
            for r in selected
            if r.get("wavelength_um") is not None
            and (wave_min is None or float(r["wavelength_um"]) >= wave_min)
            and (wave_max is None or float(r["wavelength_um"]) <= wave_max)
        ]
    if mjd_min is not None or mjd_max is not None:
        selected = [
            r
            for r in selected
            if (mjd_min is None or float(r["mjd"]) >= mjd_min)
            and (mjd_max is None or float(r["mjd"]) <= mjd_max)
        ]
    if not selected:
        raise APIError("ORB-RS-001", "frame_window selected no frames")

    if max_span is not None and len(selected) > 1:
        # Densest run of frames whose mjd span stays within max_span_days.
        # Sliding window over the mjd-sorted list; ties keep the earliest run.
        ordered = sorted(selected, key=lambda r: (float(r["mjd"]), str(r["frame_ref"])))
        mjds = [float(r["mjd"]) for r in ordered]
        # Every single frame qualifies (span 0 <= max_span), so a size-1
        # window is always a valid fallback.
        best_lo, best_hi = 0, 1
        j = 0
        for i in range(len(ordered)):
            while j < len(ordered) and mjds[j] - mjds[i] <= max_span:
                j += 1
            # frames i..j-1 span <= max_span (j is the exclusive upper bound)
            if j - i > best_hi - best_lo:
                best_lo, best_hi = i, j
        selected = ordered[best_lo:best_hi]

    n_time_deduplicated = 0
    if min_sep > 0 and len(selected) > 1:
        # Collapse a visit's exposures into one effective epoch before
        # thinning. Single-linkage by time gap: a frame joins the current
        # cluster if it is less than min_sep from the previous frame (so a run
        # of closely spaced frames is one epoch even if its total span
        # exceeds min_sep).
        #
        # Keep the EARLIEST frame of each cluster, not the one nearest the
        # median MJD. Within a SPHEREx visit the frames are not repeats: the
        # wavelength sweeps through the visit (tutorial field, 6 min:
        # 1.048 -> 1.073 -> 1.098 -> 1.121 um), so later exposures are
        # different spectral channels. The earliest exposure anchors one
        # channel per visit, which keeps the difference-imaging coadd
        # spectrally consistent; picking a later one can drag the bright
        # long-wavelength edge into the coadd and leave a ~3 MJy/sr pedestal
        # in the other frames (RS.08, 2026-11-12). Ties -> earliest frame_ref.
        ordered = sorted(selected, key=lambda r: (float(r["mjd"]), str(r["frame_ref"])))
        clusters: list[list[dict[str, Any]]] = []
        for row in ordered:
            if clusters and float(row["mjd"]) - float(clusters[-1][-1]["mjd"]) < min_sep:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        kept = [cluster[0] for cluster in clusters]
        n_time_deduplicated = len(selected) - len(kept)
        selected = kept

    if n_frames is not None and n_frames < len(selected):
        ordered = sorted(selected, key=lambda r: (float(r["mjd"]), str(r["frame_ref"])))
        mjds = np.array([float(r["mjd"]) for r in ordered])
        # Evenly spaced target epochs; nearest unused frame to each target.
        targets = np.linspace(mjds[0], mjds[-1], n_frames)
        used: set[int] = set()
        keep: list[int] = []
        for t in targets:
            dists = np.abs(mjds - t)
            for idx in np.argsort(dists, kind="stable"):
                i = int(idx)
                if i not in used:
                    used.add(i)
                    keep.append(i)
                    break
        selected = [ordered[i] for i in sorted(keep)]

    applied = {
        "frame_refs": [str(r.get("frame_ref")) for r in selected],
        "n_frames_selected": len(selected),
        "n_frames_available": len(rows),
        "mjd_min": mjd_min,
        "mjd_max": mjd_max,
        "max_span_days": max_span,
        "wavelength_min_um": wave_min,
        "wavelength_max_um": wave_max,
        "n_wavelength_dropped": n_wavelength_dropped,
        "min_time_separation_days": min_sep,
        "n_time_deduplicated": n_time_deduplicated,
        "n_frames_requested": n_frames,
    }
    return selected, applied


@dataclass
class ScienceSearchResult:
    """Output of the shared science path (preprocess -> search -> extract).

    ``candidates`` is the full ranked extraction list (unflagged first, then
    ``stack_snr``) BEFORE the persist cap — validation matching needs every
    extracted detection for honest spurious accounting; the search handler
    applies ``EXTRACTION_MAX_PERSISTED_CANDIDATES`` itself.
    """

    result: Any
    candidates: list[dict[str, Any]]
    candidates_found: int
    variant_used: str
    variant_requested: str
    extraction_method: str
    extraction_diagnostic: str | None
    zero_trial_index: int | None
    # The exact preprocessed frames `search` consumed — kept so the handler
    # can re-combine the winning trial for the candidate stack PNG (§8.2).
    core_frames: list[Frame]
    # Variance multiplier applied so stack_snr is honest sigma units
    # (1.0 when the stored variance already matches the processed noise).
    noise_calibration: float = 1.0
    # D-24 DSP register: sub-pixel alignment kernel (1 bilinear default,
    # 3 cubic experiment). Variance propagation stays bilinear either way.
    interpolation_order: int = 1
    # D-24 DSP register: "global" median-ratio calibration or "per_frame"
    # empirical per-frame variance rescaling (PROMOTED 2026-11-15 — see the
    # register; ratios clamped to NOISE_RATIO_CLAMP around the median).
    noise_model: str = "per_frame"
    # Per-frame variance ratios actually applied, aligned with core_frames
    # order — provenance so any odd candidate can be traced to its frames.
    frame_noise_ratios: list[float] = field(default_factory=list)


def _validate_noise_model(value: Any) -> str:
    """D-24 DSP register: per-frame vs global variance calibration.

    Default is ``per_frame`` — promoted 2026-11-15 after the pre-registered
    injection study (recall 0.517 vs 0.271, spurious 25 vs 29; register
    queue item 2). ``global`` remains selectable for comparison.
    """
    if value is None:
        return "per_frame"
    v = str(value).strip().lower()
    if v not in ("global", "per_frame"):
        raise APIError(
            "ORB-API-001",
            "noise_model must be 'global' (median ratio, shipped default) "
            "or 'per_frame' (D-24 DSP experiment)",
        )
    return v


def _frame_noise_ratio(fr: Frame) -> float | None:
    """MAD-measured noise variance / median stored variance for one frame."""
    img = np.asarray(fr.image, dtype=np.float64)
    var = np.asarray(fr.variance, dtype=np.float64)
    ok = np.isfinite(img) & np.isfinite(var) & (var > 0)
    if int(ok.sum()) < 100:
        return None
    vals = img[ok]
    med = float(np.median(vals))
    sig = 1.4826 * float(np.median(np.abs(vals - med)))
    vmed = float(np.median(var[ok]))
    if sig > 0 and vmed > 0:
        return sig * sig / vmed
    return None


# D-24 DSP register: per-frame ratios are clamped to this band around the
# cross-frame median. A frame whose MAD ratio collapses (heavily masked, or
# nearly identical to the E/F coadd baseline) would otherwise shrink its
# variance until it dominates the inverse-variance stack — the same
# threshold-inflation failure class the experiment fixed, mirrored.
NOISE_RATIO_CLAMP = (0.25, 4.0)


def _clamp_noise_ratio(ratio: float, median_ratio: float) -> float:
    lo, hi = NOISE_RATIO_CLAMP
    clamped = min(max(ratio, lo * median_ratio), hi * median_ratio)
    if clamped != ratio:
        log.warning(
            "per-frame noise ratio %.4g clamped to %.4g (median %.4g, band %.2g-%.2g)",
            ratio,
            clamped,
            median_ratio,
            lo,
            hi,
        )
    return clamped


def _calibrate_frame_variances(
    core_frames: list[Frame], *, mode: str
) -> tuple[float, list[Frame], list[float]]:
    """Rescale frame variances so stack_snr is honest sigma units.

    Returns ``(noise_calibration, frames, applied_ratios)`` where the scalar
    is the median of the applied per-frame ratios (the value validation's
    flux_for_snr uses) and ``applied_ratios`` is aligned with ``core_frames``
    order for result provenance. ``mode="global"`` applies that median to
    every frame; ``mode="per_frame"`` applies each frame's own measured
    ratio clamped to ``NOISE_RATIO_CLAMP`` around the median, falling back
    to the median for frames without a measurable ratio.
    """
    per_frame = [_frame_noise_ratio(fr) for fr in core_frames]
    measured = [r for r in per_frame if r is not None]
    if not measured:
        return 1.0, core_frames, [1.0] * len(core_frames)
    median_ratio = float(np.median(measured))
    if not np.isfinite(median_ratio) or median_ratio <= 0:
        return 1.0, core_frames, [1.0] * len(core_frames)
    noise_calibration = median_ratio
    if mode == "per_frame":
        applied = [
            _clamp_noise_ratio(r, median_ratio) if r is not None else median_ratio
            for r in per_frame
        ]
    else:
        applied = [noise_calibration] * len(core_frames)
    if mode == "global" and abs(noise_calibration - 1.0) <= 1e-6:
        return noise_calibration, core_frames, applied
    out_frames = [
        Frame(
            image=np.asarray(fr.image, dtype=np.float64),
            variance=np.asarray(fr.variance, dtype=np.float64) * a,
            time=float(fr.time),
            frame_id=fr.frame_id,
        )
        for fr, a in zip(core_frames, applied, strict=True)
    ]
    return noise_calibration, out_frames, applied


def _validate_interpolation_order(value: Any) -> int:
    """Job-param validator for the shift kernel experiment (D-24 register)."""
    if value is None:
        return 1
    if isinstance(value, bool):
        raise APIError("ORB-API-001", "interpolation_order must be 1 or 3")
    try:
        order = int(value)
    except (TypeError, ValueError) as exc:
        raise APIError("ORB-API-001", "interpolation_order must be 1 or 3") from exc
    if order not in (1, 3):
        raise APIError("ORB-API-001", "interpolation_order must be 1 or 3")
    return order


def run_science_search(
    decoded_frames: list[DecodedFrame],
    *,
    motion_vectors: list[MotionVector],
    min_coverage_fraction: float,
    detection_threshold_sigma: float | None,
    nms_radius_px: int,
    background_variant: str,
    work_budget: int,
    interpolation_order: int = 1,
    noise_model: str = "per_frame",
    warn: Any = None,
) -> ScienceSearchResult:
    """Shared science path: preprocessing -> shift-and-stack -> extraction.

    This is the ``_science_path`` the RS.09/D-24 follow-up called for: the
    moving-object search handler and the VAL.04 validation job both run
    injected or stored ``DecodedFrame`` inputs through exactly this code, so
    the two cannot silently diverge. ``warn`` is an optional ``(code,
    message)`` callback (``JobContext.warn`` in handlers).
    """
    if not decoded_frames:
        raise APIError("ORB-RS-001", "no decodable frames with variance available")

    # RS.09/D-24: background/static variant (PROVISIONAL(G1), default F).
    requested_variant = _validate_background_variant(background_variant)
    interpolation_order = _validate_interpolation_order(interpolation_order)
    variant = requested_variant
    if variant in ("e", "f") and len(decoded_frames) < STATIC_MASK_MIN_FRAMES:
        # Difference imaging needs >=5 epochs: with fewer, the low-quantile
        # coadd is dominated by mover flux and 1-2 valid epochs produce
        # meaningless (often exactly zero) differences (RS.09 review).
        if warn is not None:
            warn(
                "ORB-RS-PROVISIONAL",
                f"background_variant {variant} requires >= {STATIC_MASK_MIN_FRAMES} "
                f"frames; got {len(decoded_frames)} — fell back to c",
            )
        variant = "c"
    if variant in ("e", "f") and warn is not None:
        # D-24 RS.08 amendment: difference imaging self-subtracts movers that
        # stay on the same pixels across near-simultaneous exposures (a visit's
        # frames are wavelength sweeps, not independent epochs). frame_window
        # .min_time_separation_days deduplicates visits, but it only runs in
        # windowed mode — warn when the surviving frames still contain
        # sub-separation gaps (unwindowed search or frame_refs).
        mjds = sorted(float(fr.mjd) for fr in decoded_frames)
        min_gap = min((b - a for a, b in zip(mjds, mjds[1:], strict=False)), default=None)
        if min_gap is not None and min_gap < DEFAULT_FRAME_MIN_TIME_SEPARATION_DAYS:
            warn(
                "ORB-RS-PROVISIONAL",
                f"background_variant {variant} on frames {min_gap:.4f} d apart: "
                "near-simultaneous exposures can self-subtract slow movers "
                "(D-24 ghost trail); consider frame_window."
                "min_time_separation_days to deduplicate visits",
            )
    core_frames = _background_frames(variant, decoded_frames)
    # Noise calibration (RS.08): the stored variance map under-models the
    # real noise floor — on the live tutorial field it captured ~1/14 of
    # the effective noise, inflating stack_snr. Measure the noise the
    # search actually sees: per-frame MAD of the PROCESSED image vs the
    # median stored variance. ``noise_model="global"`` (default) takes the
    # median variance-ratio across frames and rescales every frame by it;
    # ``noise_model="per_frame"`` (D-24 DSP register, experiment 2) applies
    # each frame's own ratio — frames differ in background/exptime and,
    # under E/F, in how much coadd-subtraction noise the difference images
    # carry, none of which a single scalar can represent. Frames without
    # enough valid pixels fall back to the median ratio; per_frame ratios
    # are clamped to NOISE_RATIO_CLAMP around the median so a collapsed
    # ratio cannot let one frame dominate the stack. The median of the
    # applied ratios is reported as noise_calibration either way — it is
    # what validation's flux_for_snr consumes via
    # ScienceSearchResult.noise_calibration.
    noise_model = _validate_noise_model(noise_model)
    noise_calibration, calibrated_frames, applied_ratios = _calibrate_frame_variances(
        core_frames, mode=noise_model
    )
    core_frames = calibrated_frames
    # Static-source evidence always comes from the C-style (15 px high-pass)
    # frames so the flag stays an independent safety net even when the search
    # itself runs difference imaging (E/F) — otherwise a residual star leak
    # would be invisible to the flag (RS.09 review).
    evidence_frames = (
        core_frames if variant in ("c", "d") else _background_frames("c", decoded_frames)
    )
    detection_masks, _frame_detection_stats = _frame_detection_masks(
        evidence_frames, sigma_threshold=STATIC_SOURCE_INDIVIDUAL_SIGMA
    )
    if variant == "d":
        core_frames = _mask_static_sources(core_frames)
    zero_trial_index = _find_zero_motion_trial_index(motion_vectors)

    min_coverage_count = math.ceil(float(min_coverage_fraction) * len(core_frames))
    result = search(
        core_frames,
        motion_vectors,
        min_coverage_fraction=min_coverage_fraction,
        work_budget=int(work_budget),
        interpolation_order=interpolation_order,
        return_trial_maps=False,
    )

    extracted, extraction_method, extraction_diagnostic = _extract_candidates(
        result,
        detection_threshold_sigma=detection_threshold_sigma,
        nms_radius_px=nms_radius_px,
        edge_margin_px=EXTRACTION_EDGE_MARGIN_PX,
        min_coverage_count=min_coverage_count,
    )
    if warn is not None:
        warn(
            "ORB-RS-PROVISIONAL",
            "candidate extraction/static-source handling are PROVISIONAL(G1); "
            f"D-24 variant {variant} preprocessing applied; "
            "static-source heuristic recorded",
        )

    # Compute static-source evidence for every extracted candidate, then rank
    # unflagged candidates above flagged ones (ties by stack_snr) so leak
    # candidates cannot fill the persist cap (RS.09 review).
    baseline_days = (
        max(float(f.time) for f in core_frames) - min(float(f.time) for f in core_frames)
        if core_frames
        else 0.0
    )
    for cand in extracted:
        t_ev = int(cand["trial_index"])
        motion_ev = result.motion_vectors[t_ev]
        disp_ev = math.hypot(motion_ev.dx_px_per_day, motion_ev.dy_px_per_day) * baseline_days
        cand["_static_ev"] = _static_source_evidence(
            x=int(cand["x"]),
            y=int(cand["y"]),
            zero_trial_index=zero_trial_index,
            best_trial_index=t_ev,
            detection_masks=detection_masks,
            winning_displacement_px=disp_ev,
            baseline_days=baseline_days,
        )
    extracted = sorted(
        extracted,
        key=lambda c: (
            bool(c["_static_ev"]["flagged_static_source"]),
            -float(c["stack_snr"]),
        ),
    )

    return ScienceSearchResult(
        result=result,
        candidates=extracted,
        candidates_found=len(extracted),
        variant_used=variant,
        variant_requested=requested_variant,
        extraction_method=extraction_method,
        extraction_diagnostic=extraction_diagnostic,
        zero_trial_index=zero_trial_index,
        core_frames=core_frames,
        noise_calibration=noise_calibration,
        interpolation_order=interpolation_order,
        noise_model=noise_model,
        frame_noise_ratios=applied_ratios,
    )


_STACK_PNG_HALF_PX = 15  # 31x31 px cutout of the winning combined image (§8.2)


def _stack_cutout_png(combined: np.ndarray, cx: float, cy: float) -> bytes:
    """Render a (2*half+1)^2 px cutout of the combined image as an LA PNG.

    Same masked-pixel convention as ``api.candidates._cutout_png``: pixels
    outside the frame are NaN (alpha 0), stretch is fit deterministically on
    the cutout. ``cx``/``cy`` are 0-based array (x, y) coordinates.
    """
    out = cutout_array(combined, cx, cy, _STACK_PNG_HALF_PX)
    params: StretchParams = fit_stretch([out])
    stretched = apply_stretch(out, params)
    valid = np.isfinite(out)
    gray = np.zeros(out.shape, np.uint8)
    gray[valid] = (np.clip(stretched[valid], 0.0, 1.0) * 255).astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([gray, alpha]), mode="LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_candidate_stack_pngs(
    core_frames: list[Frame],
    motions: list[MotionVector],
    positions: list[tuple[float, float]],
    reference_time: float,
    interpolation_order: int = 1,
) -> list[bytes | None]:
    """Re-combine each candidate's winning trial and render its stack PNG.

    Uses ``shift_and_combine`` on the same preprocessed frames and motion
    vector that produced the detection — identical shift/combine semantics
    to the search — so the image is the actual stack the score came from.
    A candidate whose combine or stretch fails gets ``None`` (the endpoint
    then reports the image unavailable) rather than failing the whole job.
    """
    pngs: list[bytes | None] = []
    for motion, (cx, cy) in zip(motions, positions, strict=True):
        try:
            combined = shift_and_combine(
                core_frames,
                motion,
                reference_time=reference_time,
                work_budget=None,
                interpolation_order=interpolation_order,
            )
            pngs.append(_stack_cutout_png(np.asarray(combined.combined), cx, cy))
        except Exception:
            log.warning(
                "stack PNG render failed for motion (%s, %s) px/day",
                motion.dx_px_per_day,
                motion.dy_px_per_day,
                exc_info=True,
            )
            pngs.append(None)
    return pngs


def _resolve_stack_policy_path(settings: Any | None) -> Path:
    stack_policy = getattr(settings, "stack_policy", None) if settings is not None else None
    policy_path = Path(str(stack_policy)) if stack_policy else Path("config/stack_policy.yaml")
    if not policy_path.is_absolute():
        cfg_dir = getattr(settings, "config_dir", None) if settings is not None else None
        if cfg_dir:
            policy_path = Path(cfg_dir) / policy_path.name
        else:
            # src/orbitalis/research/search.py -> repo root is parents[3]
            policy_path = Path(__file__).resolve().parents[3] / "config" / policy_path.name
    return policy_path


async def moving_object_search(ctx: JobContext) -> dict[str, Any]:
    """Job handler for moving_object_search (SDD §6.9, G1 synthetic-data flow)."""
    db = ctx.db
    settings = ctx.settings
    params = dict(ctx.params or {})
    field_id = params.get("field_id") or await _get_job_field_id(ctx)
    if not field_id:
        raise APIError("ORB-API-001", "moving_object_search requires field_id")
    params["field_id"] = field_id

    field = await get_field(db, field_id)
    if field is None:
        raise APIError("ORB-API-002", "field not found")
    if field.get("status") != "completed":
        raise APIError("ORB-RS-001", "field must be completed before analysis")

    await ctx.progress(0, 1, "load")
    ctx.check_cancelled()

    rows = await list_frames(db, field_id)
    rows, frame_window = select_frame_rows(rows, params)
    frame_refs = params.get("frame_refs")
    if frame_refs is not None:
        if not isinstance(frame_refs, list) or not all(
            isinstance(r, str) and r for r in frame_refs
        ):
            raise APIError("ORB-API-001", "frame_refs must be a list of non-empty strings")
        requested = list(frame_refs)
        by_ref = {r.get("frame_ref"): r for r in rows}
        missing = [r for r in requested if r not in by_ref]
        if missing:
            raise APIError(
                "ORB-RS-001",
                "requested frame_refs are not available for this field",
                detail={"missing_frame_refs": missing},
            )
        rows = [by_ref[r] for r in requested]
    if not rows:
        raise APIError("ORB-RS-001", "field has no frames available for search")

    # RS.04 (D-14, PROVISIONAL(G1)): policy judges combinability of supplied set.
    policy_path = _resolve_stack_policy_path(settings)
    policy = load_stack_policy(policy_path)
    enforce_combinable(rows, policy)

    # Decode and validate stored frames (actual stored blobs/metadata path).
    decoded_frames, frame_meta = _decode_stored_frames(rows)

    shapes = {f.image.shape for f in decoded_frames}
    if len(shapes) != 1:
        raise APIError("ORB-RS-001", "stored frames do not share a common aligned shape")

    frame_shape = decoded_frames[0].image.shape
    field_wcs = _resolve_field_wcs(field, rows, frame_shape)
    h, w = frame_shape
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0

    motion_vectors = _parse_motion_vectors(params, field_wcs, cx, cy, policy)
    min_cov = _validate_min_coverage_fraction(params.get("min_coverage_fraction", 0.5))
    det_sigma = params.get("detection_threshold_sigma", params.get("detection_threshold"))
    if det_sigma is not None:
        det_sigma = _coerce_finite_float(det_sigma, name="detection_threshold_sigma")
        if det_sigma <= 0:
            raise APIError("ORB-API-001", "detection_threshold_sigma must be > 0")
    nms_radius = int(params.get("nms_radius_px", EXTRACTION_NMS_RADIUS_PX))
    if nms_radius < 0:
        raise APIError("ORB-API-001", "nms_radius_px must be an integer >= 0")

    work_estimate = len(motion_vectors) * len(decoded_frames) * h * w
    cfg_budget = _resolve_search_max_work(settings)
    if work_estimate > cfg_budget:
        raise APIError(
            "ORB-RS-003",
            "search exceeds work budget",
            detail={"work_estimate": work_estimate, "budget": cfg_budget},
        )

    await ctx.progress(0, len(motion_vectors), "search")
    ctx.check_cancelled()

    # Shared science path (VAL.04 prerequisite): preprocessing -> search ->
    # extraction is identical for stored and injected frames.
    sci = await asyncio.to_thread(
        run_science_search,
        decoded_frames,
        motion_vectors=motion_vectors,
        min_coverage_fraction=min_cov,
        detection_threshold_sigma=det_sigma,
        nms_radius_px=nms_radius,
        background_variant=params.get("background_variant"),
        work_budget=cfg_budget,
        interpolation_order=_validate_interpolation_order(params.get("interpolation_order")),
        noise_model=_validate_noise_model(params.get("noise_model")),
        warn=ctx.warn,
    )
    result = sci.result
    variant = sci.variant_used
    requested_variant = sci.variant_requested
    extraction_method = sci.extraction_method
    extraction_diagnostic = sci.extraction_diagnostic
    zero_trial_index = sci.zero_trial_index

    await ctx.progress(len(motion_vectors), len(motion_vectors), "extract")
    ctx.check_cancelled()

    # Persist at most the best EXTRACTION_MAX_PERSISTED_CANDIDATES; the true
    # extracted count is reported via candidates_found (RS.09 contract).
    extracted = sci.candidates[:EXTRACTION_MAX_PERSISTED_CANDIDATES]
    candidates_extracted = sci.candidates_found

    # Render each candidate's winning-trial stack cutout from the same
    # preprocessed frames/motion the search used (§8.2 stack.png). Done once,
    # off the event loop; a failed render stores None -> endpoint 404s.
    stack_pngs = await asyncio.to_thread(
        _render_candidate_stack_pngs,
        sci.core_frames,
        [result.motion_vectors[int(c["trial_index"])] for c in extracted],
        [
            (float(c.get("x_centroid", c["x"])), float(c.get("y_centroid", c["y"])))
            for c in extracted
        ],
        float(result.reference_time),
        sci.interpolation_order,
    )
    stack_pngs_failed = sum(1 for p in stack_pngs if p is None)

    stored_ids: list[str] = []
    static_flagged = 0
    for cand_idx, cand in enumerate(extracted):
        ctx.check_cancelled()
        x_peak, y_peak = int(cand["x"]), int(cand["y"])
        x_pos = float(cand.get("x_centroid", x_peak))
        y_pos = float(cand.get("y_centroid", y_peak))
        t = int(cand["trial_index"])
        motion = result.motion_vectors[t]
        sky = field_wcs.pixel_to_world(x_pos, y_pos)
        ra = float(sky.ra.deg)
        dec = float(sky.dec.deg)
        mu_e, mu_n = _pixel_per_day_to_mu_east_north(
            motion.dx_px_per_day, motion.dy_px_per_day, field_wcs, x_pos, y_pos
        )
        static_ev = cand["_static_ev"]
        static_flagged += int(bool(static_ev["flagged_static_source"]))

        key = json.dumps(
            {"job_id": ctx.job_id, "field_id": field_id, "trial": t, "x": x_peak, "y": y_peak},
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_id = "c_" + hashlib.sha256(key.encode()).hexdigest()[:24]
        await put_candidate(
            db,
            candidate_id,
            ctx.job_id,
            field_id,
            ra=ra,
            dec=dec,
            motion={
                # D-17 persisted contract == api SkyMotionView: canonical
                # sky-plane components only (rate/PA are derivable).
                "mu_e_arcsec_per_day": float(mu_e),
                "mu_n_arcsec_per_day": float(mu_n),
            },
            stack_snr=float(cand["stack_snr"]),
            stack_png_blob=stack_pngs[cand_idx],
            frames=[
                {
                    **fm,
                    "predicted_x": x_pos
                    + motion.dx_px_per_day * (float(fm["mjd"]) - float(result.reference_time))
                    if fm.get("mjd") is not None
                    else None,
                    "predicted_y": y_pos
                    + motion.dy_px_per_day * (float(fm["mjd"]) - float(result.reference_time))
                    if fm.get("mjd") is not None
                    else None,
                    "static_source": static_ev,
                    "extraction": {
                        "background_variant": variant,
                        "method": extraction_method,
                        "diagnostic": extraction_diagnostic,
                        "threshold_sigma": cand.get("threshold_sigma"),
                        "score_threshold": cand.get("score_threshold"),
                        "score_sigma_estimate": cand.get("score_sigma_estimate"),
                        "nms_radius_px": nms_radius,
                        "edge_margin_px": EXTRACTION_EDGE_MARGIN_PX,
                        "provisional": True,
                    },
                    "peak_x": x_peak,
                    "peak_y": y_peak,
                    "centroid_x": x_pos,
                    "centroid_y": y_pos,
                }
                for fm in frame_meta
            ],
            model_score=None,
            model_version=None,
            predicted_category=None,
            static_source=bool(static_ev["flagged_static_source"]),
            reference_mjd=float(result.reference_time),
        )
        stored_ids.append(candidate_id)

    await ctx.progress(len(motion_vectors), len(motion_vectors), "done")

    result_dict = {
        "field_id": field_id,
        "job_id": ctx.job_id,
        "frames_used": len(decoded_frames),
        "motion_trials": len(motion_vectors),
        "work_estimate": int(result.work_estimate),
        "candidates_found": candidates_extracted,
        "candidates_persisted": len(stored_ids),
        "candidates_truncated": candidates_extracted > len(stored_ids),
        "candidate_ids": stored_ids,
        "reference_time_mjd": float(result.reference_time),
        "status": "unconfirmed_candidates",
        "model_ran": False,
        "extraction_method": extraction_method,
        "extraction_diagnostic": extraction_diagnostic,
        "background_variant": variant,
        "background_variant_requested": requested_variant,
        "static_source_handling": f"d24_variant_{_VARIANT_LABELS[variant]}_provisional_g1",
        "static_sources_flagged": static_flagged,
        "zero_motion_trial_index": zero_trial_index,
        "stack_pngs_failed": stack_pngs_failed,
        "noise_calibration": float(sci.noise_calibration),
        "interpolation_order": sci.interpolation_order,
        "noise_model": sci.noise_model,
        # Per-frame variance ratios actually applied, aligned with the
        # search's frame order (same order as frames_used / frame_meta).
        "noise_model_frame_ratios": [round(float(r), 6) for r in sci.frame_noise_ratios],
    }
    if frame_window is not None:
        result_dict["frame_window"] = frame_window
    return result_dict


__all__ = [
    "EXTRACTION_DEFAULT_SIGMA_THRESHOLD",
    "EXTRACTION_EDGE_MARGIN_PX",
    "EXTRACTION_NMS_RADIUS_PX",
    "HIGHPASS_MEDIAN_SIZE_PX",
    "STATIC_SOURCE_INDIVIDUAL_SIGMA",
    "STATIC_SOURCE_MIN_FRAME_FRACTION",
    "ScienceSearchResult",
    "moving_object_search",
    "select_frame_rows",
    "run_science_search",
]
