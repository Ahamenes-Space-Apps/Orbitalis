#!/usr/bin/env python3
"""X-03: timing benchmarks for shiftstack and stack combine.

Purpose: Record where the science path spends time before any optimisation
(X-03 exists so we benchmark BEFORE optimising). ``rank`` is a scaffold
(D-18/D-11 open) so there is nothing to time there yet — noted honestly.
Inputs: seeded synthetic frames on a common grid.
Outputs: printed timings (not assertions — CI stays fast).
Called by: ``uv run python scripts/bench_core.py``
"""

from __future__ import annotations

import time

import numpy as np

from orbitalis_core.shiftstack import Frame, MotionVector, search, shift_and_combine
from orbitalis_core.stack import combine

rng = np.random.default_rng(7)


def _frames(n: int, shape: tuple[int, int] = (128, 128)) -> list[Frame]:
    return [
        Frame(
            image=rng.normal(0, 1, shape),
            time=60000.0 + i * 0.5,
            variance=np.full(shape, 1.0),
            frame_id=f"f{i}",
        )
        for i in range(n)
    ]


def _time(fn, *args, **kw) -> float:
    t0 = time.perf_counter()
    fn(*args, **kw)
    return time.perf_counter() - t0


def main() -> None:
    print("== orbitalis_core benchmarks (single run, wall time) ==")
    for n in (8, 32):
        frames = _frames(n)
        vec = MotionVector(dx_px_per_day=1.0, dy_px_per_day=0.5)
        dt = _time(shift_and_combine, frames, vec)
        print(f"shift_and_combine  n_frames={n:3d} 128x128 : {dt * 1e3:8.1f} ms")

    # stack.combine on the same cube sizes
    for n in (8, 32):
        cube = [f.image for f in _frames(n)]
        dt = _time(combine, cube, method="mean")
        print(f"stack.combine(mean) n={n:3d} 128x128    : {dt * 1e3:8.1f} ms")

    # search over a small motion grid — the RS.03 hot path
    frames = _frames(8)
    vectors = [
        MotionVector(dx_px_per_day=dx, dy_px_per_day=dy)
        for dx in (-1.0, 0.0, 1.0)
        for dy in (-1.0, 0.0, 1.0)
    ]
    dt = _time(search, frames, vectors, min_coverage_fraction=0.5, work_budget=10**12)
    print(f"shiftstack.search   n_frames=8  9 vectors : {dt * 1e3:8.1f} ms")
    print("note: orbitalis_core.rank is a scaffold (D-18/D-11 open) — nothing to benchmark.")


if __name__ == "__main__":
    main()
