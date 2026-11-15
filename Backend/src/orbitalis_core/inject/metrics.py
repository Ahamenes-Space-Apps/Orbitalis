"""Recovery metrics for injection-and-recovery validation experiments.

Purpose: VAL.03 / FR-VAL-03 / D-15 — aggregate ``Outcomes`` into recall,
precision relative to injected sources, position error and completeness by
flux and speed bin.
Inputs: ``Outcomes`` from ``inject.match``; optional bin edges.
Outputs: ``Metrics`` dataclass. Pure function; no I/O.
Called by: ``orbitalis.research`` validation handlers (VAL.04); tests.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from orbitalis_core.errors import InvalidInputError
from orbitalis_core.inject.match import (
    OUTCOME_MISSED,
    OUTCOME_PREEXISTING,
    OUTCOME_RECOVERED,
    DetectionClass,
    Outcomes,
    TruthMatch,
)

__all__ = [
    "BinCompleteness",
    "Metrics",
    "PooledMetrics",
    "PositionError",
    "metrics",
    "pooled_metrics",
]


@dataclass(frozen=True)
class PositionError:
    """Distribution of truth↔detection distances for recovered sources."""

    n: int
    median_px: float | None
    p90_px: float | None
    max_px: float | None


@dataclass(frozen=True)
class BinCompleteness:
    """Completeness inside one flux or speed bin (D-15: preexisting excluded)."""

    lo: float
    hi: float
    n_injected: int  # recovered + missed (preexisting excluded)
    n_recovered: int
    n_preexisting: int
    completeness: float | None  # None when the bin has no evaluable truth


@dataclass(frozen=True)
class Metrics:
    """Aggregate validation metrics for one experiment (or trial set).

    ``recall`` = recovered / (recovered + missed) over injected truth;
    ``precision`` = recovered / (recovered + spurious) over detections and is
    ALWAYS relative to the sources injected in this experiment (D-15) — it is
    never a statement about real-data false positives.
    """

    n_truth_total: int
    n_recovered_truth: int
    n_missed_truth: int
    n_preexisting_truth: int
    n_detections_total: int
    n_recovered_detections: int
    n_preexisting_detections: int
    n_spurious_detections: int
    recall: float | None
    precision: float | None
    position_error: PositionError
    completeness_flux: tuple[BinCompleteness, ...]
    completeness_speed: tuple[BinCompleteness, ...]
    n_truth_outside_flux_bins: int
    n_truth_outside_speed_bins: int


def _ratio(num: int, den: int) -> float | None:
    return (num / den) if den else None


def _position_error(outcomes: Outcomes) -> PositionError:
    dists = [
        t.distance_px
        for t in outcomes.truth
        if t.outcome == OUTCOME_RECOVERED and t.distance_px is not None
    ]
    if not dists:
        return PositionError(n=0, median_px=None, p90_px=None, max_px=None)
    arr = np.asarray(dists, dtype=np.float64)
    return PositionError(
        n=len(dists),
        median_px=float(np.median(arr)),
        p90_px=float(np.percentile(arr, 90)),
        max_px=float(arr.max()),
    )


def _check_edges(edges: Sequence[float] | None, name: str) -> list[float] | None:
    if edges is None:
        return None
    e = [float(v) for v in edges]
    if len(e) < 2 or any(not np.isfinite(v) for v in e):
        raise InvalidInputError(f"{name} bin edges must be >=2 finite values")
    if any(b <= a for a, b in zip(e, e[1:], strict=False)):
        raise InvalidInputError(f"{name} bin edges must be strictly increasing")
    return e


def _bin_completeness(
    outcomes: Outcomes,
    values: Sequence[float],
    edges: list[float] | None,
) -> tuple[tuple[BinCompleteness, ...], int]:
    """Per-bin recovered/(recovered+missed); returns (bins, n_outside)."""
    if edges is None:
        return (), 0
    bins = [{"n_inj": 0, "n_rec": 0, "n_pre": 0} for _ in range(len(edges) - 1)]
    n_outside = 0
    last = len(edges) - 2
    for t, v in zip(outcomes.truth, values, strict=True):
        # [lo, hi) bins, except the last which is right-closed so a source
        # sampled exactly at the top edge is not silently dropped.
        idx = next(
            (
                i
                for i in range(len(edges) - 1)
                if edges[i] <= v < edges[i + 1] or (i == last and v == edges[i + 1])
            ),
            None,
        )
        if idx is None:
            n_outside += 1
            continue
        b = bins[idx]
        if t.outcome == OUTCOME_RECOVERED:
            b["n_rec"] += 1
            b["n_inj"] += 1
        elif t.outcome == OUTCOME_MISSED:
            b["n_inj"] += 1
        elif t.outcome == OUTCOME_PREEXISTING:
            b["n_pre"] += 1
    return (
        tuple(
            BinCompleteness(
                lo=edges[i],
                hi=edges[i + 1],
                n_injected=b["n_inj"],
                n_recovered=b["n_rec"],
                n_preexisting=b["n_pre"],
                completeness=_ratio(b["n_rec"], b["n_inj"]),
            )
            for i, b in enumerate(bins)
        ),
        n_outside,
    )


def metrics(
    outcomes: Outcomes,
    *,
    flux_edges: Sequence[float] | None = None,
    speed_edges: Sequence[float] | None = None,
    speed_values: Sequence[float] | None = None,
) -> Metrics:
    """Aggregate ``Outcomes`` into experiment-level metrics (D-15).

    ``flux_edges``/``speed_edges`` are strictly increasing bin edges
    (``[lo, hi)`` bins, last bin right-closed); sources outside every bin
    are counted in ``n_truth_outside_*_bins``, never silently dropped.
    Speed defaults to the injected velocity magnitude in px/day
    (``hypot(vx, vy)``); callers whose public unit is arcsec/day (D-17)
    pass ``speed_values`` aligned with ``outcomes.truth`` instead, and
    must supply edges in the same unit.
    """
    if not isinstance(outcomes, Outcomes):
        raise InvalidInputError("metrics requires an Outcomes object")
    fe = _check_edges(flux_edges, "flux")
    se = _check_edges(speed_edges, "speed")

    flux_vals = [float(t.source.flux) for t in outcomes.truth]
    if speed_values is None:
        speed_vals = [float(np.hypot(t.source.vx, t.source.vy)) for t in outcomes.truth]
    else:
        speed_vals = [float(v) for v in speed_values]
        if len(speed_vals) != len(outcomes.truth):
            raise InvalidInputError("speed_values must align with outcomes.truth")
    cflux, n_out_flux = _bin_completeness(outcomes, flux_vals, fe)
    cspeed, n_out_speed = _bin_completeness(outcomes, speed_vals, se)

    n_rec_t = outcomes.n_recovered_truth
    n_mis_t = outcomes.n_missed_truth
    n_rec_d = outcomes.n_recovered_detections
    n_spu_d = outcomes.n_spurious_detections

    return Metrics(
        n_truth_total=len(outcomes.truth),
        n_recovered_truth=n_rec_t,
        n_missed_truth=n_mis_t,
        n_preexisting_truth=outcomes.n_preexisting_truth,
        n_detections_total=len(outcomes.detections),
        n_recovered_detections=n_rec_d,
        n_preexisting_detections=outcomes.n_preexisting_detections,
        n_spurious_detections=n_spu_d,
        recall=_ratio(n_rec_t, n_rec_t + n_mis_t),
        precision=_ratio(n_rec_d, n_rec_d + n_spu_d),
        position_error=_position_error(outcomes),
        completeness_flux=cflux,
        completeness_speed=cspeed,
        n_truth_outside_flux_bins=n_out_flux,
        n_truth_outside_speed_bins=n_out_speed,
    )


@dataclass(frozen=True)
class PooledMetrics:
    """Metrics pooled across many validation trials (VAL.04).

    Counts are summed across trials BEFORE ratios are taken — pooled recall
    is ``sum(recovered) / sum(recovered + missed)``, never the mean of
    per-trial ratios (which biases toward small trials). Wilson 95%
    intervals are reported because trial counts are small. ``spurious_counts``
    keeps the per-trial spurious-detection counts so precision is never read
    without its denominator structure (D-15: precision is relative to the
    sources injected in this experiment).
    """

    n_trials: int
    n_truth_total: int
    n_recovered_truth: int
    n_missed_truth: int
    n_preexisting_truth: int
    n_detections_total: int
    n_recovered_detections: int
    n_preexisting_detections: int
    n_spurious_detections: int
    recall: float | None
    recall_ci_low: float | None
    recall_ci_high: float | None
    precision: float | None
    precision_ci_low: float | None
    precision_ci_high: float | None
    position_error: PositionError
    completeness_flux: tuple[BinCompleteness, ...]
    completeness_speed: tuple[BinCompleteness, ...]
    n_truth_outside_flux_bins: int
    n_truth_outside_speed_bins: int
    spurious_counts: tuple[int, ...]


def _wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    """Wilson score interval for a binomial proportion (95% by default)."""
    if n <= 0:
        return None, None
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pooled_metrics(
    outcomes: Sequence[Outcomes],
    *,
    flux_edges: Sequence[float] | None = None,
    speed_edges: Sequence[float] | None = None,
    speed_values: Sequence[float] | None = None,
) -> PooledMetrics:
    """Pool per-trial ``Outcomes`` into experiment-level metrics (VAL.04).

    All trials must come from the same experiment (same match radius and
    reference-time convention); ``radius_px``/``reference_time`` of the
    merged view are taken from the first trial and are not used by the
    aggregation. Position-error distances and completeness bins pool over
    every recovered truth / injected source across trials.
    ``speed_values`` (arcsec/day or any caller-chosen unit, per D-17) must
    align with the concatenated ``truth`` of all trials when given.
    """
    trials = list(outcomes)
    if not trials:
        raise InvalidInputError("pooled_metrics requires at least one Outcomes")
    for o in trials:
        if not isinstance(o, Outcomes):
            raise InvalidInputError("pooled_metrics requires Outcomes instances")

    truth: list[TruthMatch] = []
    dets: list[DetectionClass] = []
    spurious_counts: list[int] = []
    for o in trials:
        truth.extend(o.truth)
        dets.extend(o.detections)
        spurious_counts.append(o.n_spurious_detections)

    merged = Outcomes(
        truth=tuple(truth),
        detections=tuple(dets),
        radius_px=trials[0].radius_px,
        reference_time=trials[0].reference_time,
    )
    m = metrics(
        merged,
        flux_edges=flux_edges,
        speed_edges=speed_edges,
        speed_values=speed_values,
    )

    n_rec_t = m.n_recovered_truth
    n_eval_t = n_rec_t + m.n_missed_truth
    n_rec_d = m.n_recovered_detections
    n_eval_d = n_rec_d + m.n_spurious_detections
    rec_ci = _wilson(n_rec_t, n_eval_t)
    prec_ci = _wilson(n_rec_d, n_eval_d)

    return PooledMetrics(
        n_trials=len(trials),
        n_truth_total=m.n_truth_total,
        n_recovered_truth=n_rec_t,
        n_missed_truth=m.n_missed_truth,
        n_preexisting_truth=m.n_preexisting_truth,
        n_detections_total=m.n_detections_total,
        n_recovered_detections=n_rec_d,
        n_preexisting_detections=m.n_preexisting_detections,
        n_spurious_detections=m.n_spurious_detections,
        recall=m.recall,
        recall_ci_low=rec_ci[0],
        recall_ci_high=rec_ci[1],
        precision=m.precision,
        precision_ci_low=prec_ci[0],
        precision_ci_high=prec_ci[1],
        position_error=m.position_error,
        completeness_flux=m.completeness_flux,
        completeness_speed=m.completeness_speed,
        n_truth_outside_flux_bins=m.n_truth_outside_flux_bins,
        n_truth_outside_speed_bins=m.n_truth_outside_speed_bins,
        spurious_counts=tuple(spurious_counts),
    )
