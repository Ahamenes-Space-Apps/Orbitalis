#!/usr/bin/env python3
"""Interpolation-kernel injection-recovery study (D-24 DSP register, exp. 1).

Purpose: decide whether the cubic shift kernel (``interpolation_order=3``)
beats the shipped bilinear default (``interpolation_order=1``) often enough
to promote. Bilinear is a 2-tap low-pass with phase-dependent response on
the ~1 px-FWHM SPHEREx PSF — the measured ~0.77x realized-peak ratio
motivates this experiment. Both arms run ``background_variant="f"`` (the
shipped default) so the kernel is the only changed variable.

PRE-REGISTERED COMPARISON BARS (fixed before looking at outcomes):

- Primary metric: pooled recall = recovered / (recovered + missed) across
  all fields x seeds, with Wilson 95% CI.
- Spurious count per arm reported alongside (never recall alone).
- PROMOTE bar: order-3 pooled recall exceeds order-1 AND order-3 spurious
  <= order-1 spurious AND order-3 recall >= order-1 recall on at least
  3 of 5 fields. Anything less keeps ``interpolation_order=1`` as default
  and the kernel stays an experiment.
- Slow-mover stratum (speed < 2 arcsec/day) reported separately — kernel
  choice must not trade away the slow regime.

Method mirrors ``dsp_injection_study.py`` / VAL.02: one injected source
per trial, ``match`` against each arm's OWN un-injected detections, truth
positions/speeds/SNR drawn identically for both arms (paired). Fresh
seeds (20261117, 20261118 — distinct from the background-variant study's
20261115/16) per the pre-registration. Fields: the same 5 real demo
fields, which include RW Aur (f_6bc1) and R Dra (f_be71).

Outputs: stdout tables + ``out/kernel_injection_study.json``.
Called by: ``uv run python scripts/kernel_injection_study.py [field_id ...]``.
PROVISIONAL(G1): small-N study; informs the D-24 register, not a
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

BUNDLE = Path(__file__).resolve().parents[1] / "demo_bundle"
OUT = Path(__file__).resolve().parents[1] / "out" / "kernel_injection_study.json"

DEFAULT_FIELDS = [
    "f_47a8f0fb315ca4961417999a",  # epsilon-indi-b
    "f_6bc163f4091713c4a85d3b90",  # rw-aur
    "f_be719d6214cce7c417ef1b88",  # r-dra
    "f_ec6ebe1a5e63a231d3d66893",  # wise-0855
    "f_25faaae4bee9c18ab8c3ce82",  # tutorial
]
N_INJ = 6  # per seed; SEEDS x N_INJ = 12 injections per field
SEEDS = (20261117, 20261118)  # fresh seeds, pre-registered
SNR_LO, SNR_HI = 8.0, 20.0  # PROVISIONAL — same range as the variant study
SPEED_LO, SPEED_HI = 0.5, 8.0  # PROVISIONAL arcsec/day
SLOW_SPEED_ARCSEC_DAY = 2.0  # stratum boundary for the slow-mover report
PSF_SIGMA_PX = 0.45  # validation default (PROVISIONAL(G1))
MATCH_RADIUS_PX = 1.5
ARMS = (1, 3)  # interpolation_order: bilinear baseline vs cubic experiment
BG_VARIANT = "f"  # shipped default; kernel is the only changed variable


async def _sci(frames, mvs, settings, order):
    return await asyncio.to_thread(
        sm.run_science_search,
        frames,
        motion_vectors=mvs,
        min_coverage_fraction=0.5,
        detection_threshold_sigma=None,
        nms_radius_px=sm.EXTRACTION_NMS_RADIUS_PX,
        background_variant=BG_VARIANT,
        work_budget=settings.search_max_work,
        interpolation_order=order,
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

    # Per-arm un-injected baseline: detections + reference epoch.
    bases: dict[int, dict] = {}
    for order in ARMS:
        sci = await _sci(decoded, mvs, settings, order)
        bases[order] = {
            "sci": sci,
            "dets": val._candidates_to_detections(sci.candidates, "b"),
            "ref": float(sci.result.reference_time),
        }
    ref_time = bases[1]["ref"]

    # Truth sampling (shared across arms so comparisons are paired).
    rng = np.random.default_rng(seed)
    psf = GaussianPSF(sigma_px=PSF_SIGMA_PX)
    margin = int(np.ceil(4.0 * PSF_SIGMA_PX)) + 1
    base_frames = val._raw_core_frames(decoded)
    frame_times = np.array([float(f.time) for f in base_frames])
    var_stack = np.stack([np.asarray(f.variance) for f in decoded])
    finite_var = var_stack[np.isfinite(var_stack) & (var_stack > 0)]
    med_var = float(np.median(finite_var))
    var_for_snr = med_var * float(bases[1]["sci"].noise_calibration)

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
    for order in ARMS:
        base = bases[order]
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
            sci = await _sci(inj_decoded, mvs, settings, order)
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
        out[f"order{order}"] = {
            "interpolation_order": order,
            "background_variant": BG_VARIANT,
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


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


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
                        f"{'arm':<8} {'inj':>4} {'rec':>4} {'miss':>5} "
                        f"{'preT':>5} {'spur':>5} {'recall':>7} {'meanC':>6}"
                    )
                    print(hdr)
                    for order in ARMS:
                        m = res[f"order{order}"]
                        print(
                            f"order{order:<3} {m['n_inj']:>4} {m['recovered']:>4} "
                            f"{m['missed']:>5} {m['preexisting_truth']:>5} "
                            f"{m['spurious']:>5} {m['recall']:>7} "
                            f"{m['mean_candidates']:>6}"
                        )
        finally:
            await db.close()

    # ---- pooled summary + pre-registered bar check -------------------------
    pooled: dict[str, dict[str, int]] = {
        f"order{o}": {"rec": 0, "miss": 0, "spur": 0} for o in ARMS
    }
    slow: dict[str, dict[str, int]] = {f"order{o}": {"rec": 0, "miss": 0} for o in ARMS}
    per_field_recall: dict[str, dict[str, float]] = {}
    for key, res in results.items():
        fid = key.split("@")[0]
        for order in ARMS:
            m = res[f"order{order}"]
            arm = f"order{order}"
            pooled[arm]["rec"] += m["recovered"]
            pooled[arm]["miss"] += m["missed"]
            pooled[arm]["spur"] += m["spurious"]
            for tr in m["trials"]:
                if tr["speed_arcsec_day"] < SLOW_SPEED_ARCSEC_DAY:
                    if tr["outcome"] == "recovered":
                        slow[arm]["rec"] += 1
                    elif tr["outcome"] == "missed":
                        slow[arm]["miss"] += 1
        per_field_recall.setdefault(fid, {})
        for order in ARMS:
            m = res[f"order{order}"]
            n = m["n_inj"]
            acc = per_field_recall[fid].setdefault(arm := f"order{order}", {"rec": 0, "n": 0})
            acc["rec"] += m["recovered"]
            acc["n"] += n - m["preexisting_truth"]
    summary = {}
    for order in ARMS:
        arm = f"order{order}"
        p = pooled[arm]
        n = p["rec"] + p["miss"]
        lo, hi = _wilson(p["rec"], n)
        summary[arm] = {
            "recovered": p["rec"],
            "missed": p["miss"],
            "n_graded": n,
            "recall": round(p["rec"] / n, 3) if n else 0.0,
            "recall_ci95": [lo, hi],
            "spurious": p["spur"],
        }
    slow_summary = {}
    for order in ARMS:
        arm = f"order{order}"
        s = slow[arm]
        n = s["rec"] + s["miss"]
        slow_summary[arm] = {
            "recovered": s["rec"],
            "missed": s["miss"],
            "n_graded": n,
            "recall": round(s["rec"] / n, 3) if n else 0.0,
        }
    field_recall = {
        fid: {arm: round(v["rec"] / v["n"], 3) if v["n"] else None for arm, v in arms.items()}
        for fid, arms in per_field_recall.items()
    }
    r1, r3 = summary["order1"], summary["order3"]
    fields_not_worse = sum(
        1
        for fid, arms in field_recall.items()
        if (arms["order3"] or 0.0) >= (arms["order1"] or 0.0)
    )
    promote = (
        r3["recall"] > r1["recall"] and r3["spurious"] <= r1["spurious"] and fields_not_worse >= 3
    )
    summary["_verdict"] = {
        "promote_order3": promote,
        "fields_not_worse": fields_not_worse,
        "bar": "order3 recall > order1 AND spurious <= order1 AND >=3/5 fields not worse",
    }
    results["_summary"] = {
        "pooled": summary,
        "slow_stratum": slow_summary,
        "per_field_recall": field_recall,
        "n_fields": len({k.split("@")[0] for k in results}),
        "n_seeds": len(SEEDS),
        "injections_per_field": N_INJ,
    }

    print("\n==== POOLED ====")
    for arm in ("order1", "order3"):
        s = summary[arm]
        print(
            f"{arm}: recall {s['recall']} CI{s['recall_ci95']} "
            f"({s['recovered']}/{s['n_graded']}), spurious {s['spurious']}"
        )
    print('slow stratum (<2"/day):', json.dumps(slow_summary))
    print("per-field recall:", json.dumps(field_recall, indent=1))
    print("verdict:", json.dumps(summary["_verdict"]))

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
