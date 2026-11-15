"""Quick-look light-curve measurement shared by field and candidate endpoints.

Purpose: Decode stored ``array_blob`` frames, run
``orbitalis_core.photometry.measure_source`` at each requested position,
and shape the per-frame points. Presentation-side orchestration only — the
measurement itself is pure core.
Inputs: frame rows from ``store``, a common-grid WCS and pixel solid angle.
Outputs: point dicts matching ``PhotometryPointView``.
Called by: ``api.fields`` (``/fields/{id}/lightcurve``) and
``api.candidates`` (``/candidates/{id}/lightcurve``).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from astropy.wcs import WCS

from orbitalis.store import decode_frame_array
from orbitalis_core.calibration import solid_angle_map
from orbitalis_core.flags import ALLOWED_BITS, FLAG_BITS
from orbitalis_core.photometry import measure_source

logger = logging.getLogger(__name__)

_BIT_NAMES = sorted(FLAG_BITS.items(), key=lambda kv: kv[1])
# SOURCE and FULLSAMPLE are kept by the mask policy, so they never cause
# masking — reporting them would mislead (every masked bright pixel also
# carries SOURCE).
_MASKING_BITS = [(name, bit) for name, bit in _BIT_NAMES if bit & ~ALLOWED_BITS]

# Measurement parameters, surfaced verbatim in the response ``method`` so
# clients can label the plot honestly. Aperture/annulus radii are in common
# -grid pixels (~6.15 arcsec each).
CENTROID_RADIUS_PX = 3.0
APERTURE_RADIUS_PX = 2.5
ANNULUS_INNER_PX = 4.0
ANNULUS_OUTER_PX = 7.0
MIN_SNR = 3.0
UPPER_LIMIT_SIGMA = 3.0
MIN_APERTURE_COVERAGE = 0.8

METHOD_NOTE = (
    "quick-look aperture photometry on resampled MJy/sr frames; "
    "no aperture correction, not a calibrated light curve; "
    "frames span multiple bandpasses so brightness mixes wavelengths - "
    "group or colour points by wavelength_um"
)


def wcs_from_grid_spec(spec: dict[str, Any] | None) -> WCS | None:
    """Rebuild the common-grid WCS from the field's stored grid spec.

    Same axis-aligned TAN convention as ``pipeline.runner._target_grid``:
    no SIP or rotation terms (the grid is defined without them).
    """
    if not spec:
        return None
    scale_deg = float(spec["pixel_scale_arcsec"]) / 3600.0
    h, w = (int(v) for v in spec["shape"])
    ww = WCS(naxis=2)
    ww.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    ww.wcs.crpix = [w / 2 + 0.5, h / 2 + 0.5]
    ww.wcs.crval = [float(spec["ra"]), float(spec["dec"])]
    ww.wcs.cdelt = [-scale_deg, scale_deg]
    return ww


def pixel_solid_angle_sr(wcs: WCS, shape: tuple[int, int]) -> float:
    """Median per-pixel solid angle in steradians over the grid.

    The grid is a near-conformal TAN projection so the median is a faithful
    single conversion factor for aperture sums; per-pixel values come from
    ``solid_angle_map`` (exact spherical quads).
    """
    sr_map = solid_angle_map(wcs, shape)
    finite = sr_map[np.isfinite(sr_map)]
    if finite.size == 0:
        raise ValueError("solid_angle_map returned no finite pixels")
    return float(np.median(finite))


def method_dict(kind: str) -> dict[str, Any]:
    """Honest ``method`` block shared by both light-curve endpoints."""
    return {
        "kind": kind,
        "centroid_radius_px": CENTROID_RADIUS_PX,
        "aperture_radius_px": APERTURE_RADIUS_PX,
        "annulus_px": [ANNULUS_INNER_PX, ANNULUS_OUTER_PX],
        "min_snr": MIN_SNR,
        "min_aperture_coverage": MIN_APERTURE_COVERAGE,
        "upper_limit_sigma": UPPER_LIMIT_SIGMA,
        "note": METHOD_NOTE,
        "calibrated": False,
    }


def measure_points(
    items: list[dict[str, Any]],
    wcs: WCS,
    sr_per_pixel: float,
) -> tuple[list[dict[str, Any]], int]:
    """Measure each ``{frame, x, y, ...extra}`` item on its stored array.

    ``frame`` is a ``store`` frame row; ``x``/``y`` the prior position in
    common-grid pixels. Extra keys (``predicted_x``, ``predicted_y``,
    ``mjd``) pass through to the point. Returns ``(points, skipped)`` —
    frames without a decodable ``array_blob`` are skipped and counted.
    """
    points: list[dict[str, Any]] = []
    skipped = 0
    for item in items:
        frame = item["frame"]
        blob = frame.get("array_blob")
        if not blob:
            skipped += 1
            continue
        try:
            decoded = decode_frame_array(bytes(blob))
        except Exception:
            skipped += 1
            continue
        image = np.asarray(decoded.data, dtype=np.float64)
        mask = np.asarray(decoded.mask, dtype=bool)
        if mask.shape == image.shape:
            image = np.where(mask, image, np.nan)
        prior_x = float(item["x"])
        prior_y = float(item["y"])
        masked_bits = _masked_bits(decoded.flags, image, prior_x, prior_y)
        try:
            m = measure_source(
                image,
                prior_x,
                prior_y,
                centroid_radius_px=CENTROID_RADIUS_PX,
                aperture_radius_px=APERTURE_RADIUS_PX,
                annulus_inner_px=ANNULUS_INNER_PX,
                annulus_outer_px=ANNULUS_OUTER_PX,
                min_snr=MIN_SNR,
                min_coverage=MIN_APERTURE_COVERAGE,
            )
        except Exception as exc:
            # prior off-grid or degenerate window: honest non-detection
            logger.debug("lightcurve measurement failed for %s: %s", frame["frame_ref"], exc)
            m = None
        point: dict[str, Any] = {
            "frame_ref": frame["frame_ref"],
            "mjd": item.get("mjd", frame.get("mjd")),
            "wavelength_um": frame.get("wavelength_um"),
            "detector": frame.get("detector"),
            "x": None,
            "y": None,
            "ra": None,
            "dec": None,
            "flux_mjy": None,
            "flux_err_mjy": None,
            "upper_limit_mjy": None,
            "snr": None,
            "detected": False,
            "aperture_coverage": None,
            "masked_bits": masked_bits,
            "background_mjy_sr": None,
        }
        for key in ("predicted_x", "predicted_y"):
            if key in item:
                point[key] = item[key]
        if m is not None:
            point["snr"] = m.snr
            point["aperture_coverage"] = m.aperture_coverage
            if m.aperture_coverage >= 1.0:
                point["masked_bits"] = None
            point["background_mjy_sr"] = m.background_mjy_sr
            if m.net_sum_mjy_sr is not None and m.aperture_coverage >= MIN_APERTURE_COVERAGE:
                # Flux and its error are reported even without a detection:
                # a non-detection is an upper limit, not a gap (e.g. a Mira
                # at minimum still constrains the curve). Below the coverage
                # floor the sum silently drops the masked — usually brightest —
                # pixels, so flux/UL are withheld rather than biased low.
                point["flux_mjy"] = m.net_sum_mjy_sr * sr_per_pixel
                if m.flux_err_mjy_sr is not None:
                    err_mjy = m.flux_err_mjy_sr * sr_per_pixel
                    point["flux_err_mjy"] = err_mjy
                    point["upper_limit_mjy"] = UPPER_LIMIT_SIGMA * err_mjy
            if m.detected:
                # Only a detection earns an observed position: below the
                # threshold the centroid is a noise peak, not a measurement.
                point["detected"] = True
                point["x"] = m.x
                point["y"] = m.y
                try:
                    ra, dec = wcs.all_pix2world(m.x, m.y, 0)
                    if np.isfinite(ra) and np.isfinite(dec):
                        point["ra"] = float(ra)
                        point["dec"] = float(dec)
                except Exception:
                    pass
        points.append(point)
    points.sort(key=lambda p: (p["mjd"] is None, p["mjd"] or 0.0))
    return points, skipped


def spectrum_bins(
    points: list[dict[str, Any]], *, min_bins: int = 3
) -> tuple[str, str | None, list[dict[str, Any]] | None]:
    """Bin per-frame photometry points by ``wavelength_um`` into a spectrum.

    Each wavelength bin aggregates the frames that share it. In practice
    ``wavelength_um`` is the native-cutout centre wavelength and rarely
    repeats exactly, so most bins hold a single frame — the "median" is
    then that frame's value. ``flux_mjy`` is the median of per-frame
    aperture sums — a quick-look spectral energy distribution, not a
    calibrated spectrum (see ``method.calibrated``). Bins whose frames all
    lack valid flux are kept with ``flux_mjy: null`` and their
    ``masked_bits`` so masked bands are visible, not silently dropped.
    ``ok`` requires at least ``min_bins`` bins with a detection — bare
    noise points never earn an ``ok`` spectrum (§8.5).
    """
    groups: dict[float, list[dict[str, Any]]] = {}
    for p in points:
        lam = p.get("wavelength_um")
        if lam is None:
            continue
        groups.setdefault(round(float(lam), 4), []).append(p)
    bins: list[dict[str, Any]] = []
    for lam in sorted(groups):
        members = groups[lam]
        fluxes = [m["flux_mjy"] for m in members if m.get("flux_mjy") is not None]
        errs = [m["flux_err_mjy"] for m in members if m.get("flux_err_mjy") is not None]
        uls = [m["upper_limit_mjy"] for m in members if m.get("upper_limit_mjy") is not None]
        n_det = sum(1 for m in members if m.get("detected"))
        masked = sorted({b for m in members for b in (m.get("masked_bits") or [])})
        bins.append(
            {
                "wavelength_um": lam,
                "flux_mjy": float(np.median(fluxes)) if fluxes else None,
                "flux_err_mjy": float(np.median(errs)) if errs else None,
                "upper_limit_mjy": float(np.median(uls)) if uls else None,
                "n_frames": len(members),
                "n_detected": n_det,
                "detected": n_det > 0,
                "masked_bits": masked or None,
            }
        )
    n_ok = sum(1 for b in bins if b["detected"])
    if n_ok < min_bins:
        return (
            "insufficient_data",
            f"no significant detections: {n_ok} wavelength bins detected (need {min_bins})",
            None,
        )
    return "ok", None, bins


def _masked_bits(
    flags: np.ndarray | None,
    image: np.ndarray,
    x: float,
    y: float,
) -> list[str] | None:
    """Flag-bit names on masked pixels inside the source aperture.

    Lets the UI explain a low ``aperture_coverage`` (e.g. "aperture masked:
    BLOOM") instead of showing a silent gap. Returns ``None`` when there is
    nothing masked or no flags plane.
    """
    if flags is None:
        return None
    fl = np.asarray(flags)
    if fl.shape != image.shape:
        return None
    h, w = image.shape
    xi, yi = int(round(x)), int(round(y))
    r = int(np.ceil(APERTURE_RADIUS_PX)) + 1
    y0, y1 = max(0, yi - r), min(h, yi + r + 1)
    x0, x1 = max(0, xi - r), min(w, xi + r + 1)
    if y1 <= y0 or x1 <= x0:
        return None
    sub_img = image[y0:y1, x0:x1]
    sub_fl = fl[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    disk = (xx - x) ** 2 + (yy - y) ** 2 <= APERTURE_RADIUS_PX**2
    masked = disk & ~np.isfinite(sub_img)
    if not masked.any():
        return None
    counts: dict[str, int] = {}
    for name, bit in _MASKING_BITS:
        n = int(((sub_fl[masked].astype(np.int64) & bit) != 0).sum())
        if n:
            counts[name] = n
    if not counts:
        return None
    return [name for name, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:6]]


__all__ = [
    "ANNULUS_INNER_PX",
    "ANNULUS_OUTER_PX",
    "APERTURE_RADIUS_PX",
    "CENTROID_RADIUS_PX",
    "MIN_SNR",
    "METHOD_NOTE",
    "measure_points",
    "method_dict",
    "pixel_solid_angle_sr",
    "wcs_from_grid_spec",
]
