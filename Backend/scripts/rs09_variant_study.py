#!/usr/bin/env python3
"""RS.09 / D-24 variant study: background & static-source handling.

Purpose: Measure the four D-24 preprocessing variants on a synthetic
injection ensemble and report recall vs spurious candidates, so the
variant choice is backed by numbers rather than the provisional default.

Variants (``background_variant``):
    a — no preprocessing
    b — smooth-background subtraction (large-median ZODI stand-in)
    c — per-frame 15 px median high-pass (current default)
    d — c + static-source masking (compact peaks on the median coadd)

Method: for each fixture field, run the same functions the
``moving_object_search`` handler calls — ``_background_frames``,
``_frame_detection_masks``, ``_mask_static_sources`` (d only),
``_default_motion_vectors``, ``shiftstack.search`` and
``_extract_candidates`` — directly, once per variant, then re-extract at
several sigma thresholds from the same score maps. This measures the
science path without job/DB overhead and makes the threshold sweep cheap.
Persistence is covered separately by the handler tests.

Truth is built from sky coordinates (``SkyCoord.directional_offset_by`` +
``world_to_pixel``), independent of the handler's pixel<->sky conversion.
Matching is in reported units: candidate position <= 2.5 px of the
injected start position at the reference epoch AND reported sky motion
(mu_e/mu_n arcsec/day) within 0.75 arcsec/day per axis of truth AND the
candidate is not static_source-flagged.

Outputs: stdout tables + ``out/rs09_variant_study.json``.
Called by: ``uv run python scripts/rs09_variant_study.py``.

PROVISIONAL(G1): synthetic data only; informs D-24, not a validation
result. Real ZODI (variant B) and real fields remain for RS.08/VAL.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

from orbitalis.research import search as sm
from orbitalis.research.policy import load_stack_policy
from orbitalis.settings import Settings
from orbitalis_core.shiftstack import Frame
from orbitalis_core.shiftstack import search as core_search

# -- ensemble parameters (PROVISIONAL(G1): study fixture, not science values)
N_FIELDS = 8
N_CONTROL = 4  # no-mover fields: pure noise + static-star false-alarm rate
N_LONG_FIELDS = 4  # 7-day baseline cohort: fast movers reach >2 FWHM
N_LONG_CONTROL = 2
N_PURE_CONTROL = 4  # no stars, no movers: isolates the noise false-alarm rate
FIELD_N = 48
RA, DEC = 150.0, 2.0
SCALE_ARCSEC = 6.15
TIMES = tuple(60000.0 + i for i in range(5))
BASELINE_DAYS = TIMES[-1] - TIMES[0]
# 5 epochs spaced 1.75 d -> 7-day baseline (policy max_time_span_days).
TIMES_LONG = tuple(60000.0 + 1.75 * i for i in range(5))
BASELINE_LONG_DAYS = TIMES_LONG[-1] - TIMES_LONG[0]
VARIANCE = 0.04
NOISE_SIGMA = math.sqrt(VARIANCE)
N_STATIC = 4
N_MOVERS = 4
MOVER_AMPS = (1.5, 4.0, 9.0, 4.0)
# Speeds in arcsec/day: two slow (~PSF-scale drift) and two fast movers.
MOVER_SPEEDS = (3.0, 7.5, 5.0, 8.0)  # within the D-17 ~8 arcsec/day prior
STATIC_AMP = 6.0
PSF_SIGMA_PX = 1.2
FWHM_PX = 2.0 * math.sqrt(2.0 * math.log(2.0)) * PSF_SIGMA_PX
POS_TOL_PX = 2.5
MOTION_TOL_ARCSEC_DAY = 0.75  # same tolerance as the RS.07 exit test
NEAR_PX = 3.0  # unmatched-candidate classification radius
STAR_MATCH_PX = 2.5
PASS_PX = 3.0  # winning-track pass-through distance that counts as a star leak
SIGMA_SWEEP = (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 14.0, 20.0, 30.0, 45.0, 60.0, 90.0)
# Perturbed cohort uses a reduced sweep: the decision is at the shipped
# operating point plus a little headroom, not the full frontier.
SIGMAS_SWEEP_PERT = (4.0, 5.0, 6.0, 8.0, 10.0)
MIN_STAR_SEP_PX = 6.0
VARIANTS = ("a", "b", "c", "d", "e", "f")
PERTURBED_FIELDS = 4  # epoch-perturbed mover cohort (realistic SPHEREx sky)
PERTURBED_CONTROL = 2  # epoch-perturbed stars-only controls
MIN_COVERAGE = 0.5
WORK_BUDGET = 3e8
SEED_BASE = int(sys.argv[1]) if len(sys.argv) > 1 else 20261111
TRACK_TOL_PX = 1.0  # max per-epoch position error for track recall


def _grid_wcs() -> WCS:
    scale = SCALE_ARCSEC / 3600.0
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crpix = [FIELD_N / 2 + 0.5, FIELD_N / 2 + 0.5]
    w.wcs.crval = [RA, DEC]
    w.wcs.cdelt = [-scale, scale]
    return w


def _psf(xx, yy, x, y, amp, sigma=PSF_SIGMA_PX):
    return amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2.0 * sigma**2))


def _jitter(rng: np.random.Generator, lo: float, hi: float) -> tuple[float, float]:
    """Random sub-pixel offset with magnitude uniform in [lo, hi] px."""
    r = float(rng.uniform(lo, hi))
    a = float(rng.uniform(0.0, 2.0 * math.pi))
    return r * math.cos(a), r * math.sin(a)


def _make_field(
    rng: np.random.Generator,
    wcs: WCS,
    *,
    with_movers: bool,
    with_statics: bool = True,
    times=TIMES,
    perturbed: bool = False,
):
    """One fixture field. Returns (decoded_frames, truth_movers, statics).

    Truth positions/motion come from sky offsets + world_to_pixel, never
    from the handler's conversion helpers.
    """
    yy, xx = np.mgrid[0:FIELD_N, 0:FIELD_N].astype(np.float64)
    bkg = 1.0 + 0.8 * np.sin(xx / 18.0) * np.cos(yy / 22.0) + 0.4 * (xx / FIELD_N)

    statics = []
    for _ in range(N_STATIC if with_statics else 0):
        for _try in range(100):
            x = float(rng.uniform(6, FIELD_N - 7))
            y = float(rng.uniform(6, FIELD_N - 7))
            if all(math.hypot(x - sx, y - sy) >= 5.0 for sx, sy in statics):
                statics.append((x, y))
                break

    baseline = times[-1] - times[0]
    movers = []
    if with_movers:
        ref = SkyCoord(ra=RA, dec=DEC, unit="deg")
        for i in range(N_MOVERS):
            amp = MOVER_AMPS[i % len(MOVER_AMPS)]
            speed = MOVER_SPEEDS[i % len(MOVER_SPEEDS)]
            ang = float(rng.uniform(0, 2 * math.pi))
            mu_e = speed * math.sin(ang)
            mu_n = speed * math.cos(ang)

            # Sky-based truth: offset the reference coordinate and convert
            # with world_to_pixel at two epochs to get the pixel velocity.
            def _pix(dt, mu_e=mu_e, mu_n=mu_n, ref=ref):
                sep = math.hypot(mu_e, mu_n) * dt
                pa = math.degrees(math.atan2(mu_e, mu_n))
                c = ref.directional_offset_by(pa * u.deg, sep * u.arcsec)
                return wcs.world_to_pixel(c)

            p0 = np.asarray(_pix(0.0), dtype=float)
            p1 = np.asarray(_pix(1.0), dtype=float)
            vx, vy = float(p1[0] - p0[0]), float(p1[1] - p0[1])
            # Reject placements whose track leaves the frame or passes
            # within MIN_STAR_SEP_PX of a static star.
            placed = False
            for _try in range(200):
                x = float(rng.uniform(8, FIELD_N - 9))
                y = float(rng.uniform(8, FIELD_N - 9))
                ok = True
                for k in range(9):
                    dt = baseline * k / 8.0
                    px, py = x + vx * dt, y + vy * dt
                    if not (4.0 <= px <= FIELD_N - 5 and 4.0 <= py <= FIELD_N - 5):
                        ok = False
                        break
                    if any(math.hypot(px - sx, py - sy) < MIN_STAR_SEP_PX for sx, sy in statics):
                        ok = False
                        break
                if ok:
                    placed = True
                    break
            if not placed:
                continue
            movers.append(
                {
                    "x0": x,
                    "y0": y,
                    "vx": vx,
                    "vy": vy,
                    "mu_e": mu_e,
                    "mu_n": mu_n,
                    "amp": amp,
                    "speed_arcsec_day": speed,
                    "displacement_fwhm": math.hypot(vx, vy) * baseline / FWHM_PX,
                }
            )

    frames = []
    for i, t in enumerate(times):
        dt = t - TIMES[0]
        if perturbed:
            # Realistic epoch-to-epoch sky variation (RS.09 review): per-frame
            # background offset + few-percent gradient change, per-epoch PSF
            # width variation, sub-pixel position jitter on every source, and
            # 10-20% flux scatter on the static stars (wavelength/detector/
            # regrid effects the clean fixture cannot produce).
            img = (
                bkg * (1.0 + float(rng.uniform(-0.05, 0.05)))
                + float(rng.uniform(-0.08, 0.08))
                + rng.normal(0.0, NOISE_SIGMA, size=(FIELD_N, FIELD_N))
            )
            psf_sig = PSF_SIGMA_PX * float(rng.uniform(0.9, 1.1))
            for x, y in statics:
                jx, jy = _jitter(rng, 0.1, 0.3)
                flux = STATIC_AMP * float(rng.uniform(0.8, 1.2))
                img = img + _psf(xx, yy, x + jx, y + jy, flux, psf_sig)
            for m in movers:
                jx, jy = _jitter(rng, 0.1, 0.3)
                img = img + _psf(
                    xx,
                    yy,
                    m["x0"] + m["vx"] * dt + jx,
                    m["y0"] + m["vy"] * dt + jy,
                    m["amp"],
                    psf_sig,
                )
        else:
            img = bkg + rng.normal(0.0, NOISE_SIGMA, size=(FIELD_N, FIELD_N))
            for x, y in statics:
                img = img + _psf(xx, yy, x, y, STATIC_AMP)
            for m in movers:
                img = img + _psf(xx, yy, m["x0"] + m["vx"] * dt, m["y0"] + m["vy"] * dt, m["amp"])
        var = np.full((FIELD_N, FIELD_N), VARIANCE, dtype=np.float32)
        frames.append(
            Frame(
                image=img.astype(np.float64),
                variance=var.astype(np.float64),
                time=float(t),
                frame_id=f"f{i}",
            )
        )
    return frames, movers, statics


def _track_passes_near_source(c, statics, times):
    """Min distance from the candidate's winning track to any static star
    over the epochs — the one-epoch star pass-through signature."""
    t0 = times[0]
    best = 1e9
    for t in times:
        dt = t - t0
        px, py = c["x"] + c["vx"] * dt, c["y"] + c["vy"] * dt
        for sx, sy in statics:
            best = min(best, math.hypot(px - sx, py - sy))
    return best


def _run_variant(frames, variant, wcs, policy, baseline_days=BASELINE_DAYS):
    """Mirror the handler's science path; return per-sigma candidate lists."""
    decoded = [
        sm.DecodedFrame(
            frame_ref=f.frame_id,
            image=np.asarray(f.image, dtype=np.float64),
            variance=np.asarray(f.variance, dtype=np.float64),
            mask=np.isfinite(f.image),
            flags=None,
            mjd=float(f.time),
            detector=1,
            wavelength_um=1.0 + 0.05 * i,
            meta={},
        )
        for i, f in enumerate(frames)
    ]
    core = sm._background_frames(variant, decoded)
    # Mirror the handler: static-source evidence always comes from the
    # C-style (15 px high-pass) frames so the flag stays an independent
    # safety net under difference-imaging variants.
    evidence = core if variant in ("c", "d") else sm._background_frames("c", decoded)
    detection_masks, _ = sm._frame_detection_masks(
        evidence, sigma_threshold=sm.STATIC_SOURCE_INDIVIDUAL_SIGMA
    )
    if variant == "d":
        core = sm._mask_static_sources(core)
    cx, cy = (FIELD_N - 1) / 2.0, (FIELD_N - 1) / 2.0
    vectors = sm._default_motion_vectors(wcs, cx, cy, policy)
    res = core_search(
        core,
        vectors,
        min_coverage_fraction=MIN_COVERAGE,
        work_budget=int(WORK_BUDGET),
        return_trial_maps=False,
    )
    zero_idx = sm._find_zero_motion_trial_index(vectors)
    min_cov_count = math.ceil(MIN_COVERAGE * len(core))
    out = {}
    for sig in SIGMA_SWEEP:
        ext, _method, _diag = sm._extract_candidates(
            res,
            detection_threshold_sigma=sig,
            nms_radius_px=sm.EXTRACTION_NMS_RADIUS_PX,
            edge_margin_px=sm.EXTRACTION_EDGE_MARGIN_PX,
            min_coverage_count=min_cov_count,
        )
        cands = []
        for c in ext:
            mv = res.motion_vectors[int(c["trial_index"])]
            x_pos = float(c.get("x_centroid", c["x"]))
            y_pos = float(c.get("y_centroid", c["y"]))
            mu_e, mu_n = sm._pixel_per_day_to_mu_east_north(
                mv.dx_px_per_day, mv.dy_px_per_day, wcs, x_pos, y_pos
            )
            disp = math.hypot(mv.dx_px_per_day, mv.dy_px_per_day) * baseline_days
            ev = sm._static_source_evidence(
                x=int(c["x"]),
                y=int(c["y"]),
                zero_trial_index=zero_idx,
                best_trial_index=int(c["trial_index"]),
                detection_masks=detection_masks,
                winning_displacement_px=disp,
                baseline_days=baseline_days,
            )
            cands.append(
                {
                    "x": x_pos,
                    "y": y_pos,
                    "vx": mv.dx_px_per_day,
                    "vy": mv.dy_px_per_day,
                    "snr": float(c["stack_snr"]),
                    "mu_e": mu_e,
                    "mu_n": mu_n,
                    "static_flagged": bool(ev["flagged_static_source"]),
                    "winning_disp_px": disp,
                }
            )
        out[sig] = cands
    return out


