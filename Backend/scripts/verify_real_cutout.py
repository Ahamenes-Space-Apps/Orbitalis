#!/usr/bin/env python3
"""Check the project's own CORE.02, CORE.03 and CORE.04 code against a real SPHEREx cutout (v2).

Run from the repository root so `orbitalis_core` imports:

    uv run python scripts/verify_real_cutout.py archive/irsa_manual_check_output/spherex_tutorial_cutout.fits

Every number comes from a function in orbitalis_core and is compared with an independent
astropy computation. v2 fixes two flaws in v1:
  * CORE.03: the reference now includes SIP (finite-difference Jacobian), because
    proj_plane_pixel_area ignores SIP, so v1 compared unlike things.
  * CORE.04: the integer-shift test now uses a SIP-free WCS. With SIP present, moving
    CRPIX moves the origin of the distortion polynomial, so it is not a pure translation.
"""

import sys
import warnings

import astropy.units as u
import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_area
from scipy import ndimage

from orbitalis_core.align.grid import to_common_grid
from orbitalis_core.calibration.solid_angle import solid_angle_map
from orbitalis_core.wcs import WaveTab, wavelength_at, wavelength_map

SR_TO_ARCSEC2 = (180.0 / np.pi * 3600.0) ** 2
ALLOWED_FLAG_BITS = (1 << 21) | (1 << 12)  # SOURCE and FULLSAMPLE stay unmasked
RESULTS = []


def banner(t):
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


def strip_sip(h):
    h = h.copy()
    for k in list(h):
        if k.startswith(("A_", "B_", "AP_", "BP_")):
            del h[k]
    return h


def clean_header(h):
    """No SIP coefficients and a plain TAN CTYPE, so astropy has nothing to apply."""
    h = strip_sip(h)
    for k in ("CTYPE1", "CTYPE2"):
        h[k] = h[k].replace("-SIP", "")
    return h


def local_area_arcsec2(w, x, y):
    """Independent pixel area at (x, y): central-difference Jacobian of pix2world.

    Uses all_pix2world, so SIP is included. cos(dec) and RA wrap are handled explicitly.
    """
    h = 0.5
    p = lambda a, b: np.array(w.all_pix2world(a, b, 0), dtype=float)  # noqa: E731
    dx = p(x + h, y) - p(x - h, y)
    dy = p(x, y + h) - p(x, y - h)
    for d in (dx, dy):
        d[0] = (d[0] + 180.0) % 360.0 - 180.0
    cosd = np.cos(np.deg2rad(p(x, y)[1]))
    jac = np.array([[dx[0] * cosd, dy[0] * cosd], [dx[1], dy[1]]]) * 3600.0
    return abs(np.linalg.det(jac))


