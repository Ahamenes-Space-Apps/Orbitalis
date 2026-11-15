#!/usr/bin/env python3
"""DSP preprocessing experiments on real demo-bundle fields.

Purpose: quantify how candidate yield changes under alternative
preprocessing before touching ``orbitalis_core`` — the parked DSP list
(interpolation kernel, noise model, PSF matched filter, low-rank
background) needs evidence on real SPHEREx data, not just synthetic.

Method: load ``demo_bundle`` into a scratch SQLite DB (offline), decode
each field's stored frames exactly as ``moving_object_search`` does, then
run the shared ``run_science_search`` path once per variant:

    baseline — stored frames as-is (variant e, the shipped default)
    mf       — per-frame PSF matched filter (masked Gaussian, sigma=1 px)
               applied to image AND variance before the science path
    lr       — per-pixel low-rank temporal background subtraction
               (rank-k SVD of the frame cube), variant a on top
    mf+lr    — both

Matched-filter approximation: image' = G*I / G*M, variance' = G*V / G*M
(M = finite mask). This treats the stored variance map as the local noise
level; it is NOT a rigorous noise-propagation through the kernel — fine
for a candidate-count experiment, flagged as such in the output.

Low-rank background: cube (npix, n_frames) with NaN -> column mean, SVD,
keep top-k components, subtract reconstruction from finite pixels only.
Cross-wavelength frames share the same pixels on the common grid, so a
low-rank model captures shared optical/background structure; per-frame
wavelength dependence is NOT modelled (limitation, noted in output).

Truth: real fields have no injected movers, so "candidates" are
unconfirmed detections — mostly noise/clutter. The metric is yield at a
fixed extraction threshold plus the stack_snr distribution; fewer
candidates at equal-or-better SNR separation is the win condition.
Static-source flagged fraction is reported separately.

Outputs: stdout tables + ``out/dsp_experiments.json``.
Called by: ``uv run python scripts/dsp_experiments.py [field_id ...]``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy import ndimage

from orbitalis.demo import load_bundle
from orbitalis.research import search as sm
from orbitalis.research.policy import load_stack_policy
from orbitalis.settings import Settings
from orbitalis.store import get_field, list_frames, open_database

BUNDLE = Path(__file__).resolve().parents[1] / "demo_bundle"
OUT = Path(__file__).resolve().parents[1] / "out" / "dsp_experiments.json"

MF_SIGMA_PX = 1.0  # PROVISIONAL: ~SPHEREx PSF core width on the 6.15"/px grid
LR_RANK = 3  # PROVISIONAL: low-rank background components
VARIANTS = ("baseline", "mf", "lr", "mf+lr")


def _matched_filter(frames: list) -> list:
    """Gaussian PSF matched filter on image+variance (masked, renormalized)."""
    out = []
    for fr in frames:
        img = np.asarray(fr.image, dtype=np.float64)
        var = np.asarray(fr.variance, dtype=np.float64)
        finite = np.isfinite(img)
        img0 = np.where(finite, img, 0.0)
        var0 = np.where(finite & np.isfinite(var) & (var > 0), var, 0.0)
        den = ndimage.gaussian_filter(finite.astype(np.float64), MF_SIGMA_PX)
        num = ndimage.gaussian_filter(img0, MF_SIGMA_PX)
        vnum = ndimage.gaussian_filter(var0, MF_SIGMA_PX)
        with np.errstate(invalid="ignore", divide="ignore"):
            new_img = np.where(den > 1e-3, num / den, np.nan)
            new_var = np.where(den > 1e-3, vnum / den, np.nan)
        out.append(
            sm.DecodedFrame(
                frame_ref=fr.frame_ref,
                image=new_img.astype(np.float32),
                variance=new_var.astype(np.float32),
                mask=fr.mask,
                flags=fr.flags,
                mjd=fr.mjd,
                detector=fr.detector,
                wavelength_um=fr.wavelength_um,
                meta=fr.meta,
            )
        )
    return out


def _lowrank_background(frames: list, rank: int = LR_RANK) -> list:
    """Subtract a rank-k temporal SVD background from each frame."""
    cube = np.stack(
        [np.asarray(f.image, dtype=np.float64).ravel() for f in frames], axis=1
    )  # (npix, n)
    finite = np.isfinite(cube)
    counts = finite.sum(axis=1)
    sums = np.where(finite, cube, 0.0).sum(axis=1)
    col_mean = np.where(counts > 0, sums / np.maximum(counts, 1), 0.0)
    filled = np.where(finite, cube, col_mean[:, None])
    # economy SVD on the filled cube
    u, s, vt = np.linalg.svd(filled - filled.mean(axis=1, keepdims=True), full_matrices=False)
    k = min(rank, u.shape[1])
    bg = (u[:, :k] * s[:k]) @ vt[:k] + filled.mean(axis=1, keepdims=True)
    out = []
    for i, fr in enumerate(frames):
        img = np.asarray(fr.image, dtype=np.float64)
        resid = img - bg[:, i].reshape(img.shape)
        resid = np.where(np.isfinite(img), resid, np.nan)
        out.append(
            sm.DecodedFrame(
                frame_ref=fr.frame_ref,
                image=resid.astype(np.float32),
                variance=fr.variance,
                mask=fr.mask,
                flags=fr.flags,
                mjd=fr.mjd,
                detector=fr.detector,
                wavelength_um=fr.wavelength_um,
                meta=fr.meta,
            )
        )
    return out


async def _load_fields(db, bundle: Path) -> list[str]:
    await load_bundle(db, bundle)
    return sorted(p.name for p in bundle.iterdir() if p.is_dir())


async def _run_variant(decoded, field, rows, policy, settings, variant):
    """Run the shared science path; return compact metrics."""
    shape = decoded[0].image.shape
    wcs = sm._resolve_field_wcs(field, rows, shape)
    h, w = shape
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    mvs = sm._default_motion_vectors(wcs, cx, cy, policy)
    bg_variant = "a" if variant in ("lr", "mf+lr") else "e"
    t0 = time.monotonic()
    sci = sm.run_science_search(
        decoded,
        motion_vectors=mvs,
        min_coverage_fraction=0.5,
        detection_threshold_sigma=None,
        nms_radius_px=sm.EXTRACTION_NMS_RADIUS_PX,
        background_variant=bg_variant,
        work_budget=settings.search_max_work,
    )
    dt = time.monotonic() - t0
    cands = sci.candidates
    snrs = [float(c["stack_snr"]) for c in cands]
    flagged = sum(1 for c in cands if c.get("_static_ev", {}).get("flagged_static_source"))
    return {
        "candidates": len(cands),
        "static_flagged": flagged,
        "snr_max": max(snrs) if snrs else None,
        "snr_median": float(np.median(snrs)) if snrs else None,
        "snr_p90": float(np.percentile(snrs, 90)) if snrs else None,
        "runtime_s": round(dt, 2),
        "noise_calibration": sci.noise_calibration,
    }


async def main() -> None:
    only = set(sys.argv[1:])
    settings = Settings()
    policy = load_stack_policy("config/stack_policy.yaml")
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as td:
        db = await open_database(Path(td) / "bundle.db")
        try:
            field_ids = await _load_fields(db, BUNDLE)
            for fid in field_ids:
                if only and fid not in only:
                    continue
                field = await get_field(db, fid)
                rows = await list_frames(db, fid)
                decoded, _meta = sm._decode_stored_frames(rows)
                variants_frames = {
                    "baseline": decoded,
                    "mf": _matched_filter(decoded),
                    "lr": _lowrank_background(decoded),
                    "mf+lr": _matched_filter(_lowrank_background(decoded)),
                }
                row_out: dict[str, dict] = {}
                for name, frames in variants_frames.items():
                    row_out[name] = await _run_variant(frames, field, rows, policy, settings, name)
                results[fid] = row_out
                print(f"\n== {fid} ({len(decoded)} frames) ==")
                print(
                    f"{'variant':<10} {'cands':>6} {'flagged':>8} {'snr_max':>8} "
                    f"{'snr_med':>8} {'snr_p90':>8} {'t(s)':>7}"
                )
                for name, m in row_out.items():
                    print(
                        f"{name:<10} {m['candidates']:>6} {m['static_flagged']:>8} "
                        f"{(m['snr_max'] or 0):>8.1f} {(m['snr_median'] or 0):>8.1f} "
                        f"{(m['snr_p90'] or 0):>8.1f} {m['runtime_s']:>7}"
                    )
        finally:
            await db.close()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