def _track_error_px(c, m, times):
    """Max per-epoch |predicted - true| position error over the baseline."""
    t0 = times[0]
    err = 0.0
    for t in times:
        dt = t - t0
        px = c["x"] + c["vx"] * dt
        py = c["y"] + c["vy"] * dt
        tx = m["x0"] + m["vx"] * dt
        ty = m["y0"] + m["vy"] * dt
        err = max(err, math.hypot(px - tx, py - ty))
    return err


def _match(cands, truth, times=TIMES):
    """Match candidates to truth movers.

    Returns (strict, detect, track, used, track_errs). *detect* = unflagged
    candidate within POS_TOL_PX of the injected start position; *strict*
    additionally requires reported sky motion within MOTION_TOL_ARCSEC_DAY
    per axis; *track* requires the candidate's winning velocity to keep every
    epoch's predicted position within TRACK_TOL_PX of truth — the physically
    meaningful criterion for cutout predictions. Motion-rate tolerance is
    reported separately because the argmax trial scatters several grid steps
    on bright sources (flat score surface, position/velocity degeneracy).
    """
    used = set()
    strict = set()
    detect = set()
    track = set()
    track_errs = {}
    for ti, m in enumerate(truth):
        best = None
        for ci, c in enumerate(cands):
            if ci in used or c["static_flagged"]:
                continue
            d = math.hypot(c["x"] - m["x0"], c["y"] - m["y0"])
            if d > POS_TOL_PX:
                continue
            if best is None or d < best[0]:
                best = (d, ci, c)
        if best is not None:
            _d, ci, c = best
            used.add(ci)
            detect.add(ti)
            terr = _track_error_px(c, m, times)
            track_errs[ti] = terr
            if terr <= TRACK_TOL_PX:
                track.add(ti)
            if (
                abs(c["mu_e"] - m["mu_e"]) <= MOTION_TOL_ARCSEC_DAY
                and abs(c["mu_n"] - m["mu_n"]) <= MOTION_TOL_ARCSEC_DAY
            ):
                strict.add(ti)
    return strict, detect, track, used, track_errs


