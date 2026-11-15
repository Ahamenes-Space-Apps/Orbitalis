"""Sky-motion <-> pixel-motion conversion using the local celestial WCS Jacobian.

Purpose: Convert sky-plane motion (arcseconds/day) to pixel-plane motion
(pixels/day) on the common image grid, and back, using the local celestial WCS
Jacobian at a supplied reference pixel. Implements SDD D-17: API and storage use
(``mu_e``, ``mu_n``) in arcseconds/day; core internals use pixels/day; one
conversion function using the local WCS Jacobian with a round-trip property.

Inputs:
    An astropy celestial WCS (RA/DEC), a reference pixel, and motion components
    in arcseconds/day or pixels/day.

Outputs:
    ``(dx_px_per_day, dy_px_per_day)`` or ``(mu_e_arcsec_per_day,
    mu_n_arcsec_per_day)`` tuples of finite floats.

Called by:
    Research handlers (to build ``MotionVector`` grids) and tests. Not by the
    gateway, pipeline, API, or store directly.

Notes:
    * The conversion is a local first-order (Jacobian) approximation about the
      reference pixel. It is exact for linear (pure TAN without SIP) WCS and a
      good local approximation for distorted WCS when motions are small relative
      to the distortion scale.
    * RA differences are never used directly as eastward offsets; the
      cos(dec) factor and the WCS projection geometry are handled by the
      astropy world/pixel transforms.
    * The WCS is never mutated.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from astropy.wcs import WCS

from ..errors import InvalidInputError

# SPHEREx native pixel scale used by the D-17 grid-step convention
# (config/stack_policy.yaml and D-17 both cite ~6.15 arcsec/pixel).
DEFAULT_PIXEL_SCALE_ARCSEC_PER_PIXEL = 6.15

# Default sky-motion prior radius (arcseconds/day) from D-17.
DEFAULT_PRIOR_RADIUS_ARCSEC_PER_DAY = 8.0


def _as_float(value: Any, name: str) -> float:
    """Coerce ``value`` to a finite real float, rejecting bool/complex."""
    if isinstance(value, (bool, np.bool_)):
        raise InvalidInputError(f"{name} must be a finite real number")
    if isinstance(value, (complex, np.complexfloating)):
        raise InvalidInputError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInputError(f"{name} must be a finite real number") from exc
    if not np.isfinite(result):
        raise InvalidInputError(f"{name} must be a finite real number")
    return result


def _validate_celestial_wcs(wcs: Any, name: str = "wcs") -> WCS:
    """Return ``wcs`` as an astropy WCS, validating it is celestial (RA/DEC)."""
    if not isinstance(wcs, WCS):
        raise InvalidInputError(f"{name} must be an astropy WCS object")
    try:
        ctype = [str(c) for c in np.atleast_1d(wcs.wcs.ctype)]
    except (AttributeError, ValueError) as exc:
        raise InvalidInputError(f"{name} has no usable celestial CTYPE information") from exc
    if len(ctype) < 2 or not ctype[0].startswith("RA") or not ctype[1].startswith("DEC"):
        raise InvalidInputError(f"{name} must be a celestial RA/DEC WCS, got CTYPE={ctype}")
    return wcs


def _validate_reference_pixel(
    wcs: WCS, x: float, y: float, name: str = "reference_pixel"
) -> tuple[float, float]:
    """Validate that the reference pixel is finite and within the WCS projection plane."""
    x = _as_float(x, f"{name}[0]")
    y = _as_float(y, f"{name}[1]")

    # astropy's pixel-to-world raises ValueError/TypeError on pixels that
    # cannot be transformed; it can also return non-finite world coordinates
    # without raising. Both mean "outside the projection plane".
    try:
        world = wcs.pixel_to_world(x, y)
        ra = float(world.ra.degree)
        dec = float(world.dec.degree)
    except (ValueError, TypeError) as exc:
        raise InvalidInputError(f"{name} ({x}, {y}) is outside the WCS projection plane") from exc
    if not (np.isfinite(ra) and np.isfinite(dec)):
        raise InvalidInputError(f"{name} ({x}, {y}) is outside the WCS projection plane")
    return x, y


def _ra_delta_deg(ra_a: float, ra_b: float) -> float:
    """Shortest signed RA difference ``ra_a - ra_b`` in degrees, wrapped to [-180, 180).

    Plain subtraction jumps ~360 deg across the 0/360 meridian and would
    produce a garbage Jacobian for fields near RA 0.
    """
    return (ra_a - ra_b + 180.0) % 360.0 - 180.0


def _local_jacobian(wcs: WCS, x: float, y: float) -> np.ndarray:
    """Return the local 2x2 Jacobian mapping pixel offset -> sky offset.

    The returned array ``J`` maps a small pixel offset ``[dx, dy]`` (columns,
    rows) to a sky offset ``[d_lon_coslat, d_lat]`` in degrees, where
    ``d_lon_coslat = d_ra_deg * cos(dec)``. The rows correspond to
    (east-like, north-like) sky components and the columns to (x, y) pixel
    components::

        [d_lon_coslat]   [J00 J01] [dx]
        [d_lat       ] = [J10 J11] [dy]

    Computed by finite differences through ``wcs.pixel_to_world`` at the
    reference pixel, with the longitude column scaled by cos(dec) so that both
    sky components are true angular separations (degrees) on the local tangent
    plane. This correctly accounts for rotation, parity (negative CD axes), and
    local projection geometry (including SIP via the underlying astropy WCS).
    """
    world = wcs.pixel_to_world(x, y)
    dec_deg = float(world.dec.degree)

    h = 1e-3  # finite-difference step, small relative to one pixel

    # Perturb x (column) direction.
    wx_plus = wcs.pixel_to_world(x + h, y)
    wx_minus = wcs.pixel_to_world(x - h, y)
    # Perturb y (row) direction.
    wy_plus = wcs.pixel_to_world(x, y + h)
    wy_minus = wcs.pixel_to_world(x, y - h)

    d_ra_dx = _ra_delta_deg(float(wx_plus.ra.degree), float(wx_minus.ra.degree)) / (2.0 * h)
    d_ra_dy = _ra_delta_deg(float(wy_plus.ra.degree), float(wy_minus.ra.degree)) / (2.0 * h)
    d_dec_dx = (float(wx_plus.dec.degree) - float(wx_minus.dec.degree)) / (2.0 * h)
    d_dec_dy = (float(wy_plus.dec.degree) - float(wy_minus.dec.degree)) / (2.0 * h)

    cos_dec = np.cos(np.deg2rad(dec_deg))

    # Convert RA-rate (deg/day per px) to eastward angular rate by multiplying
    # by cos(dec). Both sky components are now true angular degrees.
    J = np.array(
        [
            [d_ra_dx * cos_dec, d_ra_dy * cos_dec],
            [d_dec_dx, d_dec_dy],
        ],
        dtype=np.float64,
    )
    return J


def sky_motion_to_pixel(
    mu_e_arcsec_per_day: float,
    mu_n_arcsec_per_day: float,
    wcs: Any,
    reference_pixel: tuple[float, float],
) -> tuple[float, float]:
    """Convert sky motion (arcsec/day) to pixel motion (pixels/day).

    Uses the local celestial WCS Jacobian at ``reference_pixel``. The input is
    a Cartesian tangent-plane velocity: ``mu_e`` eastward and ``mu_n`` northward
    in arcseconds/day. The output is ``(dx_px_per_day, dy_px_per_day)`` in
    pixels/day on the common image grid.

    The conversion solves::

        [d_lon_coslat_deg]   [J00 J01] [dx]
        [d_lat_deg       ] = [J10 J11] [dy]

    where the left side is the sky motion converted from arcseconds to degrees,
    and ``J`` is the local pixel->sky Jacobian. Inverting ``J`` yields the pixel
    velocity that produces the requested sky motion.

    Args:
        mu_e_arcsec_per_day: Eastward sky motion in arcseconds/day.
        mu_n_arcsec_per_day: Northward sky motion in arcseconds/day.
        wcs: A celestial astropy WCS (RA/DEC) for the common image grid.
        reference_pixel: ``(x, y)`` pixel coordinates (0-based, column, row) at
            which the local Jacobian is evaluated.

    Returns:
        ``(dx_px_per_day, dy_px_per_day)`` as finite floats.

    Raises:
        InvalidInputError: If the WCS is not celestial, the reference pixel is
            invalid, the motion values are non-finite, or the Jacobian is
            singular (cannot invert).
    """
    mu_e = _as_float(mu_e_arcsec_per_day, "mu_e_arcsec_per_day")
    mu_n = _as_float(mu_n_arcsec_per_day, "mu_n_arcsec_per_day")
    wcs = _validate_celestial_wcs(wcs, "wcs")
    x, y = _validate_reference_pixel(wcs, reference_pixel[0], reference_pixel[1])

    jacobian = _local_jacobian(wcs, x, y)

    # Sky motion in degrees (true angular components on the local tangent plane).
    sky_motion_deg = np.array([mu_e / 3600.0, mu_n / 3600.0], dtype=np.float64)

    try:
        pixel_motion = np.linalg.solve(jacobian, sky_motion_deg)
    except np.linalg.LinAlgError as exc:
        raise InvalidInputError(
            "celestial WCS Jacobian is singular at the reference pixel; "
            "cannot convert sky motion to pixel motion"
        ) from exc

    dx = float(pixel_motion[0])
    dy = float(pixel_motion[1])
    if not (np.isfinite(dx) and np.isfinite(dy)):
        raise InvalidInputError("conversion produced non-finite pixel motion")
    return dx, dy


def pixel_motion_to_sky(
    dx_px_per_day: float,
    dy_px_per_day: float,
    wcs: Any,
    reference_pixel: tuple[float, float],
) -> tuple[float, float]:
    """Convert pixel motion (pixels/day) to sky motion (arcsec/day).

    The inverse of :func:`sky_motion_to_pixel`. Uses the same local celestial
    WCS Jacobian at ``reference_pixel``.

    Args:
        dx_px_per_day: Horizontal (column/x) pixel rate in pixels/day.
        dy_px_per_day: Vertical (row/y) pixel rate in pixels/day.
        wcs: A celestial astropy WCS (RA/DEC) for the common image grid.
        reference_pixel: ``(x, y)`` pixel coordinates (0-based, column, row) at
            which the local Jacobian is evaluated.

    Returns:
        ``(mu_e_arcsec_per_day, mu_n_arcsec_per_day)`` as finite floats.

    Raises:
        InvalidInputError: If the WCS is not celestial, the reference pixel is
            invalid, the motion values are non-finite, or the Jacobian is
            singular.
    """
    dx = _as_float(dx_px_per_day, "dx_px_per_day")
    dy = _as_float(dy_px_per_day, "dy_px_per_day")
    wcs = _validate_celestial_wcs(wcs, "wcs")
    x, y = _validate_reference_pixel(wcs, reference_pixel[0], reference_pixel[1])

    jacobian = _local_jacobian(wcs, x, y)

    pixel_motion = np.array([dx, dy], dtype=np.float64)
    sky_motion_deg = jacobian @ pixel_motion  # [d_lon_coslat, d_lat] in degrees

    mu_e = float(sky_motion_deg[0] * 3600.0)
    mu_n = float(sky_motion_deg[1] * 3600.0)
    if not (np.isfinite(mu_e) and np.isfinite(mu_n)):
        raise InvalidInputError("conversion produced non-finite sky motion")
    return mu_e, mu_n


def generate_sky_motion_grid(
    max_time_span_days: float,
    *,
    radius_arcsec_per_day: float = DEFAULT_PRIOR_RADIUS_ARCSEC_PER_DAY,
    pixel_scale_arcsec_per_pixel: float = DEFAULT_PIXEL_SCALE_ARCSEC_PER_PIXEL,
) -> list[tuple[float, float]]:
    """Generate a deterministic Cartesian sky-motion grid in arcseconds/day.

    Implements the D-17 grid convention::

        step = 0.5 px * 6.15 arcsec/px / max_time_span_days   (arcsec/day)

    The grid is Cartesian in ``(mu_e, mu_n)``, includes exactly ``(0.0, 0.0)``,
    and contains only points inside the circular prior radius (Euclidean
    distance from the origin <= radius). Points are generated over a square
    bounding box and filtered to the circle, so rounding cannot place a point
    outside the radius.

    Args:
        max_time_span_days: The field's time span in days. Must be finite and
            greater than zero.
        radius_arcsec_per_day: Circular prior radius in arcseconds/day. Must be
            finite and greater than zero. Defaults to 8.0 (D-17).
        pixel_scale_arcsec_per_pixel: Native pixel scale in arcseconds/pixel.
            Used only for the step-size convention. Defaults to 6.15
            (SPHEREx native scale cited by D-17).

    Returns:
        A list of ``(mu_e, mu_n)`` tuples in arcseconds/day. The origin
        ``(0.0, 0.0)`` is included exactly once. Ordering is deterministic
        (sorted by ``mu_n`` then ``mu_e``).

    Raises:
        InvalidInputError: If ``max_time_span_days`` or ``radius`` are invalid,
        or if any generated value is non-finite.
    """
    max_time_span_days = _as_float(max_time_span_days, "max_time_span_days")
    if max_time_span_days <= 0.0:
        raise InvalidInputError("max_time_span_days must be greater than zero")
    radius_arcsec_per_day = _as_float(radius_arcsec_per_day, "radius_arcsec_per_day")
    if radius_arcsec_per_day <= 0.0:
        raise InvalidInputError("radius_arcsec_per_day must be greater than zero")
    pixel_scale_arcsec_per_pixel = _as_float(
        pixel_scale_arcsec_per_pixel, "pixel_scale_arcsec_per_pixel"
    )
    if pixel_scale_arcsec_per_pixel <= 0.0:
        raise InvalidInputError("pixel_scale_arcsec_per_pixel must be greater than zero")

    step_arcsec_per_day = 0.5 * pixel_scale_arcsec_per_pixel / max_time_span_days
    if not np.isfinite(step_arcsec_per_day) or step_arcsec_per_day <= 0.0:
        raise InvalidInputError("computed grid step is not finite or positive")

    # Half-width of the square bounding box (inclusive). Use floor to avoid
    # generating a point just outside the radius due to ceil rounding.
    half_steps = int(np.floor(radius_arcsec_per_day / step_arcsec_per_day))

    points: list[tuple[float, float]] = []
    radius_sq = radius_arcsec_per_day * radius_arcsec_per_day
    # Small epsilon to include boundary points whose squared distance is
    # within rounding error of the radius.
    boundary_eps = 1e-12 * max(1.0, radius_sq)

    for iy in range(-half_steps, half_steps + 1):
        mu_n = iy * step_arcsec_per_day
        for ix in range(-half_steps, half_steps + 1):
            mu_e = ix * step_arcsec_per_day
            if mu_e == 0.0 and mu_n == 0.0:
                # Ensure exact zero representation.
                points.append((0.0, 0.0))
                continue
            dist_sq = mu_e * mu_e + mu_n * mu_n
            if dist_sq <= radius_sq + boundary_eps:
                points.append((mu_e, mu_n))

    # Deduplicate (defensive; the integer-lattice generation cannot produce
    # duplicates, but guard against step == 0 edge cases).
    unique: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for mu_e, mu_n in points:
        key = (mu_e, mu_n)
        if key not in seen:
            seen.add(key)
            unique.append(key)

    # Deterministic ordering: north then east.
    unique.sort(key=lambda p: (p[1], p[0]))

    # Final finite check.
    for mu_e, mu_n in unique:
        if not (np.isfinite(mu_e) and np.isfinite(mu_n)):
            raise InvalidInputError("grid contains a non-finite motion value")

    return unique


def sky_motion_grid_to_motion_vectors(
    grid: list[tuple[float, float]],
    wcs: Any,
    reference_pixel: tuple[float, float],
) -> list:
    """Convert a sky-motion grid to a list of ``MotionVector`` instances.

    Convenience wrapper that applies :func:`sky_motion_to_pixel` to every point
    in ``grid`` and returns ``MotionVector(dx_px_per_day, dy_px_per_day)``
    objects ready for :func:`orbitalis_core.shiftstack.search.search`.

    Args:
        grid: A list of ``(mu_e, mu_n)`` tuples in arcseconds/day (e.g. from
            :func:`generate_sky_motion_grid`).
        wcs: A celestial astropy WCS for the common image grid.
        reference_pixel: ``(x, y)`` reference pixel for the Jacobian.

    Returns:
        A list of ``MotionVector`` instances.

    Raises:
        InvalidInputError: If any point cannot be converted (invalid WCS,
            singular Jacobian, or non-finite results).
    """
    from .models import MotionVector

    # The Jacobian depends only on the reference pixel, not on the motion
    # point — compute it once instead of ~1,000 times.
    wcs = _validate_celestial_wcs(wcs, "wcs")
    x, y = _validate_reference_pixel(wcs, reference_pixel[0], reference_pixel[1])
    jacobian = _local_jacobian(wcs, x, y)

    vectors: list = []
    for mu_e, mu_n in grid:
        mu_e = _as_float(mu_e, "grid mu_e")
        mu_n = _as_float(mu_n, "grid mu_n")
        sky_motion_deg = np.array([mu_e / 3600.0, mu_n / 3600.0], dtype=np.float64)
        try:
            pixel_motion = np.linalg.solve(jacobian, sky_motion_deg)
        except np.linalg.LinAlgError as exc:
            raise InvalidInputError(
                "celestial WCS Jacobian is singular at the reference pixel; "
                "cannot convert sky motion to pixel motion"
            ) from exc
        dx, dy = float(pixel_motion[0]), float(pixel_motion[1])
        if not (np.isfinite(dx) and np.isfinite(dy)):
            raise InvalidInputError("conversion produced non-finite pixel motion")
        vectors.append(MotionVector(dx_px_per_day=dx, dy_px_per_day=dy))
    return vectors


__all__ = [
    "DEFAULT_PIXEL_SCALE_ARCSEC_PER_PIXEL",
    "DEFAULT_PRIOR_RADIUS_ARCSEC_PER_DAY",
    "generate_sky_motion_grid",
    "pixel_motion_to_sky",
    "sky_motion_grid_to_motion_vectors",
    "sky_motion_to_pixel",
]
