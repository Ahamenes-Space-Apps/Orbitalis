"""Reprojection of frames onto a common sky grid.

Purpose: ``to_common_grid`` resamples one frame onto the field's shared
tangent-plane grid. Imaging stays in MJy/sr (surface brightness), so
interpolation conserves the quantity we combine — see SDD D-03.
Inputs: A 2-D float array in MJy/sr (NaN = masked), its celestial astropy
WCS, the target celestial WCS, and the output ``(rows, cols)`` shape.
Outputs: ``(array, footprint)`` — the resampled MJy/sr array and a boolean
footprint of output pixels covered by the input. Inputs are never mutated.
Called by: ``orbitalis_core.align`` consumers — the field pipeline and tests.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from astropy.wcs.utils import pixel_to_pixel
from reproject import reproject_interp

from orbitalis_core.errors import InvalidInputError, check_not_all_nan


def _check_celestial(wcs: Any, name: str) -> None:
    try:
        ctype = [str(c) for c in np.atleast_1d(wcs.wcs.ctype)]
    except AttributeError as exc:
        raise InvalidInputError(f"{name} must be an astropy WCS, got {type(wcs).__name__}") from exc
    if len(ctype) < 2 or not ctype[0].startswith("RA") or not ctype[1].startswith("DEC"):
        raise InvalidInputError(f"{name} must be a celestial RA/DEC WCS, got CTYPE={ctype}")


def to_common_grid(
    data: Any,
    wcs: Any,
    target_wcs: Any,
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Reproject ``data`` onto ``target_wcs`` with shape ``(rows, cols)``.

    ``data`` must be a 2-D numeric array in MJy/sr; NaN marks invalid pixels
    and propagates through the interpolation. Returns the resampled array
    (float64, NaN where uncovered) and ``footprint``, the boolean coverage
    mask reproject reports. Mask dilation for conservative footprints is the
    caller's job (DS spec §7, roadmap PL.01).

    Known cost of bilinear interpolation (measured in
    ``tests/core/test_align.py::test_bilinear_peak_smoothing_is_measured``):
    a sigma=1 px point source loses ~17% of peak flux and noise is
    correlated/suppressed (~0.64x std). ``to_common_grid`` resamples the
    image only — variance arrays need their own path because resampling
    variance is not the same operation (roadmap CORE.04a). An all-NaN
    result raises ``EmptyResultError`` (SDD §6.1).
    """
    arr = np.asarray(data)
    if arr.ndim != 2 or arr.dtype.kind not in "fiu":
        raise InvalidInputError("data must be a 2-D numeric MJy/sr array")
    if (
        not isinstance(shape, tuple)
        or len(shape) != 2
        or any(not isinstance(n, int) or n < 1 for n in shape)
    ):
        raise InvalidInputError(
            f"shape must be a (rows, cols) tuple of positive ints, got {shape!r}"
        )
    _check_celestial(wcs, "wcs")
    _check_celestial(target_wcs, "target_wcs")

    out, footprint = reproject_interp(
        (arr.astype(np.float64, copy=False), wcs),
        target_wcs,
        tuple(shape),
        order="bilinear",
        return_footprint=True,
    )
    out = check_not_all_nan(np.asarray(out, dtype=np.float64), name="reprojected frame")
    return out, np.asarray(footprint, dtype=bool)


# -- variance and flags resampling (CORE.04a, D-29) -------------------------
#
# ``to_common_grid`` resamples the image with bilinear weights w_i. The
# variance of that estimator is sum(w_i^2 * sigma_i^2) under independent
# pixel noise — resampling the variance array itself would apply the
# unsquared weights and underestimate it. Flags are bit fields, not
# continuous values, so they are resampled as a conservative bitwise OR
# over the same footprint instead of being interpolated.