def _classify_unmatched(cands, used, truth, statics):
    """Split unmatched candidates into near-static / near-mover / elsewhere."""
    near_static = near_mover = elsewhere = 0
    for ci, c in enumerate(cands):
        if ci in used:
            continue
        d_star = min((math.hypot(c["x"] - sx, c["y"] - sy) for sx, sy in statics), default=1e9)
        d_mov = min((math.hypot(c["x"] - m["x0"], c["y"] - m["y0"]) for m in truth), default=1e9)
        if d_star <= NEAR_PX:
            near_static += 1
        elif d_mov <= NEAR_PX:
            near_mover += 1
        else:
            elsewhere += 1
    return near_static, near_mover, elsewhere


def _star_candidate_counts(cands, statics):
    """Candidates within STAR_MATCH_PX of a static star: (flagged, unflagged)."""
    flagged = unflagged = 0
    for c in cands:
        if any(math.hypot(c["x"] - sx, c["y"] - sy) <= STAR_MATCH_PX for sx, sy in statics):
            if c["static_flagged"]:
                flagged += 1
            else:
                unflagged += 1
    return flagged, unflagged


def _interp_recall_at(points, target_unmatched):
    """Interpolate recall at a target unmatched/field along the sigma sweep.

    points: list of (unmatched_per_field, recall) sorted by unmatched.
    Returns None if target is outside the range.
    """
    pts = sorted(set(points))
    if not pts or target_unmatched < pts[0][0] or target_unmatched > pts[-1][0]:
        return None
    for (u0, r0), (u1, r1) in zip(pts, pts[1:], strict=False):
        if u0 <= target_unmatched <= u1:
            if u1 == u0:
                return r1
            f = (target_unmatched - u0) / (u1 - u0)
            return r0 + f * (r1 - r0)
    return pts[-1][1]


