"""Quick-look aperture photometry and centroiding on stored frames.

Purpose: Per-frame position and flux measurement for the light-curve view —
a fixed sky position (named targets, clicked sky) or a candidate's predicted
track position. Imaging stays in MJy/sr; the caller converts the returned
aperture sum to flux density with the pixel solid angle.
Inputs: A 2-D float array in MJy/sr (NaN = masked) and a prior position in
0-based pixel coordinates ``(x=col, y=row)``.
Outputs: A :class:`SourceMeasurement` — refined centroid, aperture sum,
background, noise and an honest ``detected`` flag. A non-detection is a
result, not an error; degenerate inputs raise ``InvalidInputError``.
Called by: ``orbitalis.api`` light-curve endpoints and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from orbitalis_core.errors import InvalidInputError


@dataclass(frozen=True)
class SourceMeasurement:
    """One frame's measurement at a prior position.

    ``x``/``y`` are the refined centroid in 0-based pixel coordinates
    (``x`` = column, ``y`` = row), ``offset_px`` its distance from the
    prior. ``net_sum_mjy_sr`` is the background-subtracted aperture sum in
    MJy/sr (multiply by the pixel solid angle for MJy); ``flux_err`` is the
    annulus-noise estimate on that sum. ``detected`` requires SNR, aperture
    coverage and a centroid that stayed inside its search radius — it is a
    quick-look flag, not a detection claim.
    """

    x: float
    y: float
    offset_px: float
    detected: bool
    snr: float | None
    net_sum_mjy_sr: float | None
    flux_err_mjy_sr: float | None
    background_mjy_sr: float | None
    aperture_coverage: float
    n_aperture: int
    n_annulus: int


def _window(
    image: np.ndarray, x: float, y: float, half: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    """Clip a ``(2*half+1)^2`` window around ``(x, y)`` to the image.

    Returns ``(cutout, yy, xx, x0, y0)`` where ``yy``/``xx`` are absolute
    pixel coordinates for every cutout element.
    """
    h, w = image.shape
    x0 = int(np.clip(round(x) - half, 0, w - 1))
    y0 = int(np.clip(round(y) - half, 0, h - 1))
    x1 = int(np.clip(round(x) + half + 1, 0, w))
    y1 = int(np.clip(round(y) + half + 1, 0, h))
    cut = image[y0:y1, x0:x1]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    return cut, yy.astype(np.float64), xx.astype(np.float64), x0, y0


def measure_source(
    image: Any,
    x: float,
    y: float,
    *,
    centroid_radius_px: float = 3.0,
    centroid_iters: int = 2,
    aperture_radius_px: float = 2.5,
    annulus_inner_px: float = 4.0,
    annulus_outer_px: float = 7.0,
    min_snr: float = 3.0,
    min_aperture_pixels: int = 3,
    min_coverage: float = 0.8,
    noise_samples: int = 25,
) -> SourceMeasurement:
    """Centroid-refine and aperture-measure a source near ``(x, y)``.

    Background is the annulus median around the *prior* position; the
    centroid is the positive-residual intensity-weighted mean iterated
    ``centroid_iters`` times; the flux is the background-subtracted sum over
    ``aperture_radius_px`` around the refined centroid.

    Noise is estimated empirically: the same aperture is placed at
    ``noise_samples`` deterministic pseudo-random positions in the field
    (excluding the source neighbourhood) and the robust MAD spread of
    those sums is the flux error. Unlike ``annulus_std * sqrt(n)`` this
    captures the noise correlation introduced by bilinear resampling —
    which makes the naive estimator overstate SNR. ``detected`` requires
    SNR, aperture coverage (``min_coverage`` of the expected aperture
    pixels must be finite) and a centroid that stayed inside its search
    radius — it is a quick-look flag, not a detection claim.
    """
    arr = np.asarray(image, dtype=np.float64)
    if arr.ndim != 2 or arr.dtype.kind not in "fiu":
        raise InvalidInputError("image must be a 2-D numeric MJy/sr array")
    if not (np.isfinite(x) and np.isfinite(y)):
        raise InvalidInputError("prior position must be finite pixel coordinates")
    h, w = arr.shape
    if not (0.0 <= x < w and 0.0 <= y < h):
        raise InvalidInputError(f"prior position ({x}, {y}) is outside the {w}x{h} image")

    half = int(np.ceil(annulus_outer_px)) + 2
    cut, yy, xx, x0, y0 = _window(arr, x, y, half)
    r_prior = np.hypot(xx - x, yy - y)

    annulus = (r_prior >= annulus_inner_px) & (r_prior < annulus_outer_px) & np.isfinite(cut)
    n_annulus = int(annulus.sum())
    if n_annulus < min_aperture_pixels:
        return SourceMeasurement(
            x=float(x),
            y=float(y),
            offset_px=0.0,
            detected=False,
            snr=None,
            net_sum_mjy_sr=None,
            flux_err_mjy_sr=None,
            background_mjy_sr=None,
            aperture_coverage=0.0,
            n_aperture=0,
            n_annulus=n_annulus,
        )
    bg = float(np.median(cut[annulus]))
    resid = np.where(np.isfinite(cut), cut - bg, np.nan)

    # Centroid refinement on positive residuals only.
    cx, cy = float(x), float(y)
    for _ in range(max(1, centroid_iters)):
        rc = np.hypot(xx - cx, yy - cy)
        near = (rc <= centroid_radius_px) & np.isfinite(resid) & (resid > 0)
        weight = np.where(near, resid, 0.0)
        total = float(weight.sum())
        if total <= 0:
            break
        cx = float((weight * xx).sum() / total)
        cy = float((weight * yy).sum() / total)

    offset = float(np.hypot(cx - x, cy - y))
    rc = np.hypot(xx - cx, yy - cy)
    ap_all = rc <= aperture_radius_px
    n_ap_expected = int(ap_all.sum())
    ap = ap_all & np.isfinite(resid)
    n_ap = int(ap.sum())
    coverage = n_ap / n_ap_expected if n_ap_expected else 0.0
    if n_ap < min_aperture_pixels:
        return SourceMeasurement(
            x=cx,
            y=cy,
            offset_px=offset,
            detected=False,
            snr=None,
            net_sum_mjy_sr=None,
            flux_err_mjy_sr=None,
            background_mjy_sr=bg,
            aperture_coverage=coverage,
            n_aperture=n_ap,
            n_annulus=n_annulus,
        )
    net_sum = float(resid[ap].sum())

    # Empirical noise: repeat the same aperture sum at deterministic
    # pseudo-random empty positions. The MAD spread of those sums is the
    # flux error — it absorbs the noise correlation that bilinear
    # resampling introduces, which ``annulus_std * sqrt(n)`` misses.
    rng = np.random.default_rng(0x0B17A1)
    wy, wx = cut.shape
    empty_sums: list[float] = []
    margin = int(np.ceil(aperture_radius_px)) + 1
    for _ in range(noise_samples * 4):
        if len(empty_sums) >= noise_samples:
            break
        sx = float(rng.uniform(margin, max(margin + 1, wx - margin)))
        sy = float(rng.uniform(margin, max(margin + 1, wy - margin)))
        # sample centre in absolute pixel coords
        ax = x0 + sx
        ay = y0 + sy
        if np.hypot(ax - cx, ay - cy) < aperture_radius_px + 1.0:
            continue  # too close to the source aperture
        rs = np.hypot(xx - ax, yy - ay)
        sap = (rs <= aperture_radius_px) & np.isfinite(resid)
        if int(sap.sum()) < min_aperture_pixels:
            continue
        empty_sums.append(float(resid[sap].sum()))
    flux_err: float | None = None
    snr: float | None = None
    if len(empty_sums) >= 10:
        vals = np.asarray(empty_sums)
        med = float(np.median(vals))
        sigma = 1.4826 * float(np.median(np.abs(vals - med)))
        if sigma <= 0.0:
            sigma = float(np.std(vals))
        if sigma > 0.0:
            flux_err = sigma
            snr = float(net_sum / sigma)
    detected = bool(
        snr is not None
        and snr >= min_snr
        and coverage >= min_coverage
        and offset <= centroid_radius_px
    )
    return SourceMeasurement(
        x=cx,
        y=cy,
        offset_px=offset,
        detected=detected,
        snr=snr,
        net_sum_mjy_sr=net_sum,
        flux_err_mjy_sr=flux_err,
        background_mjy_sr=bg,
        aperture_coverage=coverage,
        n_aperture=n_ap,
        n_annulus=n_annulus,
    )


__all__ = ["SourceMeasurement", "measure_source"]