path = sys.argv[1]
with fits.open(path) as hdul, warnings.catch_warnings():
    warnings.simplefilter("ignore", FITSFixedWarning)
    header = hdul["IMAGE"].header
    image = hdul["IMAGE"].data.astype(np.float64)
    flags = hdul["FLAGS"].data
    shape = image.shape
    wcs = WCS(header, relax=True)
    wcs_clean = WCS(clean_header(header), relax=True)
    print(
        f"cutout shape (rows, cols) = {shape}; CTYPE = {list(wcs.wcs.ctype)}; "
        f"SIP present: {wcs.sip is not None}; CRPIX = {wcs.wcs.crpix}"
    )

    # ---- CORE.03 solid_angle_map ------------------------------------------------
    banner("CORE.03  solid_angle_map vs an independent SIP-aware reference")
    omega = solid_angle_map(wcs, shape) * SR_TO_ARCSEC2  # arcsec^2
    cy, cx = shape[0] // 2, shape[1] // 2
    ref_fd = local_area_arcsec2(wcs, cx, cy)
    ref_plane = proj_plane_pixel_area(wcs) * 3600.0**2
    omega_clean = solid_angle_map(wcs_clean, shape) * SR_TO_ARCSEC2
    ref_fd_clean = local_area_arcsec2(wcs_clean, cx, cy)
    print(
        f"  function (with SIP)        centre {omega[cy, cx]:.4f}  min {omega.min():.4f}  max {omega.max():.4f} arcsec^2"
    )
    print(f"  finite-difference with SIP centre {ref_fd:.4f}")
    print(f"  astropy proj_plane (no SIP, at CRPIX) {ref_plane:.4f}")
    print(
        f"  function (SIP stripped)    centre {omega_clean[cy, cx]:.4f}; finite-difference stripped {ref_fd_clean:.4f}"
    )
    print(
        f"  SIP effect on pixel area at the centre: {100 * (omega[cy, cx] / omega_clean[cy, cx] - 1):+.3f} %"
    )
    print(
        f"  variation across the cutout (max/min - 1): {100 * (omega.max() / omega.min() - 1):.3f} %"
    )
    check(
        "function matches SIP-aware reference",
        abs(omega[cy, cx] / ref_fd - 1) < 5e-4,
        f"({100 * (omega[cy, cx] / ref_fd - 1):+.4f} %, limit 0.05 %)",
    )
    check(
        "function matches SIP-free reference when SIP is stripped",
        abs(omega_clean[cy, cx] / ref_fd_clean - 1) < 5e-4,
        f"({100 * (omega_clean[cy, cx] / ref_fd_clean - 1):+.4f} %)",
    )

    # ---- CORE.04 to_common_grid -------------------------------------------------
    banner("CORE.04  to_common_grid on the real header")
    bad = (flags & ~ALLOWED_FLAG_BITS) != 0
    img = np.where(bad, np.nan, image)
    scale_med = np.nanmedian(np.abs(img))
    scale_max = np.nanmax(np.abs(img))
    print(
        f"  masked pixels from FLAGS: {int(bad.sum())} of {bad.size}; |img| median {scale_med:.3g}, max {scale_max:.3g}"
    )

    out, foot = to_common_grid(img, wcs, wcs, shape)
    inside = np.isfinite(img) & np.isfinite(out)
    err = np.abs(out[inside] - img[inside]).max() / scale_max
    check(
        "identity regrid reproduces valid pixels",
        err < 1e-6,
        f"(max |diff| / max|img| = {err:.2e})",
    )

    # NaN spread: how far do invalid pixels grow on a regrid that needs no interpolation?
    in_nan = np.isnan(img)
    extra = np.isnan(out) & ~in_nan
    near = ndimage.binary_dilation(in_nan, structure=np.ones((3, 3), bool))
    print(
        f"  identity NaN: input {int(in_nan.sum())}, output {int(np.isnan(out).sum())}, "
        f"extra {int(extra.sum())} (within 1 px of a masked pixel: {int((extra & near).sum())}, elsewhere: {int((extra & ~near).sum())})"
    )
    print(
        f"  masked area grows x{np.isnan(out).sum() / max(in_nan.sum(), 1):.2f} on an identity regrid"
    )

    def shift_test(w, label):
        s = w.deepcopy()
        s.wcs.crpix = s.wcs.crpix + np.array([3.0, 0.0])  # target pixel p shows source pixel p-3
        o3, _ = to_common_grid(img, w, s, shape)
        both = np.isfinite(o3[:, 3:]) & np.isfinite(img[:, :-3])
        d = np.abs(o3[:, 3:][both] - img[:, :-3][both])
        print(
            f"  {label}: compared {int(both.sum())} px; max |diff| / max|img| = {d.max() / scale_max:.2e}; "
            f"/ median|img| = {d.max() / scale_med:.2e}"
        )
        return d.max() / scale_max

    e_clean = shift_test(wcs_clean, "3 px shift, SIP stripped (pure translation)")
    check("integer shift is exact without SIP", e_clean < 1e-6)
    shift_test(
        wcs, "3 px shift, SIP kept (informational: CRPIX move also moves the distortion origin)"
    )

    # ---- CORE.02 wavelength -----------------------------------------------------
    banner("CORE.02  wavelength_map vs astropy (SIP stripped and not stripped)")
    tab = WaveTab.from_hdu(hdul["WCS-WAVE"])
    wl = wavelength_map(header, tab, shape)
    spread_nm = 1000 * (np.nanmax(wl) - np.nanmin(wl))
    print(
        f"  wavelength over the cutout: min {np.nanmin(wl):.5f}  max {np.nanmax(wl):.5f} um (spread {spread_nm:.1f} nm)"
    )
    pts = [(0, 0), (10, 10), (30, 20), (57, 58)]
    w_clean = WCS(clean_header(header), hdul, key="W", relax=True)
    w_sip = WCS(header, hdul, key="W", relax=True)
    print(
        f"  astropy key=W has SIP object: {w_sip.sip is not None} (clean: {w_clean.sip is not None})"
    )
    print("  pixel(x,y)   ours(um)    astropy clean   astropy+SIP    ours-clean(nm)  ours-SIP(nm)")
    worst_clean = worst_sip = 0.0
    for x, y in pts:
        ours = wavelength_at(header, tab, x, y)
        c = w_clean.pixel_to_world(x, y)[0].to(u.um).value
        s = w_sip.pixel_to_world(x, y)[0].to(u.um).value
        worst_clean = max(worst_clean, abs(ours - c) * 1000)
        worst_sip = max(worst_sip, abs(ours - s) * 1000)
        print(
            f"  ({x:>3},{y:>3})  {ours:.6f}  {c:.6f}      {s:.6f}    {1000 * (ours - c):+.4f}        {1000 * (ours - s):+.4f}"
        )
    check(
        "wavelength matches astropy without SIP",
        worst_clean < 0.01,
        f"(worst {worst_clean:.5f} nm)",
    )
    print(
        f"  cost of astropy bug 18404 if the SIP-carrying WCS were used: up to {worst_sip:.2f} nm"
    )

    # ---- CORE.04a variance/flags on the real header + rotated grid ---------
    banner("CORE.04a  variance NaN footprint == image footprint (real SIP header)")
    from orbitalis_core.align.grid import to_common_grid_flags, to_common_grid_variance

    var_hdu = hdul["VARIANCE"] if "VARIANCE" in [h.name for h in hdul] else None
    variance = var_hdu.data.astype(np.float64) if var_hdu is not None else None
    if variance is not None:
        var_in = np.where(np.isfinite(img) & (variance > 0), variance, np.nan)
        # rotated target grid: rotate the source's PC matrix by ~0.05 rad
        # (real header carries PC + CDELT, not CD)
        wt = wcs.deepcopy()
        th = 0.05
        rot = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
        wt.wcs.pc = wcs.wcs.pc @ rot
        wt.wcs.set()
        vout = to_common_grid_variance(var_in, wcs, wt, shape)
        iout, _ = to_common_grid(img, wcs, wt, shape)
        same = np.isfinite(vout) == np.isfinite(iout)
        check(
            "variance NaN footprint == image (real header, rotated grid)",
            bool(same.all()),
            f"({int((~same).sum())} mismatched px)",
        )
        flags_out = to_common_grid_flags(flags.astype(np.int32), wcs, wt, shape)
        check(
            "flags resample returns int32 on grid",
            flags_out.dtype == np.int32 and flags_out.shape == shape,
        )
    else:
        print("  no VARIANCE HDU in this cutout; skipped")

banner("SUMMARY")
for n, ok in RESULTS:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}")
sys.exit(0 if all(ok for _, ok in RESULTS) else 1)