def main() -> int:
    wcs = _grid_wcs()
    policy = load_stack_policy(sm._resolve_stack_policy_path(Settings()))

    # Per (variant, sigma): accumulators over mover fields and control fields.
    agg = {
        v: {
            sig: {
                "rec": 0,
                "rec_strict": 0,
                "rec_track": 0,
                "truth": 0,
                "unmatched": 0,
                "near_static": 0,
                "near_mover": 0,
                "elsewhere": 0,
                "unflagged_unmatched": 0,
                "star_flag": 0,
                "star_unflag": 0,
                "fields": 0,
            }
            for sig in SIGMA_SWEEP
        }
        for v in VARIANTS
    }
    ctrl = {
        v: {
            sig: {
                cohort: {
                    "unmatched": 0,
                    "unflagged": 0,
                    "near_static": 0,
                    "near_mover": 0,
                    "elsewhere": 0,
                    "fields": 0,
                }
                for cohort in ("std", "long", "pure")
            }
            for sig in SIGMA_SWEEP
        }
        for v in VARIANTS
    }
    # Star pass-through diagnostic on stars-only control fields:
    # (variant, sig) -> [cands whose winning track passes <=PASS_PX of a
    # static star at some epoch, total unmatched candidates].
    passthrough = {}
    # Paired per-mover recovery at sigma=5.0 (the shipped default).
    # Keys carry the cohort: std (4 d) and long (7 d) fields reuse fidx.
    mover_keys = []
    paired = {}  # (cohort, field_idx, mover_idx) -> {variant: bool}
    missed_detail = []

    jobs = (
        [(f, True, TIMES, "std") for f in range(N_FIELDS)]
        + [(f, False, TIMES, "std") for f in range(N_CONTROL)]
        + [(f, True, TIMES_LONG, "long") for f in range(N_LONG_FIELDS)]
        + [(f, False, TIMES_LONG, "long") for f in range(N_LONG_CONTROL)]
        + [(f, False, TIMES, "pure") for f in range(N_PURE_CONTROL)]
    )
    for fidx, with_movers, times, cohort in jobs:
        rng = np.random.default_rng(
            SEED_BASE
            + fidx * 7
            + (1000 if cohort == "long" else 0)
            + (77 if cohort == "pure" else 0)
        )
        frames, truth, statics = _make_field(
            rng, wcs, with_movers=with_movers, with_statics=cohort != "pure", times=times
        )
        for variant in VARIANTS:
            per_sig = _run_variant(frames, variant, wcs, policy, baseline_days=times[-1] - times[0])
            for sig, cands in per_sig.items():
                rec_strict, rec_detect, rec_track, used, _terr = _match(cands, truth, times=times)
                ns, nm, ne = _classify_unmatched(cands, used, truth, statics)
                sf, su = _star_candidate_counts(cands, statics)
                if with_movers:
                    a = agg[variant][sig]
                    a["rec"] += len(rec_detect)
                    a["rec_strict"] += len(rec_strict)
                    a["rec_track"] += len(rec_track)
                    a["truth"] += len(truth)
                    a["unmatched"] += len(cands) - len(used)
                    a["unflagged_unmatched"] += sum(
                        1
                        for ci, c in enumerate(cands)
                        if ci not in used and not c["static_flagged"]
                    )
                    a["near_static"] += ns
                    a["near_mover"] += nm
                    a["elsewhere"] += ne
                    a["star_flag"] += sf
                    a["star_unflag"] += su
                    a["fields"] += 1
                else:
                    c = ctrl[variant][sig][cohort]
                    c["unmatched"] += len(cands)
                    c["unflagged"] += sum(1 for cd in cands if not cd["static_flagged"])
                    c["near_static"] += ns
                    c["near_mover"] += nm
                    c["elsewhere"] += ne
                    c["fields"] += 1
                    if statics:
                        pt = passthrough.setdefault((variant, sig), [0, 0])
                        for cd in cands:
                            pt[1] += 1
                            if _track_passes_near_source(cd, statics, times) <= PASS_PX:
                                pt[0] += 1
            # paired recovery at default sigma
            sig5 = per_sig[5.0]
            _rs, rec5, _rt, _u, _te = _match(sig5, truth, times=times)
            if with_movers:
                for ti, m in enumerate(truth):
                    key = (cohort, fidx, ti)
                    if key not in paired:
                        paired[key] = {}
                        mover_keys.append((key, m))
                    paired[key][variant] = ti in rec5
                    if variant == "c" and ti not in rec5:
                        missed_detail.append(
                            {
                                "cohort": cohort,
                                "field": fidx,
                                "mover": ti,
                                "amp": m["amp"],
                                "speed": m["speed_arcsec_day"],
                                "dispfwhm": round(m["displacement_fwhm"], 2),
                                "pos": [round(m["x0"], 1), round(m["y0"], 1)],
                            }
                        )
        tag = f"{cohort}-{'movers' if with_movers else 'control'}"
        print(
            f"field {fidx} ({tag}): truth={len(truth)} baseline="
            f"{times[-1] - times[0]:.1f}d variants done",
            flush=True,
        )

    # ---- perturbed cohort: realistic epoch-varying sky (RS.09 review) ----
    # Per-epoch star flux scatter 10-20%, sub-pixel jitter 0.1-0.3 px, PSF
    # width variation, per-frame background offset + gradient change. This is
    # the cohort that decides whether E stays the default: a perfectly static
    # sky cannot produce the residual-star leak E was built to remove.
    pert_mover = {
        v: {
            sig: {
                "rec": 0,
                "rec_strict": 0,
                "rec_track": 0,
                "truth": 0,
                "unflagged_unmatched": 0,
                "fields": 0,
            }
            for sig in SIGMAS_SWEEP_PERT
        }
        for v in VARIANTS
    }
    pert_ctrl = {
        v: {sig: {"unmatched": 0, "unflagged": 0, "fields": 0} for sig in SIGMAS_SWEEP_PERT}
        for v in VARIANTS
    }
    for fidx in range(PERTURBED_FIELDS + PERTURBED_CONTROL):
        with_movers = fidx < PERTURBED_FIELDS
        rng = np.random.default_rng(SEED_BASE + 5000 + fidx * 11)
        frames, truth, statics = _make_field(rng, wcs, with_movers=with_movers, perturbed=True)
        for variant in VARIANTS:
            per_sig = _run_variant(frames, variant, wcs, policy)
            for sig in SIGMAS_SWEEP_PERT:
                cands = per_sig[sig]
                rec_s, rec_d, rec_t, used, _te = _match(cands, truth)
                if with_movers:
                    a = pert_mover[variant][sig]
                    a["rec"] += len(rec_d)
                    a["rec_strict"] += len(rec_s)
                    a["rec_track"] += len(rec_t)
                    a["truth"] += len(truth)
                    a["unflagged_unmatched"] += sum(
                        1
                        for ci, c in enumerate(cands)
                        if ci not in used and not c["static_flagged"]
                    )
                    a["fields"] += 1
                else:
                    c = pert_ctrl[variant][sig]
                    c["unmatched"] += len(cands)
                    c["unflagged"] += sum(1 for cd in cands if not cd["static_flagged"])
                    c["fields"] += 1
        print(
            f"perturbed field {fidx} ({'movers' if with_movers else 'control'}): "
            f"truth={len(truth)} variants done",
            flush=True,
        )

    print("\n-- PERTURBED cohort (epoch-varying sky): recall / unflagged-unmatched per field --")
    print(f"{'var':<5} {'sig':>4} {'recall':>15} {'unfl/f':>7}")
    for v in VARIANTS:
        for sig in SIGMAS_SWEEP_PERT:
            a = pert_mover[v][sig]
            nf = a["fields"] or 1
            recall = a["rec"] / a["truth"] if a["truth"] else 0.0
            recall_s = a["rec_strict"] / a["truth"] if a["truth"] else 0.0
            recall_t = a["rec_track"] / a["truth"] if a["truth"] else 0.0
            print(
                f"{v:<5} {sig:>4} {recall:>7.3f}({recall_s:.3f}|{recall_t:.3f}) "
                f"{a['unflagged_unmatched'] / nf:>7.2f}"
            )
    print("-- PERTURBED controls (stars only, epoch-varying): unmatched(unflagged)/field --")
    for v in VARIANTS:
        cells = [
            f"{sig}:{pert_ctrl[v][sig]['unmatched'] / max(pert_ctrl[v][sig]['fields'], 1):.1f}"
            f"({pert_ctrl[v][sig]['unflagged'] / max(pert_ctrl[v][sig]['fields'], 1):.1f}u)"
            for sig in SIGMAS_SWEEP_PERT
        ]
        print(f"  {v}: {' '.join(cells)}")

    # ---- report ----
    print("\nRS.09 / D-24 variant study (synthetic, PROVISIONAL(G1))")
    print(
        f"mover fields={N_FIELDS} std + {N_LONG_FIELDS} long ({N_MOVERS} movers each) "
        f"control fields={N_CONTROL} std + {N_LONG_CONTROL} long statics/field={N_STATIC}"
    )
    print(
        f"FWHM={FWHM_PX:.2f}px match: pos<={POS_TOL_PX}px "
        f"motion<={MOTION_TOL_ARCSEC_DAY} arcsec/day unflagged"
    )

    print("\n-- sigma sweep: recall / unmatched-per-field (mover fields) --")
    print(
        f"{'var':<5} {'sig':>4} {'recall':>15} {'unm/f':>7} {'unfl/f':>7} "
        f"{'nearSta':>8} {'nearMov':>8} {'elsew':>6} {'starF':>6} {'starU':>6}"
    )
    print(
        "   recall shown as detect(strict-motion<=0.75|track<=1px); "
        "unm/f=all unmatched, unfl/f=UNFLAGGED (static-flagged excluded)"
    )
    curves = {}
    for v in VARIANTS:
        curves[v] = []
        for sig in SIGMA_SWEEP:
            a = agg[v][sig]
            nf = a["fields"] or 1
            recall = a["rec"] / a["truth"] if a["truth"] else 0.0
            recall_s = a["rec_strict"] / a["truth"] if a["truth"] else 0.0
            recall_t = a["rec_track"] / a["truth"] if a["truth"] else 0.0
            unm_all = a["unmatched"] / nf
            unm = a["unflagged_unmatched"] / nf
            curves[v].append((unm, recall))
            print(
                f"{v:<5} {sig:>4} {recall:>7.3f}({recall_s:.3f}|{recall_t:.3f}) "
                f"{unm_all:>7.2f} "
                f"{unm:>7.2f} "
                f"{a['near_static'] / nf:>8.2f} {a['near_mover'] / nf:>8.2f} "
                f"{a['elsewhere'] / nf:>6.2f} {a['star_flag'] / nf:>6.2f} "
                f"{a['star_unflag'] / nf:>6.2f}"
            )

    print("\n-- control fields (no movers): unmatched per field = false-alarm rate --")
    for v in VARIANTS:
        for cohort in ("std", "long", "pure"):
            cells = []
            for sig in SIGMA_SWEEP:
                cc = ctrl[v][sig][cohort]
                nf = max(cc["fields"], 1)
                cells.append(f"{sig}:{cc['unmatched'] / nf:.1f}({cc['unflagged'] / nf:.1f}u)")
            row = " ".join(cells)
            print(f"  {v} {cohort}: {row}   (total(unflagged) per field)")

    print(
        "\n-- star pass-through test (stars-only controls): candidates whose "
        "winning track passes <=3px of a static star at ANY epoch --"
    )
    for v in VARIANTS:
        row = " ".join(
            f"{sig}:{passthrough.get((v, sig), [0, 0])[0]}/{passthrough.get((v, sig), [0, 0])[1]}"
            for sig in (3.0, 5.0, 10.0, 30.0)
        )
        print(f"  {v}: {row}   (pass/total unmatched candidates)")

    print("\n-- recall at matched unmatched-per-field (interpolated; -- = out of range) --")
    targets = sorted({round(u) for v in VARIANTS for u, _ in curves[v]})
    print(f"{'unm/f':>6}" + "".join(f"{v:>8}" for v in VARIANTS))
    for t in targets:
        cells = []
        for v in VARIANTS:
            r = _interp_recall_at(curves[v], float(t))
            cells.append(f"{r:>8.3f}" if r is not None else f"{'--':>8}")
        print(f"{t:>6}" + "".join(cells))

    # paired analysis at sigma=5
    print("\n-- paired per-mover detection recall at sigma=5.0 --")
    print(f"{'variant':<8} {'recovered':>18}")
    for v in VARIANTS:
        n_rec = sum(1 for p in paired.values() if p.get(v))
        print(f"{v:<8} {n_rec:>6}/{len(paired)}")
    print("\nMcNemar-style pairs vs C (b=only-C, c=only-other):")
    for v in ("a", "b", "d"):
        b = sum(1 for p in paired.values() if p.get("c") and not p.get(v))
        c = sum(1 for p in paired.values() if not p.get("c") and p.get(v))
        print(f"  C vs {v}: C-only={b} {v}-only={c}")

    # stratified recall at sigma=5
    print("\n-- stratified DETECTION recall at sigma=5.0 --")
    for label, pred in (
        ("amp=1.5", lambda m: m["amp"] == 1.5),
        ("amp=4.0", lambda m: m["amp"] == 4.0),
        ("amp=9.0", lambda m: m["amp"] == 9.0),
        ("disp<1 FWHM", lambda m: m["displacement_fwhm"] < 1.0),
        ("1-2 FWHM", lambda m: 1.0 <= m["displacement_fwhm"] < 2.0),
        (">=2 FWHM", lambda m: m["displacement_fwhm"] >= 2.0),
    ):
        row = f"{label:<12}"
        for v in VARIANTS:
            keys = [k for k, m in mover_keys if pred(m)]
            rec = sum(1 for k in keys if paired.get(k, {}).get(v))
            row += f" {v}:{rec}/{len(keys)}"
        print(row)

    if missed_detail:
        print("\n-- movers missed by variant C at sigma=5 (diagnostics) --")
        for d in missed_detail:
            print(
                f"  {d['cohort']}-field{d['field']} m{d['mover']} amp={d['amp']} "
                f'speed={d["speed"]}"/d disp={d["dispfwhm"]}FWHM pos={d["pos"]}'
            )

    out_dir = Path("out")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "rs09_variant_study.json"
    out.write_text(
        json.dumps(
            {
                "params": {
                    "n_fields": N_FIELDS,
                    "n_control": N_CONTROL,
                    "n_long_fields": N_LONG_FIELDS,
                    "n_long_control": N_LONG_CONTROL,
                    "n_pure_control": N_PURE_CONTROL,
                    "n_perturbed_fields": PERTURBED_FIELDS,
                    "n_perturbed_control": PERTURBED_CONTROL,
                    "pass_px": PASS_PX,
                    "baseline_days": BASELINE_DAYS,
                    "baseline_days_long": BASELINE_LONG_DAYS,
                    "seed_base": SEED_BASE,
                    "track_tol_px": TRACK_TOL_PX,
                    "n_movers": N_MOVERS,
                    "pos_tol_px": POS_TOL_PX,
                    "motion_tol_arcsec_day": MOTION_TOL_ARCSEC_DAY,
                    "sigma_sweep": list(SIGMA_SWEEP),
                    "note": (
                        "PROVISIONAL(G1) synthetic study; informs D-24, not a validation result"
                    ),
                },
                "mover_fields": {
                    v: {
                        str(sig): {
                            "recall_detect": (
                                (a := agg[v][sig])["rec"] / a["truth"] if a["truth"] else 0
                            ),
                            "recall_strict": a["rec_strict"] / a["truth"] if a["truth"] else 0,
                            "recall_track": a["rec_track"] / a["truth"] if a["truth"] else 0,
                            "unmatched_per_field": a["unmatched"] / a["fields"],
                            "unflagged_unmatched_per_field": (
                                a["unflagged_unmatched"] / a["fields"]
                            ),
                            "near_static_per_field": a["near_static"] / a["fields"],
                            "near_mover_per_field": a["near_mover"] / a["fields"],
                            "elsewhere_per_field": a["elsewhere"] / a["fields"],
                            "star_flagged_per_field": a["star_flag"] / a["fields"],
                            "star_unflagged_per_field": a["star_unflag"] / a["fields"],
                        }
                        for sig in SIGMA_SWEEP
                    }
                    for v in VARIANTS
                },
                "control_fields": {
                    v: {
                        str(sig): {
                            cohort: {
                                "unmatched_per_field": ctrl[v][sig][cohort]["unmatched"]
                                / max(ctrl[v][sig][cohort]["fields"], 1),
                                "unflagged_per_field": (
                                    ctrl[v][sig][cohort]["unflagged"]
                                    / max(ctrl[v][sig][cohort]["fields"], 1)
                                ),
                            }
                            for cohort in ("std", "long", "pure")
                        }
                        for sig in SIGMA_SWEEP
                    }
                    for v in VARIANTS
                },
                "star_passthrough": {
                    f"{v}|{sig}": {"pass": p[0], "total": p[1]}
                    for (v, sig), p in sorted(passthrough.items())
                },
                "paired_sigma5": {
                    "recovered": {v: sum(1 for p in paired.values() if p.get(v)) for v in VARIANTS},
                    "total_movers": len(paired),
                },
                "perturbed": {
                    "mover_fields": PERTURBED_FIELDS,
                    "control_fields": PERTURBED_CONTROL,
                    "description": (
                        "epoch-varying sky: star flux scatter 10-20%, sub-pixel "
                        "jitter 0.1-0.3px, PSF width +/-10%, per-frame background "
                        "offset +/-0.08 + few-percent scale change"
                    ),
                    "movers": {
                        v: {
                            str(sig): {
                                "recall_detect": (
                                    pert_mover[v][sig]["rec"] / pert_mover[v][sig]["truth"]
                                    if pert_mover[v][sig]["truth"]
                                    else 0
                                ),
                                "recall_strict": (
                                    pert_mover[v][sig]["rec_strict"] / pert_mover[v][sig]["truth"]
                                    if pert_mover[v][sig]["truth"]
                                    else 0
                                ),
                                "recall_track": (
                                    pert_mover[v][sig]["rec_track"] / pert_mover[v][sig]["truth"]
                                    if pert_mover[v][sig]["truth"]
                                    else 0
                                ),
                                "unflagged_unmatched_per_field": (
                                    pert_mover[v][sig]["unflagged_unmatched"]
                                    / max(pert_mover[v][sig]["fields"], 1)
                                ),
                            }
                            for sig in SIGMAS_SWEEP_PERT
                        }
                        for v in VARIANTS
                    },
                    "controls": {
                        v: {
                            str(sig): {
                                "unmatched_per_field": (
                                    pert_ctrl[v][sig]["unmatched"]
                                    / max(pert_ctrl[v][sig]["fields"], 1)
                                ),
                                "unflagged_per_field": (
                                    pert_ctrl[v][sig]["unflagged"]
                                    / max(pert_ctrl[v][sig]["fields"], 1)
                                ),
                            }
                            for sig in SIGMAS_SWEEP_PERT
                        }
                        for v in VARIANTS
                    },
                },
            },
            indent=2,
        )
    )
    print(f"\nwrote {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
