"""CORE.06a / D-26 measurement: real SPHEREx background vs wavelength.

Purpose: quantify whether one frozen display stretch (fit on the first
``ORB_STRETCH_FIX_N`` frames) saturates or crushes later frames as
wavelength/background changes within a field. Runs entirely offline on
``demo_bundle/`` — the baked real-IRSA frames.

For every bundled field and frame this reports:

- wavelength (um), MJD, bandpass, detector
- robust background level: median and p16/p84 of valid pixels (MJy/sr)
- each frame's own stored ``StretchParams`` (what its PNG was rendered with —
  frames in one field can carry different stretches; see SDD D-26)
- per-frame saturation: fraction of valid pixels mapping to stretch output
  0.0 (``data <= lo``) or 1.0 (``data >= hi``) under that frame's params

Background vs wavelength is summarized per field as the min/max/median of
the per-frame median background and a Spearman correlation with
wavelength (scipy is already a core dep; ties are handled by
``scipy.stats.spearmanr``).

Usage::

    uv run python scripts/measure_core06a_stretch.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

BUNDLE = Path(__file__).resolve().parent.parent / "demo_bundle"


def _frame_arrays(path: Path) -> tuple[np.ndarray, np.ndarray]:
    with np.load(path) as z:
        data = z["data"].astype(np.float64)
        mask = z["mask"].astype(bool)
    return data, mask & np.isfinite(data)


def _stretch_output_stats(
    data: np.ndarray, valid: np.ndarray, lo: float, hi: float, softness: float
) -> tuple[float, float, float]:
    """Return (frac_at_0, frac_at_1, frac_in_01) under the frozen stretch."""
    vals = data[valid]
    if vals.size == 0:
        return float("nan"), float("nan"), float("nan")
    t = (vals - lo) / (hi - lo)
    out = np.clip(np.arcsinh(softness * t) / np.arcsinh(softness), 0.0, 1.0)
    return (
        float(np.mean(out <= 0.0)),
        float(np.mean(out >= 1.0)),
        float(np.mean((out > 0.0) & (out < 1.0))),
    )


def main() -> None:
    fields = sorted(p for p in BUNDLE.iterdir() if p.is_dir())
    if not fields:
        raise SystemExit(f"no demo bundle fields under {BUNDLE}")

    for field_dir in fields:
        manifest = json.loads((field_dir / "manifest.json").read_text())
        rows = []
        for fr in manifest["frames"]:
            path = field_dir / fr["file"]
            data, valid = _frame_arrays(path)
            vals = data[valid]
            if vals.size == 0:
                continue
            meta = fr.get("meta", {})
            rows.append(
                {
                    "fr": fr,
                    "data": data,
                    "valid": valid,
                    "vals": vals,
                    "wave": float(fr["wavelength_um"]),
                    "mjd": float(fr["mjd"]),
                    "detector": fr.get("detector"),
                    "bandpass": meta.get("bandpass"),
                    "stretch": meta.get("stretch"),
                    "background_level": meta.get("background_level_mjy_sr"),
                    "median": float(np.median(vals)),
                    "p1": float(np.percentile(vals, 1)),
                    "p999": float(np.percentile(vals, 99.9)),
                }
            )
        if not rows:
            continue

        waves = np.array([r["wave"] for r in rows])
        meds = np.array([r["median"] for r in rows])
        # NOTE: per-frame "median of valid pixels" is a robust image level,
        # not a pure sky background — a bright extended source (e.g. the Mira
        # at R Dra's centre) inflates it. Treat spreads as upper bounds.
        bandpasses = sorted({str(r["bandpass"]) for r in rows})

        print(f"\n=== {field_dir.name} — {len(rows)} frames ===")
        print(f"  bandpass(es): {bandpasses}")
        print(f"  wavelength: {waves.min():.3f} – {waves.max():.3f} um")
        print(
            f"  background median: {meds.min():.4g} – {meds.max():.4g} MJy/sr "
            f"(spread {meds.max() / max(meds.min(), 1e-30):.2f}x)"
        )
        if len(rows) >= 3 and np.ptp(waves) > 0:
            rho = spearmanr(waves, meds).statistic
            print(f"  median-background vs wavelength Spearman rho: {rho:+.2f}")

        stretches = [r["stretch"] for r in rows if r["stretch"]]
        if not stretches:
            print("  no stored stretch params — skipped saturation check")
            continue
        distinct = {(round(st["lo"], 6), round(st["hi"], 6)) for st in stretches}
        print(f"  distinct stored stretches in field: {len(distinct)}")
        for lo, hi in sorted(distinct):
            print(f"    lo={lo:.4g} hi={hi:.4g}")
        worst_lo = worst_hi = 0.0
        for r in rows:
            st = r["stretch"]
            if st is None:
                print(f"    wave={r['wave']:.3f} mjd={r['mjd']:.2f} NO STRETCH STORED")
                continue
            display = r["data"]
            bg = r.get("background_level")
            if bg is not None:
                display = display - float(bg)
            f0, f1, _fmid = _stretch_output_stats(
                display, r["valid"], st["lo"], st["hi"], st["softness"]
            )
            worst_lo, worst_hi = max(worst_lo, f0), max(worst_hi, f1)
            flag = ""
            if f0 > 0.5 or f1 > 0.5:
                flag = "  <-- >50% of pixels clipped"
            print(
                f"    wave={r['wave']:.3f} mjd={r['mjd']:.2f} det={r['detector']} "
                f"lo={st['lo']:.4g} bg_med={r['median']:.4g} "
                f"p1={r['p1']:.4g} p99.9={r['p999']:.4g} "
                f"clip0={f0:.3f} clip1={f1:.3f}{flag}"
            )
        print(f"  worst per-frame clipping: bottom {worst_lo:.3f}, top {worst_hi:.3f}")


if __name__ == "__main__":
    main()
