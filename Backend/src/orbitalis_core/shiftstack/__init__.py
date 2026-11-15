"""Trial motion vectors, shift-and-combine and score maps.

Purpose: Public surface for the pure shift-and-stack science module.
Inputs: None.
Outputs: Re-exported models, helpers, and search functions.
Called by: Research handlers and tests.
"""

from .errors import InvalidInputError, ShiftStackError, WorkBudgetExceededError
from .models import Frame, MotionVector, SearchResult, ShiftCombineResult
from .motion import (
    DEFAULT_PIXEL_SCALE_ARCSEC_PER_PIXEL,
    DEFAULT_PRIOR_RADIUS_ARCSEC_PER_DAY,
    generate_sky_motion_grid,
    pixel_motion_to_sky,
    sky_motion_grid_to_motion_vectors,
    sky_motion_to_pixel,
)
from .search import DEFAULT_WORK_BUDGET, search, shift_and_combine
from .shifting import SUPPORT_THRESHOLD, shift_image, shift_variance

__all__ = [
    "DEFAULT_PIXEL_SCALE_ARCSEC_PER_PIXEL",
    "DEFAULT_PRIOR_RADIUS_ARCSEC_PER_DAY",
    "DEFAULT_WORK_BUDGET",
    "SUPPORT_THRESHOLD",
    "Frame",
    "InvalidInputError",
    "MotionVector",
    "SearchResult",
    "ShiftCombineResult",
    "ShiftStackError",
    "WorkBudgetExceededError",
    "generate_sky_motion_grid",
    "pixel_motion_to_sky",
    "search",
    "shift_and_combine",
    "shift_image",
    "shift_variance",
    "sky_motion_grid_to_motion_vectors",
    "sky_motion_to_pixel",
]
