"""Truth matching and outcome classes for injection-and-recovery validation.

Purpose: VAL.02 / FR-VAL-02 / D-15 — assign each injected truth and each
detection one of the four outcome classes.
Inputs: ``detections`` (search output on injected frames), ``truth``
(``InjectedSource`` records), ``baseline_detections`` (search output on the
un-injected frames), ``radius_px``, ``reference_time``.
Outputs: ``Outcomes`` — per-truth and per-detection classifications plus
counts. Pure function; no I/O.
Called by: ``orbitalis.research`` validation handlers (VAL.04); tests.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from orbitalis_core.errors import InvalidInputError
from orbitalis_core.inject.inject import InjectedSource

__all__ = ["Detection", "DetectionClass", "Outcomes", "TruthMatch", "match"]

OUTCOME_RECOVERED = "recovered"
OUTCOME_MISSED = "missed"
OUTCOME_PREEXISTING = "preexisting"
OUTCOME_SPURIOUS = "spurious"


@dataclass(frozen=True)
class Detection:
    """One search detection positioned at the match reference time.

    ``x``/``y`` are pixel coordinates at ``reference_time`` — the caller
    (validation handler) is responsible for reporting candidate positions at
    the same epoch the truth is evaluated on. ``label`` is an opaque id for
    traceability; ``score`` (e.g. stack SNR) is carried but unused here.
    ``static_source`` records the search's static-source flag for
    transparency; matching stays position-only (D-15), so a flagged
    detection matched to truth still counts as ``recovered``.
    """

    x: float
    y: float
    label: str | None = None
    score: float | None = None
    static_source: bool | None = None


@dataclass(frozen=True)
class TruthMatch:
    """Outcome for one injected truth source."""

    source: InjectedSource
    outcome: str  # recovered | missed | preexisting
    detection: Detection | None
    distance_px: float | None


@dataclass(frozen=True)
class DetectionClass:
    """Outcome for one detection on the injected frames."""

    detection: Detection
    outcome: str  # recovered | preexisting | spurious
    source: InjectedSource | None
    distance_px: float | None


@dataclass(frozen=True)
class Outcomes:
    """Full match result: per-truth and per-detection classes + counts."""

    truth: tuple[TruthMatch, ...]
    detections: tuple[DetectionClass, ...]
    radius_px: float
    reference_time: float

    @property
    def n_recovered_truth(self) -> int:
        return sum(1 for t in self.truth if t.outcome == OUTCOME_RECOVERED)

    @property
    def n_missed_truth(self) -> int:
        return sum(1 for t in self.truth if t.outcome == OUTCOME_MISSED)

    @property
    def n_preexisting_truth(self) -> int:
        return sum(1 for t in self.truth if t.outcome == OUTCOME_PREEXISTING)

    @property
    def n_recovered_detections(self) -> int:
        return sum(1 for d in self.detections if d.outcome == OUTCOME_RECOVERED)

    @property
    def n_preexisting_detections(self) -> int:
        return sum(1 for d in self.detections if d.outcome == OUTCOME_PREEXISTING)

    @property
    def n_spurious_detections(self) -> int:
        return sum(1 for d in self.detections if d.outcome == OUTCOME_SPURIOUS)


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def match(
    detections: Sequence[Detection],
    truth: Sequence[InjectedSource],
    baseline_detections: Sequence[Detection],
    *,
    radius_px: float,
    reference_time: float,
) -> Outcomes:
    """Classify detections and injected truth into the four outcome classes.

    D-15 rules, applied at ``reference_time``:

    - A detection matching an injected truth within ``radius_px`` is
      ``recovered`` — unless it also matches a baseline (un-injected) detection,
      in which case it is ``preexisting`` (a real source was already there).
    - A detection matching no truth but matching a baseline detection is
      ``preexisting``.
    - A detection matching neither is ``spurious``.
    - A truth coincident with a baseline source is ``preexisting``
      (excluded from recall per D-15) — checked directly at
      ``reference_time``, independent of detections, so an injection merged
      into or shifted off a real peak does not deflate recall as ``missed``.
    - Truth↔detection assignment is greedy nearest-neighbour, one-to-one, so a
      single detection cannot recover two truths and vice versa. A detection
      matched to a preexisting truth is itself ``preexisting``.
    """
    radius_px = float(radius_px)
    reference_time = float(reference_time)
    if not np.isfinite(radius_px) or radius_px <= 0:
        raise InvalidInputError("radius_px must be a positive finite number")
    if not np.isfinite(reference_time):
        raise InvalidInputError("reference_time must be finite")

    dets = list(detections)
    truths = list(truth)
    baseline = list(baseline_detections)
    for d in dets + baseline:
        if not isinstance(d, Detection):
            raise InvalidInputError("detections must be Detection instances")
        if not (np.isfinite(d.x) and np.isfinite(d.y)):
            raise InvalidInputError("detection positions must be finite")
    for s in truths:
        if not isinstance(s, InjectedSource):
            raise InvalidInputError("truth must be InjectedSource instances")

    # Truth positions evaluated at the match reference time.
    tpos = [s.position_at(reference_time) for s in truths]

    # Baseline (preexisting) flag per detection: nearest baseline detection
    # within radius. Baseline detections are already positions at the same
    # epoch convention — for static fields this is epoch-independent.
    det_preexisting: list[bool] = []
    for d in dets:
        hit = any(_dist(d.x, d.y, b.x, b.y) <= radius_px for b in baseline)
        det_preexisting.append(hit)

    # A truth coincident with a baseline source is preexisting regardless of
    # detections — checked directly at reference_time so an injection merged
    # into or shifted off a real peak does not deflate recall as "missed".
    truth_preexisting: list[bool] = []
    for tx, ty in tpos:
        hit = any(_dist(tx, ty, b.x, b.y) <= radius_px for b in baseline)
        truth_preexisting.append(hit)

    # Candidate truth-detection pairs within radius, greedy nearest-first.
    pairs = []
    for ti, (tx, ty) in enumerate(tpos):
        for di, d in enumerate(dets):
            dist = _dist(d.x, d.y, tx, ty)
            if dist <= radius_px:
                pairs.append((dist, ti, di))
    pairs.sort(key=lambda p: (p[0], p[1], p[2]))
    truth_used = [False] * len(truths)
    det_used = [False] * len(dets)
    det_match: dict[int, tuple[int, float]] = {}  # di -> (ti, dist)
    for dist, ti, di in pairs:
        if not truth_used[ti] and not det_used[di]:
            truth_used[ti] = True
            det_used[di] = True
            det_match[di] = (ti, dist)

    detection_outcomes: list[DetectionClass] = []
    for di, d in enumerate(dets):
        if di in det_match:
            ti, dist = det_match[di]
            # A detection on a preexisting truth detected the real source,
            # even if it sits slightly off the baseline position.
            if det_preexisting[di] or truth_preexisting[ti]:
                outcome = OUTCOME_PREEXISTING
            else:
                outcome = OUTCOME_RECOVERED
            detection_outcomes.append(
                DetectionClass(detection=d, outcome=outcome, source=truths[ti], distance_px=dist)
            )
        elif det_preexisting[di]:
            detection_outcomes.append(
                DetectionClass(
                    detection=d, outcome=OUTCOME_PREEXISTING, source=None, distance_px=None
                )
            )
        else:
            detection_outcomes.append(
                DetectionClass(detection=d, outcome=OUTCOME_SPURIOUS, source=None, distance_px=None)
            )

    truth_outcomes: list[TruthMatch] = []
    for ti, s in enumerate(truths):
        if truth_preexisting[ti]:
            # Coincident with a real source: excluded from recall per D-15,
            # whether or not a detection claimed it.
            di = next((di for di, (t2, _d) in det_match.items() if t2 == ti), None)
            truth_outcomes.append(
                TruthMatch(
                    source=s,
                    outcome=OUTCOME_PREEXISTING,
                    detection=dets[di] if di is not None else None,
                    distance_px=det_match[di][1] if di is not None else None,
                )
            )
        elif truth_used[ti]:
            # find the detection that claimed this truth
            di = next(di for di, (t2, _d2) in det_match.items() if t2 == ti)
            dist = det_match[di][1]
            det = dets[di]
            truth_outcomes.append(
                TruthMatch(source=s, outcome=OUTCOME_RECOVERED, detection=det, distance_px=dist)
            )
        else:
            truth_outcomes.append(
                TruthMatch(source=s, outcome=OUTCOME_MISSED, detection=None, distance_px=None)
            )

    return Outcomes(
        truth=tuple(truth_outcomes),
        detections=tuple(detection_outcomes),
        radius_px=radius_px,
        reference_time=reference_time,
    )
