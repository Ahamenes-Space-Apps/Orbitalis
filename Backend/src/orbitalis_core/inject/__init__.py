"""Synthetic injection, truth matching and recovery metrics.

Purpose: VAL.01–VAL.03 — PSF injection into frame copies (never mutating
inputs), truth matching and recovery metrics for injection-and-recovery
validation experiments.
Inputs: ``inject`` takes ``Sequence[Frame]``, ``Sequence[InjectedSource]``,
``GaussianPSF``.
Outputs: ``inject`` returns new ``list[Frame]``; ``match`` returns
``Outcomes`` (VAL.02); ``metrics`` aggregates ``Outcomes`` into
``Metrics`` (VAL.03).
Called by: ``orbitalis.research`` validation handlers; tests.
"""

from .inject import GaussianPSF, InjectedSource, flux_for_snr, inject
from .match import Detection, DetectionClass, Outcomes, TruthMatch, match
from .metrics import (
    BinCompleteness,
    Metrics,
    PooledMetrics,
    PositionError,
    metrics,
    pooled_metrics,
)

__all__ = [
    "BinCompleteness",
    "Detection",
    "DetectionClass",
    "GaussianPSF",
    "InjectedSource",
    "Metrics",
    "Outcomes",
    "PooledMetrics",
    "PositionError",
    "TruthMatch",
    "flux_for_snr",
    "inject",
    "match",
    "metrics",
    "pooled_metrics",
]