def _input_pixel_coords(
    wcs: Any, target_wcs: Any, shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Input-pixel (row, col) coordinate of every output pixel.

    Uses the same astropy ``pixel_to_pixel`` transform (including SIP and
    distortion) plus the >1 px round-trip rejection that
    ``reproject_interp`` applies, so the support footprint matches the
    image resampling exactly.
    """
    ny, nx = shape
    yy, xx = np.meshgrid(np.arange(ny, dtype=float), np.arange(nx, dtype=float), indexing="ij")
    x_out, y_out = xx.ravel(), yy.ravel()
    xi, yi = pixel_to_pixel(target_wcs, wcs, x_out, y_out)
    xr, yr = pixel_to_pixel(wcs, target_wcs, xi, yi)
    bad = (np.abs(xr - x_out) > 1.0) | (np.abs(yr - y_out) > 1.0)
    xi = np.where(bad, np.nan, xi)
    yi = np.where(bad, np.nan, yi)
    return yi.reshape(ny, nx), xi.reshape(ny, nx)


def _clip_edge(c: np.ndarray, n: int) -> np.ndarray:
    """Mirror reproject's ``_clip_coords`` for order<=1 interpolation."""
    return np.where(
        (c >= n - 1) & (c < n - 0.5), float(n - 1), np.where((c >= -0.5) & (c < 0), 0.0, c)
    )


def _bilinear_stencil(
    y_in: np.ndarray, x_in: np.ndarray, shape_in: tuple[int, int]
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
]:
    """Bilinear neighbour indices/weights for each output pixel.

    Returns ``(valid, y0, x0, y1, x1, wy0, wy1, wx0, wx1)`` where weights are
    the per-axis factors (wx0+wx1 == 1 etc.). ``valid`` is False where the
    coordinate falls outside the half-pixel border band — the same pixels
    ``reproject_interp`` leaves NaN.
    """
    ny, nx = shape_in
    y = _clip_edge(np.asarray(y_in, dtype=np.float64), ny)
    x = _clip_edge(np.asarray(x_in, dtype=np.float64), nx)
    valid = np.isfinite(y) & np.isfinite(x) & (y >= 0) & (y <= ny - 1) & (x >= 0) & (x <= nx - 1)
    yg = np.clip(np.nan_to_num(y), 0.0, ny - 1.0)
    xg = np.clip(np.nan_to_num(x), 0.0, nx - 1.0)
    # scipy's order-1 spline reads a 2-tap window {floor(c), floor(c)+1} but
    # shifts it one pixel down when floor(c) == n-1 (verified empirically:
    # at c == n-1 the footprint is {n-2, n-1}, not {n-1, n}). The extra tap
    # has zero weight but still poisons on an in-bounds NaN, so the stencil
    # must match or the variance/flags NaN footprint diverges from the
    # image's at the border.
    y0 = np.minimum(np.floor(yg), max(ny - 2, 0)).astype(np.int64)
    x0 = np.minimum(np.floor(xg), max(nx - 2, 0)).astype(np.int64)
    # raw neighbour indices may still be one past the edge on the low side
    # is impossible (coords are clipped to >= 0); y1/x1 stay in bounds after
    # the cap except for degenerate size-1 axes. Consumers must treat
    # out-of-bounds neighbours like scipy's ``cval`` — for variance that is
    # NaN (0*NaN poisons exactly like the image path); for flags it is
    # "no contribution".
    y1 = y0 + 1
    x1 = x0 + 1
    dy = yg - y0
    dx = xg - x0
    return valid, y0, x0, y1, x1, 1.0 - dy, dy, 1.0 - dx, dx


def _gather(arr: np.ndarray, yy: np.ndarray, xx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(arr[yy, xx], inside)`` — clipped gather plus an in-bounds mask."""
    ny, nx = arr.shape
    inside = (yy >= 0) & (yy < ny) & (xx >= 0) & (xx < nx)
    yc = np.clip(yy, 0, ny - 1)
    xc = np.clip(xx, 0, nx - 1)
    return arr[yc, xc], inside


def _corner_contrib(val: np.ndarray, inside: np.ndarray, w2: np.ndarray) -> np.ndarray:
    """One bilinear corner's contribution to a squared-weight resample.

    Matches scipy ``map_coordinates`` order=1 ``cval=nan`` semantics exactly:
    an in-bounds NaN poisons at *any* weight (``0 * NaN = NaN``), while an
    out-of-bounds corner only poisons when its weight is non-zero.
    """
    return np.where(inside, w2 * val, np.where(w2 > 0, np.nan, 0.0))


def _check_products_wcs(wcs: Any, target_wcs: Any, shape: tuple[int, int], name: str) -> None:
    _check_celestial(wcs, f"{name} wcs")
    _check_celestial(target_wcs, f"{name} target_wcs")
    if (
        not isinstance(shape, tuple)
        or len(shape) != 2
        or any(not isinstance(n, int) or n < 1 for n in shape)
    ):
        raise InvalidInputError(f"{name} shape must be (rows, cols) positive ints, got {shape!r}")


def to_common_grid_variance(
    variance: Any, wcs: Any, target_wcs: Any, shape: tuple[int, int]
) -> np.ndarray:
    """Resample a per-pixel variance array onto the common grid.

    For each output pixel the bilinear estimator is ``sum(w_i x_i)`` with
    ``sum(w_i) = 1``; its variance is ``sum(w_i^2 sigma_i^2)``. ``variance``
    must be a 2-D array on the same pixel grid as the image that
    ``to_common_grid`` consumes; NaN marks pixels with no valid variance
    and propagates exactly like image NaN. Returns float64 with NaN where
    uncovered or unsupported. Negative finite values raise
    ``InvalidInputError`` — variance is a squared quantity.
    """
    raw = np.asarray(variance)
    if raw.ndim != 2 or raw.dtype.kind not in "fiu":
        raise InvalidInputError("variance must be a 2-D numeric array")
    var = raw.astype(np.float64)
    if np.any(np.isfinite(var) & (var < 0)):
        raise InvalidInputError("variance contains negative values")
    _check_products_wcs(wcs, target_wcs, shape, "variance")
    y_in, x_in = _input_pixel_coords(wcs, target_wcs, shape)
    valid, y0, x0, y1, x1, wy0, wy1, wx0, wx1 = _bilinear_stencil(y_in, x_in, var.shape)
    # Corner contributions follow scipy cval semantics so the variance NaN
    # footprint matches the image's exactly (see _corner_contrib).
    out = np.zeros(shape, np.float64)
    for yy, xx, wy, wx in (
        (y0, x0, wy0, wx0),
        (y0, x1, wy0, wx1),
        (y1, x0, wy1, wx0),
        (y1, x1, wy1, wx1),
    ):
        val, inside = _gather(var, yy, xx)
        out = out + _corner_contrib(val, inside, (wy * wx) ** 2)
    out = np.where(valid, out, np.nan)
    # all-NaN is allowed: the caller decides whether a frame without any
    # valid variance is still storable (the image may be fine)
    return out


def to_common_grid_flags(
    flags: Any, wcs: Any, target_wcs: Any, shape: tuple[int, int]
) -> np.ndarray:
    """Resample native FLAGS onto the common grid as int32.

    Flag bits are categorical, so interpolation is meaningless; instead each
    output pixel gets the bitwise OR of every native pixel in its bilinear
    stencil — all four corners are read even at zero weight (that is what
    makes NaN neighbours poison the image), so the OR covers the same
    footprint. This is conservative: a bad native pixel flags every output
    pixel it can influence. Output is 0 where the coordinate is uncovered —
    consumers rely on the image NaN mask anyway.
    """
    fl = np.asarray(flags)
    if fl.ndim != 2 or fl.dtype.kind not in "iu":
        raise InvalidInputError("flags must be a 2-D integer array")
    _check_products_wcs(wcs, target_wcs, shape, "flags")
    y_in, x_in = _input_pixel_coords(wcs, target_wcs, shape)
    valid, y0, x0, y1, x1, *_ = _bilinear_stencil(y_in, x_in, fl.shape)
    fl64 = fl.astype(np.int64)
    ny, nx = fl.shape
    out = np.zeros(shape, np.int64)
    for yy, xx in ((y0, x0), (y0, x1), (y1, x0), (y1, x1)):
        val, inside = _gather(fl64, yy, xx)
        out |= np.where(inside, val, 0)
    out = np.where(valid, out, 0).astype(np.int32)
    return out


__all__ = ["to_common_grid", "to_common_grid_flags", "to_common_grid_variance"]
