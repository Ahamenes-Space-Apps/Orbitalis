"""Planet X hypothesis overlay regions (SDD D-04).

Purpose: Load the curated published-region config and answer "is this sky
position inside the region?" for the hypothesis overlay. Regions are
hypotheses, never observations — the fixed disclaimer travels with every
item.
Inputs: ``config/hypothesis.yaml`` plus a point (ra, dec in degrees).
Outputs: region dicts and a wrap-aware containment test.
Called by: ``api.overlays``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

_HYPOTHESIS_FILE = "hypothesis.yaml"


def load_hypothesis(config_dir: Path) -> dict[str, Any]:
    """Load ``config/hypothesis.yaml``; ``{"regions": []}`` when absent."""
    path = config_dir / _HYPOTHESIS_FILE
    if not path.exists():
        return {"as_of": None, "scope_note": None, "note": None, "regions": []}
    data = yaml.safe_load(path.read_text()) or {}
    regions = [r for r in data.get("regions", []) if isinstance(r, dict) and "id" in r]
    return {
        "as_of": data.get("as_of"),
        "scope_note": data.get("scope_note"),
        "note": data.get("note"),
        "regions": regions,
    }


def _ra_delta_deg(ra: float, ra0: float) -> float:
    """Signed RA difference wrapped to [-180, 180) degrees."""
    return (ra - ra0 + 180.0) % 360.0 - 180.0


def _angular_sep_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation in degrees (small-angle safe)."""
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dp = p2 - p1
    dl = math.radians(_ra_delta_deg(ra2, ra1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))


def contains_point(shape: dict[str, Any] | None, ra: float, dec: float) -> bool | None:
    """Whether (ra, dec) lies inside ``shape``; ``None`` for note-only items.

    RA wrap-around is handled by wrapping the difference to [-180, 180):
    a box with ``ra_min > ra_max`` crosses 0h (e.g. RA < 2 or > 255).
    """
    if not shape:
        return None
    stype = shape.get("type")
    if stype == "box":
        ra_min, ra_max = float(shape["ra_min"]), float(shape["ra_max"])
        dec_min, dec_max = float(shape["dec_min"]), float(shape["dec_max"])
        if dec_min <= dec <= dec_max:
            # (ra - ra_min) % 360 in [0, width] handles both wrapped boxes
            # (ra_min > ra_max) and boxes wider than 180 deg in RA.
            width = (ra_max - ra_min) % 360.0
            return (ra - ra_min) % 360.0 <= width
        return False
    if stype == "circle":
        return _angular_sep_deg(ra, dec, float(shape["ra"]), float(shape["dec"])) <= float(
            shape["radius_deg"]
        )
    return None


def shape_center(shape: dict[str, Any] | None) -> tuple[float, float] | None:
    """Representative (ra, dec) for an overlay item — box/circle centre."""
    if not shape:
        return None
    if shape.get("type") == "box":
        ra_min, ra_max = float(shape["ra_min"]), float(shape["ra_max"])
        ra_c = (ra_min + ((ra_max - ra_min) % 360.0) / 2.0) % 360.0
        dec_c = (float(shape["dec_min"]) + float(shape["dec_max"])) / 2.0
        return (ra_c, dec_c)
    if shape.get("type") == "circle":
        return float(shape["ra"]), float(shape["dec"])
    return None


__all__ = ["contains_point", "load_hypothesis", "shape_center"]
