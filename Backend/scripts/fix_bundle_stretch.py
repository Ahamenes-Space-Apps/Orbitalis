"""D-26/CORE.06a bundle repair: one background-subtracted stretch per field.

Purpose: bundled fields baked before the pipeline fix carry two different
``meta.stretch`` parameter sets (a provisional frame-1 fit, then a second
fit after a build restart — ``StretchState`` is process memory). Candidate
frame cutouts render with ``meta.stretch``, so inconsistent params show
inconsistent cutouts, and raw-data params bottom-clip low-background
frames.

For every ``demo_bundle/<field>/`` this script:

1. Loads each frame's ``array_blob`` (``data`` + ``mask``).
2. Computes the per-frame background as the median of valid pixels and
   subtracts it (display-only; the stored arrays are untouched).
3. Fits ONE ``StretchParams`` on the pooled residuals of *all* the field's
   frames. Note this differs slightly from the live pipeline, which fits
   the provisional stretch on frame 1 and freezes after ``fix_n`` frames —
   a rebake may shift the look a little; both are residual-space fits.
4. Rewrites ``manifest.json``: every frame gets the shared
   ``meta.stretch`` plus ``meta.background_level_mjy_sr`` so the cutout
   endpoint subtracts the same background before stretching.

Idempotent: re-running reproduces the same manifest. The ``.npz`` frame
payloads are never modified.

Usage::

    uv run python scripts/fix_bundle_stretch.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from orbitalis_core.stretch import StretchParams, fit_stretch

BUNDLE = Path(__file__).resolve().parent.parent / "demo_bundle"


def _frame_residual(path: Path) -> tuple[np.ndarray, float]:
    """Return (background-subtracted 2-D array, background level)."""
    with np.load(path) as z:
        data = z["data"].astype(np.float64)
        mask = z["mask"].astype(bool) if "mask" in z.files else np.isfinite(data)
    valid = mask & np.isfinite(data)
    vals = data[valid]
    if vals.size == 0:
        raise ValueError(f"{path} has no valid pixels")
    bg = float(np.median(vals))
    return data - bg, bg


def _params_dict(params: StretchParams) -> dict:
    return {
        "lo": params.lo,
        "hi": params.hi,
        "softness": params.softness,
        "lo_percentile": params.lo_percentile,
        "hi_percentile": params.hi_percentile,
        "n_pixels": params.n_pixels,
    }


def fix_field(field_dir: Path) -> tuple[int, float, float]:
    """Rewrite one field's manifest; returns (n_frames, lo, hi)."""
    manifest_path = field_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    residuals: list[np.ndarray] = []
    backgrounds: dict[str, float] = {}
    for fr in manifest["frames"]:
        res, bg = _frame_residual(field_dir / fr["file"])
        residuals.append(res)
        backgrounds[str(fr["frame_ref"])] = bg

    params = fit_stretch(residuals)
    pdict = _params_dict(params)
    for fr in manifest["frames"]:
        meta = fr.setdefault("meta", {})
        meta["stretch"] = dict(pdict)
        meta["background_level_mjy_sr"] = backgrounds[str(fr["frame_ref"])]
        meta["display"] = "background_subtracted"

    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return len(residuals), params.lo, params.hi


def main() -> None:
    fields = sorted(p for p in BUNDLE.iterdir() if p.is_dir() and (p / "manifest.json").is_file())
    if not fields:
        raise SystemExit(f"no demo bundle fields under {BUNDLE}")
    for field_dir in fields:
        n, lo, hi = fix_field(field_dir)
        print(f"{field_dir.name}: {n} frames, stretch lo={lo:.4g} hi={hi:.4g}")


if __name__ == "__main__":
    main()
