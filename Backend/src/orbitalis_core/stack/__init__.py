"""Image stacking and difference imaging.

Purpose: Public surface for sigma-clipped combination, inverse-variance
weighted combination, and frame differencing.
Inputs: None at import.
Outputs: Re-exported functions and result types.
Called by: Pipeline group combine, shift-and-stack search, research handlers
and tests.
"""

from .combine import CombineResult, combine, difference
from .weighted import combine_prepared, weighted_combine

__all__ = [
    "CombineResult",
    "combine",
    "combine_prepared",
    "difference",
    "weighted_combine",
]
