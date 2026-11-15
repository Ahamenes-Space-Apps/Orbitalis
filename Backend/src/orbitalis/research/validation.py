"""validation handler (injection and recovery, VAL.04 / FR-VAL-04 / D-15).

Purpose: Run injection-and-recovery experiments on a stored field: one
baseline search on the un-injected frames, then per trial inject one moving
PSF source into copies, run the SAME science path as ``moving_object_search``
(``run_science_search``), match detections to truth at the reference time,
and aggregate pooled metrics with Wilson intervals.

Inputs: JobContext params::

    field_id               required (or taken from the job row)
    frame_refs             optional subset of stored frames
    frame_window           optional {n_frames?, mjd_min?, mjd_max?} subset —
                           same selection as the search job (mutually
                           exclusive with frame_refs)
    n_injections           int, default 20, capped by ORB_VALIDATION_MAX_INJECTIONS
    seed                   int, default 0 (deterministic truth sampling)
    flux_min/flux_max      image-unit·pixel injection flux range, OR
    snr_min/snr_max        peak-pixel SNR range (converted via flux_for_snr
                           using the field's median variance and psf_sigma_px)
    speed_min_arcsec_per_day / speed_max_arcsec_per_day   required sky-plane
                           speed range (D-17 public units)
    position_angle_deg     optional fixed PA (0=N, 90=E); absent -> uniform
    psf_sigma_px           Gaussian PSF sigma, default 0.45 PROVISIONAL(G1),
                           capped at 8.0 px (ORB-API-001 above)
    match_radius_px        optional override; default ORB_MATCH_RADIUS_PX (1.5)
    inject_positions_px    optional [[x, y], ...] explicit start positions at
                           reference_time (len must equal n_injections);
                           the whole track must still fit inside the frame
                           margin or the job fails
    motion grid params     same keys as moving_object_search (motion_vectors,
                           rate/PA or mu_e/mu_n specs, else D-17 default grid)
    background_variant, detection_threshold_sigma, min_coverage_fraction,
    nms_radius_px          same semantics as moving_object_search
    flux_bin_edges, speed_bin_edges_arcsec_per_day   optional completeness
                           bins; defaults are linear edges over the sampled
                           range (5 flux bins, 4 speed bins per D-15).
                           Speed bins are arcsec/day per D-17.
    label                  optional experiment label

The experiment ``params`` column stores the RESOLVED parameters (defaults
filled in, including ``injection_stage="aligned_grid"`` — injection happens
on the aligned grid after regridding, so completeness excludes alignment
loss; it is not an end-to-end recovery measure, D-25). If the job is
cancelled or fails mid-run, whatever trials completed are kept and the
experiment metrics are written with ``"partial": true``.

Outputs: job result dict with experiment_id and pooled metric summary; one
``experiments`` row plus one ``experiment_trials`` row per injection.
Called by: jobs runner via HandlerRegistry (registered in main.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import asdict
from typing import Any

import numpy as np

from orbitalis.errors import APIError
from orbitalis.jobs import JobContext
from orbitalis.research import search as search_mod
from orbitalis.research.policy import enforce_combinable, load_stack_policy
from orbitalis.store import (
    add_trial,
    get_field,
    get_job,
    list_frames,
    put_experiment,
)
from orbitalis_core.errors import InvalidInputError
from orbitalis_core.inject import (
    Detection,
    GaussianPSF,
    InjectedSource,
    flux_for_snr,
    inject,
    match,
    pooled_metrics,
)
from orbitalis_core.shiftstack.models import Frame
from orbitalis_core.shiftstack.motion import sky_motion_to_pixel

log = logging.getLogger(__name__)

_DEFAULT_MATCH_RADIUS_PX = 1.5  # D-15 initial value; settings may override
_DEFAULT_N_INJECTIONS = 20
_DEFAULT_SEED = 0
# SPHEREx PSF is ~6 arcsec FWHM pixel-integrated at 6.15 arcsec/px —
# Gaussian-equivalent sigma ~0.4-0.5 px and significantly undersampled
# (Explanatory Supplement: 10x oversampled PSF products). Measured on the
# tutorial field at ~0.98 px FWHM => sigma ~0.42-0.45 px, matching the
# 6 arcsec spec. Injection is pixel-integrated (inject.GaussianPSF), so the
# SNR label maps to the realized pixel peak without the point-sampled
# phase-dependent loss. Still PROVISIONAL pending ML sign-off.
_DEFAULT_PSF_SIGMA_PX = 0.45  # TODO(ML) PROVISIONAL(G1): SPHEREx PSF width (measured ~0.98 px FWHM)
_MAX_PSF_SIGMA_PX = 8.0  # sanity cap only; real SPHEREx PSF sigma ~0.5 px
_MAX_POSITION_ATTEMPTS = 200  # reject-sampling budget per injected track
_N_FLUX_BINS = 5  # D-15
_N_SPEED_BINS = 4  # D-15


def _optional_float(params: dict[str, Any], key: str) -> float | None:
    raw = params.get(key)
    if raw is None:
        return None
    return search_mod._coerce_finite_float(raw, name=key)


def _flux_range(params: dict[str, Any]) -> tuple[str, float, float]:
    """Return (kind, lo, hi): kind is 'flux' (image units) or 'snr'."""
    has_flux = "flux_min" in params or "flux_max" in params
    has_snr = "snr_min" in params or "snr_max" in params
    if has_flux == has_snr:
        raise APIError(
            "ORB-API-001",
            "validation requires exactly one of flux_min/flux_max or snr_min/snr_max",
        )
    kind = "flux" if has_flux else "snr"
    lo = _optional_float(params, f"{kind}_min")
    hi = _optional_float(params, f"{kind}_max")
    if lo is None or hi is None:
        raise APIError("ORB-API-001", f"{kind}_min and {kind}_max are both required")
    if lo <= 0 or hi < lo:
        raise APIError("ORB-API-001", f"{kind}_min/{kind}_max must satisfy 0 < min <= max")
    return kind, float(lo), float(hi)


def _speed_range(params: dict[str, Any]) -> tuple[float, float]:
    lo = _optional_float(params, "speed_min_arcsec_per_day")
    hi = _optional_float(params, "speed_max_arcsec_per_day")
    if lo is None or hi is None:
        raise APIError(
            "ORB-API-001",
            "speed_min_arcsec_per_day and speed_max_arcsec_per_day are required",
        )
    if lo < 0 or hi < lo:
        raise APIError(
            "ORB-API-001",
            "speed range must satisfy 0 <= min <= max (arcsec/day)",
        )
    return float(lo), float(hi)


def _n_injections(params: dict[str, Any], settings: Any) -> int:
    raw = params.get("n_injections", _DEFAULT_N_INJECTIONS)
    try:
        n = int(raw)
    except Exception as exc:
        raise APIError("ORB-API-001", "n_injections must be an integer") from exc
    if n < 1:
        raise APIError("ORB-API-001", "n_injections must be >= 1")
    cap = int(getattr(settings, "validation_max_injections", 200) or 200)
    if n > cap:
        raise APIError(
            "ORB-API-001",
            f"n_injections {n} exceeds ORB_VALIDATION_MAX_INJECTIONS ({cap})",
        )
    return n


def _seed(params: dict[str, Any]) -> int:
    raw = params.get("seed", _DEFAULT_SEED)
    try:
        return int(raw)
    except Exception as exc:
        raise APIError("ORB-API-001", "seed must be an integer") from exc


def _bin_edges(params: dict[str, Any], key: str) -> list[float] | None:
    raw = params.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) < 2:
        raise APIError("ORB-API-001", f"{key} must be a list of >=2 edges")
    edges = [search_mod._coerce_finite_float(v, name=key) for v in raw]
    if any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
        raise APIError("ORB-API-001", f"{key} must be strictly increasing")
    return edges


def _linspace_edges(lo: float, hi: float, n_bins: int) -> list[float] | None:
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return None
    return [float(v) for v in np.linspace(lo, hi, n_bins + 1)]


def _raw_core_frames(decoded: list[search_mod.DecodedFrame]) -> list[Frame]:
    """NaN-encode stored frames into core Frames for injection (pre-preprocessing)."""
    return [search_mod._to_core_frame(fr, np.asarray(fr.image, dtype=np.float64)) for fr in decoded]


def _decoded_from_injected(
    injected: Frame, template: search_mod.DecodedFrame
) -> search_mod.DecodedFrame:
    """Re-wrap an injected core Frame as a DecodedFrame for the science path.

    ``inject`` preserves NaN at invalid pixels, so validity is recovered as
    finite image AND variance, intersected with the template mask.
    """
    img = np.asarray(injected.image, dtype=np.float64)
    var = None if injected.variance is None else np.asarray(injected.variance, dtype=np.float64)
    mask = np.asarray(template.mask, dtype=bool) & np.isfinite(img)
    if var is not None:
        mask &= np.isfinite(var)
    return search_mod.DecodedFrame(
        frame_ref=template.frame_ref,
        image=img,
        variance=var if var is not None else template.variance,
        mask=mask,
        flags=template.flags,
        mjd=float(injected.time),
        detector=template.detector,
        wavelength_um=template.wavelength_um,
        meta=template.meta,
    )


def _candidates_to_detections(cands: list[dict[str, Any]], prefix: str) -> list[Detection]:
    """Extracted candidates -> match.Detections at the search reference time.

    ``x_centroid``/``y_centroid`` are positions on the reference-time grid —
    the same epoch convention ``match`` evaluates truth on (VAL.02).
    """
    return [
        Detection(
            x=float(c["x_centroid"]),
            y=float(c["y_centroid"]),
            label=f"{prefix}{j}",
            score=float(c["stack_snr"]),
            static_source=bool((c.get("_static_ev") or {}).get("flagged_static_source")),
        )
        for j, c in enumerate(cands)
    ]


async def validation(ctx: JobContext) -> dict[str, Any]:
    """Job handler for ``validation`` injection-and-recovery experiments."""
    db = ctx.db
    settings = ctx.settings
    params = dict(ctx.params or {})
    field_id = params.get("field_id") or await _job_field_id(ctx)
    if not field_id:
        raise APIError("ORB-API-001", "validation requires field_id")
    params["field_id"] = field_id

    field = await get_field(db, field_id)
    if field is None:
        raise APIError("ORB-API-002", "field not found")
    if field.get("status") != "completed":
        raise APIError("ORB-RS-001", "field must be completed before validation")

    # --- experiment parameters -------------------------------------------------
    n_inj = _n_injections(params, settings)
    seed = _seed(params)
    flux_kind, flux_lo, flux_hi = _flux_range(params)
    speed_lo, speed_hi = _speed_range(params)
    psf_sigma = _optional_float(params, "psf_sigma_px")
    psf_sigma = psf_sigma if psf_sigma is not None else _DEFAULT_PSF_SIGMA_PX
    if psf_sigma <= 0 or psf_sigma > _MAX_PSF_SIGMA_PX:
        raise APIError(
            "ORB-API-001",
            f"psf_sigma_px must be in (0, {_MAX_PSF_SIGMA_PX}]",
        )
    psf = GaussianPSF(sigma_px=psf_sigma)

    match_radius = _optional_float(params, "match_radius_px")
    if match_radius is None:
        match_radius = float(getattr(settings, "match_radius_px", None) or _DEFAULT_MATCH_RADIUS_PX)
    if match_radius <= 0:
        raise APIError("ORB-API-001", "match_radius_px must be > 0")

    fixed_pa_deg = params.get("position_angle_deg")
    if fixed_pa_deg is not None:
        fixed_pa_deg = search_mod._coerce_finite_float(fixed_pa_deg, name="position_angle_deg")

    flux_edges = _bin_edges(params, "flux_bin_edges")
    speed_edges = _bin_edges(params, "speed_bin_edges_arcsec_per_day")

    await ctx.progress(0, n_inj + 1, "load")
    ctx.check_cancelled()

    rows = await list_frames(db, field_id)
    # Same frame-window/frame_refs selection as the search job so a
    # validation experiment always runs on the identical frame subset.
    rows, frame_window_applied = search_mod.select_frame_rows(rows, params)
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
        raise APIError("ORB-RS-001", "field has no frames available for validation")

    policy_path = search_mod._resolve_stack_policy_path(settings)
    policy = load_stack_policy(policy_path)
    enforce_combinable(rows, policy)

    decoded_frames, _frame_meta = search_mod._decode_stored_frames(rows)
    shapes = {f.image.shape for f in decoded_frames}
    if len(shapes) != 1:
        raise APIError("ORB-RS-001", "stored frames do not share a common aligned shape")
    frame_shape = decoded_frames[0].image.shape
    field_wcs = search_mod._resolve_field_wcs(field, rows, frame_shape)
    h, w = frame_shape
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0

    motion_vectors = search_mod._parse_motion_vectors(params, field_wcs, cx, cy, policy)
    min_cov = search_mod._validate_min_coverage_fraction(params.get("min_coverage_fraction", 0.5))
    det_sigma = params.get("detection_threshold_sigma", params.get("detection_threshold"))
    if det_sigma is not None:
        det_sigma = search_mod._coerce_finite_float(det_sigma, name="detection_threshold_sigma")
        if det_sigma <= 0:
            raise APIError("ORB-API-001", "detection_threshold_sigma must be > 0")
    nms_radius = int(params.get("nms_radius_px", search_mod.EXTRACTION_NMS_RADIUS_PX))
    if nms_radius < 0:
        raise APIError("ORB-API-001", "nms_radius_px must be an integer >= 0")
    bg_variant = params.get("background_variant")
    interp_order = search_mod._validate_interpolation_order(params.get("interpolation_order"))
    noise_model = search_mod._validate_noise_model(params.get("noise_model"))

    work_estimate = len(motion_vectors) * len(decoded_frames) * h * w
    cfg_budget = search_mod._resolve_search_max_work(settings)
    if work_estimate > cfg_budget:
        raise APIError(
            "ORB-RS-003",
            "validation search exceeds work budget",
            detail={"work_estimate": work_estimate, "budget": cfg_budget},
        )

    sci_kwargs = dict(
        motion_vectors=motion_vectors,
        min_coverage_fraction=min_cov,
        detection_threshold_sigma=det_sigma,
        nms_radius_px=nms_radius,
        background_variant=bg_variant,
        work_budget=cfg_budget,
        interpolation_order=interp_order,
        noise_model=noise_model,
    )

    # --- baseline search on the un-injected frames ------------------------------
    base_sci = await asyncio.to_thread(
        search_mod.run_science_search, decoded_frames, warn=ctx.warn, **sci_kwargs
    )
    reference_time = float(base_sci.result.reference_time)
    baseline_dets = _candidates_to_detections(base_sci.candidates, "b")

    # --- sample all truth sources up front (deterministic given seed) -----------
    rng = np.random.default_rng(seed)
    margin = int(np.ceil(4.0 * psf.sigma_px)) + 1
    if margin >= (min(h, w) - 1) / 2.0:
        raise APIError(
            "ORB-RS-001",
            "frame is too small for the injection margin at this PSF sigma",
            detail={"frame_shape": [h, w], "psf_sigma_px": psf.sigma_px},
        )
    base_frames = _raw_core_frames(decoded_frames)
    frame_times = np.array([float(f.time) for f in base_frames], dtype=np.float64)

    # Median positive variance for the SNR->flux conversion (D-17 note: only
    # needed on the snr path; a field with no usable variance cannot convert).
    var_for_snr: float | None = None
    if flux_kind == "snr":
        var_stack = np.stack([np.asarray(f.variance, dtype=np.float64) for f in decoded_frames])
        finite_vars = var_stack[np.isfinite(var_stack) & (var_stack > 0)]
        if finite_vars.size == 0:
            raise APIError(
                "ORB-RS-001",
                "field has no finite positive variance; snr_min/snr_max "
                "cannot be converted to flux",
            )
        # Noise basis = the same calibrated variance the search sees:
        # stored median variance rescaled by run_science_search's measured
        # processed-noise factor (RS.08). flux_for_snr and stack_snr then
        # share one noise definition.
        var_for_snr = float(np.median(finite_vars)) * float(base_sci.noise_calibration)

    explicit_positions = params.get("inject_positions_px")
    if explicit_positions is not None:
        ok = isinstance(explicit_positions, list) and len(explicit_positions) == n_inj
        if ok:
            for p in explicit_positions:
                try:
                    ok = (
                        isinstance(p, (list, tuple))
                        and len(p) == 2
                        and all(np.isfinite(float(v)) for v in p)
                    )
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    break
        if not ok:
            raise APIError(
                "ORB-API-001",
                "inject_positions_px must be a list of n_injections [x, y] pairs",
            )

    def _draw(
        i: int, x0: float, y0: float
    ) -> tuple[InjectedSource, dict[str, Any], np.ndarray, np.ndarray]:
        """Draw flux/speed/PA and convert to a pixel-motion InjectedSource.

        Returns ``(src, trial_meta, xs, ys)`` where ``xs``/``ys`` are the
        track positions at every frame epoch (for the in-frame check).
        """
        if flux_kind == "flux":
            flux = float(rng.uniform(flux_lo, flux_hi))
            snr_sampled = None
        else:
            snr_sampled = float(rng.uniform(flux_lo, flux_hi))
            try:
                flux = flux_for_snr(snr_sampled, var_for_snr, psf)
            except InvalidInputError as exc:
                raise APIError("ORB-RS-001", f"snr->flux failed: {exc}") from exc
        speed = float(rng.uniform(speed_lo, speed_hi))  # arcsec/day
        if fixed_pa_deg is None:
            pa = float(rng.uniform(0.0, 2.0 * math.pi))
        else:
            pa = math.radians(fixed_pa_deg)
        # D-17 PA convention (0=N, 90=E), same as _motion_to_pixel_rate.
        mu_e = speed * math.sin(pa)
        mu_n = speed * math.cos(pa)
        try:
            vx, vy = sky_motion_to_pixel(mu_e, mu_n, field_wcs, (x0, y0))
        except InvalidInputError as exc:
            raise APIError("ORB-RS-001", f"motion conversion failed: {exc}") from exc
        if not (np.isfinite(vx) and np.isfinite(vy)):
            raise APIError("ORB-RS-001", "sampled motion is not finite")
        src = InjectedSource(
            x0=x0,
            y0=y0,
            vx=float(vx),
            vy=float(vy),
            flux=flux,
            reference_time=reference_time,
            label=f"inj_{i}",
        )
        meta = {
            "flux_kind": flux_kind,
            "snr": snr_sampled,
            "flux": flux,
            "speed_arcsec_per_day": speed,
            "position_angle_deg": math.degrees(pa),
            "mu_e_arcsec_per_day": mu_e,
            "mu_n_arcsec_per_day": mu_n,
        }
        xs = x0 + vx * (frame_times - reference_time)
        ys = y0 + vy * (frame_times - reference_time)
        return src, meta, xs, ys

    def _track_in_margin(xs: np.ndarray, ys: np.ndarray) -> bool:
        return bool(
            xs.min() >= margin
            and xs.max() <= w - 1 - margin
            and ys.min() >= margin
            and ys.max() <= h - 1 - margin
        )

    # Real frames are 26-57% masked AND variant E NaNs any pixel valid in
    # <3 epochs, so coverage must be checked on the PROCESSED frames the
    # search actually sees (base_sci.core_frames), not the raw masks —
    # a track can pass raw coverage yet be mostly NaN after E (RS.08).
    proc_frames = base_sci.core_frames
    finite_cube = [np.isfinite(np.asarray(f.image, dtype=np.float64)) for f in proc_frames]
    needed_epochs = math.ceil(min_cov * len(proc_frames))

    def _track_has_data(xs: np.ndarray, ys: np.ndarray) -> bool:
        good = 0
        r = 2
        for i in range(len(xs)):
            xi, yi = int(round(float(xs[i]))), int(round(float(ys[i])))
            xb0, xb1 = max(0, xi - r), min(w, xi + r + 1)
            yb0, yb1 = max(0, yi - r), min(h, yi + r + 1)
            if xb0 >= xb1 or yb0 >= yb1:
                continue
            if float(np.mean(finite_cube[i][yb0:yb1, xb0:xb1])) >= 0.8:
                good += 1
        return good >= needed_epochs

    sources: list[InjectedSource] = []
    trial_meta: list[dict[str, Any]] = []
    n_position_rejects = 0
    for i in range(n_inj):
        # Reject-sample the start position until the WHOLE track stays inside
        # the PSF margin at every epoch — a source that leaves the frame
        # partway loses flux and would be scored missed while physically
        # undetectable (VAL.05 review). Explicit positions get exactly one
        # draw: resampling them would silently move the requested truth.
        src: InjectedSource | None = None
        meta: dict[str, Any] | None = None
        attempts = 1 if explicit_positions is not None else _MAX_POSITION_ATTEMPTS
        for _attempt in range(attempts):
            if explicit_positions is not None:
                x0, y0 = (float(v) for v in explicit_positions[i])
            else:
                x0 = float(rng.uniform(margin, w - 1 - margin))
                y0 = float(rng.uniform(margin, h - 1 - margin))
            cand_src, cand_meta, xs, ys = _draw(i, x0, y0)
            if _track_in_margin(xs, ys) and _track_has_data(xs, ys):
                src, meta = cand_src, cand_meta
                break
            n_position_rejects += 1
        if src is None or meta is None:
            raise APIError(
                "ORB-RS-001",
                "injection track leaves the frame margin at some epoch; "
                "tighten the speed range or supply inject_positions_px",
                detail={"trial": i, "frame_shape": [h, w], "margin_px": margin},
            )
        sources.append(src)
        trial_meta.append(meta)

    # Default completeness bins span the sampled ranges (D-15: 5 flux,
    # 4 speed bins); explicit params override. Degenerate ranges skip bins.
    # Speed bins are arcsec/day per D-17 — the same unit the truth was
    # sampled in — never internal px/day.
    if flux_edges is None:
        flux_vals = [s.flux for s in sources]
        flux_edges = _linspace_edges(min(flux_vals), max(flux_vals), _N_FLUX_BINS)
    speed_values = [float(m["speed_arcsec_per_day"]) for m in trial_meta]
    if speed_edges is None:
        speed_edges = _linspace_edges(min(speed_values), max(speed_values), _N_SPEED_BINS)

    # Resolved parameters: the request plus every default actually used, so
    # the stored experiment is self-describing (D-15). injection_stage marks
    # that injection happens on the aligned grid after regridding — recall
    # and completeness exclude alignment loss (D-25) and are NOT an
    # end-to-end recovery measure.
    resolved = dict(params)
    resolved.pop("speed_bin_edges_px_per_day", None)  # renamed; see D-17
    resolved.update(
        {
            "n_injections": n_inj,
            "seed": seed,
            "flux_kind": flux_kind,
            # flux_min/max or snr_min/max are already in params verbatim;
            # flux_kind records which pair they are.
            "speed_min_arcsec_per_day": speed_lo,
            "speed_max_arcsec_per_day": speed_hi,
            "psf_sigma_px": psf_sigma,
            "match_radius_px": match_radius,
            "flux_bin_edges": flux_edges,
            "speed_bin_edges_arcsec_per_day": speed_edges,
            "injection_stage": "aligned_grid",
            "background_variant_used": base_sci.variant_used,
            "noise_basis": "processed_mad_calibrated_variance",
            "noise_calibration": float(base_sci.noise_calibration),
            "n_position_rejects": n_position_rejects,
            # Provenance: the exact frames the experiment ran on, so the
            # result stays interpretable if more frames arrive later.
            "frame_refs_used": [str(r.get("frame_ref")) for r in rows],
            "frame_window_applied": frame_window_applied,
        }
    )

    # --- experiment record (trials FK requires the parent row first) -----------
    job_key = hashlib.sha256(
        json.dumps({"job_id": ctx.job_id}, sort_keys=True).encode()
    ).hexdigest()[:24]
    experiment_id = f"exp_{job_key}"
    await put_experiment(
        db,
        experiment_id,
        ctx.job_id,
        field_id,
        params=resolved,
        label=params.get("label"),
    )

    # --- trials ------------------------------------------------------------------
    all_outcomes = []
    metrics_written = False
    try:
        for i, src in enumerate(sources):
            ctx.check_cancelled()
            injected_frames = inject(base_frames, [src], psf)
            injected_decoded = [
                _decoded_from_injected(cf, tpl)
                for cf, tpl in zip(injected_frames, decoded_frames, strict=True)
            ]
            # warn=None: the provisional-variant warning already fired on the
            # baseline call; per-trial repeats would spam the job warnings.
            sci = await asyncio.to_thread(
                search_mod.run_science_search,
                injected_decoded,
                warn=None,
                **sci_kwargs,
            )
            dets = _candidates_to_detections(sci.candidates, f"t{i}_c")
            outcomes = match(
                dets,
                [src],
                baseline_dets,
                radius_px=match_radius,
                reference_time=reference_time,
            )
            all_outcomes.append(outcomes)
            tm = outcomes.truth[0]
            matched_det = None
            for dc in outcomes.detections:
                if dc.source is src and dc.distance_px is not None:
                    matched_det = dc.detection
                    break
            await add_trial(
                db,
                experiment_id,
                i,
                truth={**asdict(src), **trial_meta[i]},
                outcome=tm.outcome,
                detection=(
                    None
                    if matched_det is None
                    else {
                        "x": matched_det.x,
                        "y": matched_det.y,
                        "label": matched_det.label,
                        "score": matched_det.score,
                        "static_source": matched_det.static_source,
                    }
                ),
                position_error_px=tm.distance_px,
                n_spurious=outcomes.n_spurious_detections,
            )
            await ctx.progress(i + 1, n_inj + 1, "trial")

        # --- pooled metrics -------------------------------------------------------
        pooled = pooled_metrics(
            all_outcomes,
            flux_edges=flux_edges,
            speed_edges=speed_edges,
            speed_values=speed_values[: len(all_outcomes)],
        )
        metrics_json = json.loads(json.dumps(asdict(pooled)))  # tuples -> lists
        metrics_json["partial"] = False
        await put_experiment(
            db,
            experiment_id,
            ctx.job_id,
            field_id,
            params=resolved,
            metrics=metrics_json,
            label=params.get("label"),
        )
        metrics_written = True
    except BaseException:
        # Cancel/timeout/failure mid-run: keep the completed trials and write
        # partial pooled metrics so the experiment is still readable (a
        # real-field n=20 run can exceed the default job timeout).
        if all_outcomes and not metrics_written:
            try:
                partial = pooled_metrics(
                    all_outcomes,
                    flux_edges=flux_edges,
                    speed_edges=speed_edges,
                    speed_values=speed_values[: len(all_outcomes)],
                )
                partial_json = json.loads(json.dumps(asdict(partial)))
                partial_json["partial"] = True
                partial_json["n_trials_completed"] = len(all_outcomes)
                await put_experiment(
                    db,
                    experiment_id,
                    ctx.job_id,
                    field_id,
                    params=resolved,
                    metrics=partial_json,
                    label=params.get("label"),
                )
            except Exception:
                log.exception("failed to write partial validation metrics")
        raise

    return {
        "experiment_id": experiment_id,
        "job_id": ctx.job_id,
        "field_id": field_id,
        "n_trials": pooled.n_trials,
        "n_recovered_truth": pooled.n_recovered_truth,
        "n_missed_truth": pooled.n_missed_truth,
        "n_preexisting_truth": pooled.n_preexisting_truth,
        "n_spurious_detections": pooled.n_spurious_detections,
        "spurious_counts": list(pooled.spurious_counts),
        "recall": pooled.recall,
        "recall_ci": [pooled.recall_ci_low, pooled.recall_ci_high],
        "precision": pooled.precision,
        "precision_ci": [pooled.precision_ci_low, pooled.precision_ci_high],
        "position_error": asdict(pooled.position_error),
        "match_radius_px": match_radius,
        "reference_time_mjd": reference_time,
        "background_variant": base_sci.variant_used,
        "interpolation_order": interp_order,
        "noise_model": noise_model,
        "noise_model_frame_ratios": [round(float(r), 6) for r in base_sci.frame_noise_ratios],
        "seed": seed,
        "partial": False,
    }


async def _job_field_id(ctx: JobContext) -> str | None:
    row = await get_job(ctx.db, ctx.job_id)
    return row.get("field_id") if row else None


__all__ = ["validation"]
