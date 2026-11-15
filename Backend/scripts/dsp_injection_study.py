#!/usr/bin/env python3
"""DSP variant injection-recovery study on real demo-bundle fields.

Purpose: the decisive experiment behind ``dsp_experiments.py`` — candidate
yield means nothing without truth. Inject synthetic movers into the real
stored frames and measure, per preprocessing variant, how many injections
are recovered vs missed and how many spurious candidates appear.

Variants (v2, post-review): matched filtering dropped — it double-broadens
the PSF and measured 1/30 recall. The shipped default ``e`` (difference
imaging) is kept as the baseline because it is what production runs.

    e      — stored frames as-is, background_variant e (shipped default)
    f      — stored frames as-is, background_variant f (e + 15px high-pass)
    a      — stored frames as-is, background_variant a (no preprocessing)
    lr+a   — rank-k low-rank temporal background subtraction, then variant a

Method mirrors ``research/validation.py`` (VAL.02/D-15): one injected
source per trial, ``match`` classifies recovered/missed/preexisting/
spurious against that variant's OWN un-injected detections (so a real
source under the variant's preprocessing is not scored spurious).

Truth sampling: positions uniform inside the PSF margin with the whole
track kept in-frame; flux drawn as peak-SNR uniform in [snr_lo, snr_hi]
converted via ``flux_for_snr`` on the baseline calibrated variance;
speed uniform in [speed_lo, speed_hi] arcsec/day down to 0.5 (slow movers
are the low-rank risk case — a near-stationary source can be absorbed
into the temporal background model), random PA — the same D-17 sky-plane
convention the search grid uses. Two seeds x N_INJ injections per field,
paired across variants.

Outputs: stdout tables + ``out/dsp_injection_study.json``.
Called by: ``uv run python scripts/dsp_injection_study.py [field_id ...]``.
PROVISIONAL(G1): small-N study; informs D-24/DSP decisions, not a
validation result.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from orbitalis.demo import load_bundle
from orbitalis.research import search as sm
from orbitalis.research import validation as val
from orbitalis.research.policy import load_stack_policy
from orbitalis.settings import Settings
from orbitalis.store import get_field, list_frames, open_database
from orbitalis_core.inject import GaussianPSF, InjectedSource, inject, match
from orbitalis_core.inject.inject import flux_for_snr
from orbitalis_core.shiftstack.motion import sky_motion_to_pixel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dsp_experiments import _lowrank_background  # noqa: E402

BUNDLE = Path(__file__).resolve().parents[1] / "demo_bundle"
OUT = Path(__file__).resolve().parents[1] / "out" / "dsp_injection_study.json"

DEFAULT_FIELDS = [
    "f_47a8f0fb315ca4961417999a",
    "f_6bc163f4091713c4a85d3b90",
    "f_be719d6214cce7c417ef1b88",
    "f_ec6ebe1a5e63a231d3d66893",
    "f_25faaae4bee9c18ab8c3ce82",
]
N_INJ = 6  # per seed; SEEDS x N_INJ = 12 injections per field
SEEDS = (20261115, 20261116)
# Peak-pixel SNR draw range. Pilot on f_25faaae4bee9 showed the stack floor
# sits between 10 and 15 on that field (noise_cal ~2.3), so 8-20 straddles it.
SNR_LO, SNR_HI = 8.0, 20.0  # PROVISIONAL
# arcsec/day. Lower bound 0.5"/day ~ 0.08 px/day ~ 0.5 px over a 6 d baseline —
# the slow-mover regime where a temporal background model can eat the source.
SPEED_LO, SPEED_HI = 0.5, 8.0  # PROVISIONAL
PSF_SIGMA_PX = 0.45  # validation default (PROVISIONAL(G1))
MATCH_RADIUS_PX = 1.5
VARIANTS = ("e", "f", "a", "lr+a")


def _variant_input(name: str, decoded: list) -> tuple[list, str]:
    """Return (frames, background_variant) for the shared science path."""
    if name == "lr+a":
        return _lowrank_background(decoded), "a"
    return decoded, name


async def _sci(frames, mvs, settings, bg_variant):
    return await asyncio.to_thread(
        sm.run_science_search,
        frames,
        motion_vectors=mvs,
        min_coverage_fraction=0.5,
        detection_threshold_sigma=None,
        nms_radius_px=sm.EXTRACTION_NMS_RADIUS_PX,
        background_variant=bg_variant,
        work_budget=settings.search_max_work,
    )


async def _study_field(db, fid: str, settings, policy, seed: int) -> dict:
    field = await get_field(db, fid)
    rows = await list_frames(db, fid)
    decoded, _ = sm._decode_stored_frames(rows)
    shape = decoded[0].image.shape
    h, w = shape
    wcs = sm._resolve_field_wcs(field, rows, shape)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    mvs = sm._default_motion_vectors(wcs, cx, cy, policy)

    # Per-variant un-injected baseline: detections + reference epoch.
    bases: dict[str, dict] = {}
    for name in VARIANTS:
        frames, bg = _variant_input(name, decoded)
        sci = await _sci(frames, mvs, settings, bg)
        bases[name] = {
            "sci": sci,
            "dets": val._candidates_to_detections(sci.candidates, "b"),
            "ref": float(sci.result.reference_time),
        }
    ref_time = bases["e"]["ref"]

    # Truth sampling (shared across variants so comparisons are paired).
    rng = np.random.default_rng(seed)
    psf = GaussianPSF(sigma_px=PSF_SIGMA_PX)
    margin = int(np.ceil(4.0 * PSF_SIGMA_PX)) + 1
    base_frames = val._raw_core_frames(decoded)
    frame_times = np.array([float(f.time) for f in base_frames])
    var_stack = np.stack([np.asarray(f.variance) for f in decoded])
    finite_var = var_stack[np.isfinite(var_stack) & (var_stack > 0)]
    med_var = float(np.median(finite_var))
    var_for_snr = med_var * float(bases["e"]["sci"].noise_calibration)

    sources: list[InjectedSource] = []
    source_meta: list[dict] = []
    n_rejects = 0
    for i in range(N_INJ):
        for _ in range(50):
            x0 = float(rng.uniform(margin, w - 1 - margin))
            y0 = float(rng.uniform(margin, h - 1 - margin))
            speed = float(rng.uniform(SPEED_LO, SPEED_HI))
            pa = float(rng.uniform(0.0, 2.0 * math.pi))
            mu_e, mu_n = speed * math.sin(pa), speed * math.cos(pa)
            vx, vy = sky_motion_to_pixel(mu_e, mu_n, wcs, (x0, y0))
            xs = x0 + vx * (frame_times - ref_time)
            ys = y0 + vy * (frame_times - ref_time)
            if (
                xs.min() >= margin
                and xs.max() <= w - 1 - margin
                and ys.min() >= margin
                and ys.max() <= h - 1 - margin
            ):
                break
            n_rejects += 1
        else:
            continue
        snr = float(rng.uniform(SNR_LO, SNR_HI))
        flux = flux_for_snr(snr, var_for_snr, psf)
        sources.append(
            InjectedSource(
                x0=x0,
                y0=y0,
                vx=float(vx),
                vy=float(vy),
                flux=float(flux),
                reference_time=ref_time,
                label=f"inj_{i}",
            )
        )
        source_meta.append(
            {
                "snr": round(snr, 2),
                "speed_arcsec_day": round(speed, 3),
                "speed_px_day": round(float(np.hypot(vx, vy)), 3),
                "x0": round(x0, 1),
                "y0": round(y0, 1),
            }
        )

    out: dict[str, dict] = {}
    for name in VARIANTS:
        base = bases[name]
        t0 = time.monotonic()
        rec = miss = pre_t = spur = 0
        cand_counts = []
        trials = []
        for src, meta in zip(sources, source_meta, strict=True):
            inj_frames = inject(base_frames, [src], psf)
            inj_decoded = [
                val._decoded_from_injected(cf, tpl)
                for cf, tpl in zip(inj_frames, decoded, strict=True)
            ]
            vframes, vbg = _variant_input(name, inj_decoded)
            sci = await _sci(vframes, mvs, settings, vbg)
            dets = val._candidates_to_detections(sci.candidates, "c")
            cand_counts.append(len(dets))
            oc = match(
                dets, [src], base["dets"], radius_px=MATCH_RADIUS_PX, reference_time=base["ref"]
            )
            outcome = oc.truth[0].outcome
            trials.append({**meta, "outcome": outcome})
            if outcome == "recovered":
                rec += 1
            elif outcome == "missed":
                miss += 1
            else:
                pre_t += 1
            spur += oc.n_spurious_detections
        out[name] = {
            "n_inj": len(sources),
            "recovered": rec,
            "missed": miss,
            "preexisting_truth": pre_t,
            "spurious": spur,
            "recall": round(rec / max(1, rec + miss), 3),
            "mean_candidates": round(float(np.mean(cand_counts)), 1) if cand_counts else 0,
            "runtime_s": round(time.monotonic() - t0, 1),
            "trials": trials,
        }
    out["_meta"] = {"n_position_rejects": n_rejects, "reference_mjd": ref_time, "seed": seed}
    return out


async def main() -> None:
    only = set(sys.argv[1:])
    wanted = only or set(DEFAULT_FIELDS)
    settings = Settings()
    policy = load_stack_policy("config/stack_policy.yaml")
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as td:
        db = await open_database(Path(td) / "bundle.db")
        try:
            await load_bundle(db, BUNDLE)
            for fid in sorted(wanted):
                for seed in SEEDS:
                    key = f"{fid}@{seed}"
                    print(f"\n== {key} ==", flush=True)
                    res = await _study_field(db, fid, settings, policy, seed)
                    results[key] = res
                    hdr = (
                        f"{'variant':<8} {'inj':>4} {'rec':>4} {'miss':>5} "
                        f"{'preT':>5} {'spur':>5} {'recall':>7} {'meanC':>6}"
                    )
                    print(hdr)
                    for name in VARIANTS:
                        m = res[name]
                        print(
                            f"{name:<8} {m['n_inj']:>4} {m['recovered']:>4} {m['missed']:>5} "
                            f"{m['preexisting_truth']:>5} {m['spurious']:>5} {m['recall']:>7} "
                            f"{m['mean_candidates']:>6}"
                        )
        finally:
            await db.close()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
