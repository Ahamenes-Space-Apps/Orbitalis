"""Generate demo_bundle from live IRSA (VAL.05 / SDD §7.7).

For every preset in ``config/presets.yaml`` this script:

1. builds the field through the real pipeline (IRSA -> store),
2. runs one ``moving_object_search`` job,
3. runs one ``validation`` injection-and-recovery experiment,
4. exports frames + job results + candidates + experiment/trials into
   ``demo_bundle/<field_id>/`` via ``orbitalis.demo.export_field``.

Requires network access to IRSA; offline tests cover the bundle round-trip
instead. Run::

    uv run python scripts/bake_demo.py [--presets name1,name2]
        [--injections N] [--search-params JSON]

Search-shaping params (``frame_window``/``frame_refs`` window, motion grid,
thresholds, background variant) must reach BOTH jobs so they run on the same
frames. A preset may set a shared ``params:`` mapping (applied to both jobs)
plus job-specific ``search:``/``validation:`` overrides in
``config/presets.yaml``. Only terminal jobs are exported to the bundle.

The DB defaults to ``ORB_DATA_DIR``/``.data/orbitalis.db``; pass
``--db PATH`` to bake into a scratch database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orbitalis.demo import export_field  # noqa: E402
from orbitalis.eventbus import EventBus  # noqa: E402
from orbitalis.gateway import IrsaGateway  # noqa: E402
from orbitalis.jobs import JobRunner  # noqa: E402
from orbitalis.pipeline import FieldParams, PipelineRunner  # noqa: E402
from orbitalis.research.search import moving_object_search  # noqa: E402
from orbitalis.research.validation import validation  # noqa: E402
from orbitalis.settings import Settings  # noqa: E402
from orbitalis.store import get_field, get_job, open_database  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# VAL.05 experiment parameters per preset. Kept deliberately modest: each
# trial costs one full search, and ORB_JOB_TIMEOUT_S bounds the job.
DEFAULT_VALIDATION_PARAMS = {
    "n_injections": 20,
    "seed": 20261115,
    # SNR range, not flux: image-unit flux is meaningless on real SPHEREx
    # data; snr_min/max converts via flux_for_snr per frame variance.
    # 5-40 keeps the completeness curve visible: sub-5sigma sources are not
    # expected to recover, and the top of the range probes the bright end.
    "snr_min": 5.0,
    "snr_max": 40.0,
    "speed_min_arcsec_per_day": 1.0,
    "speed_max_arcsec_per_day": 8.0,
    "background_variant": "f",
    "noise_model": "per_frame",
    "label": "demo-bundle",
}

DEFAULT_SEARCH_PARAMS = {
    "background_variant": "f",
    "noise_model": "per_frame",
    "min_coverage_fraction": 0.5,
}


async def _wait_field(db, field_id: str, timeout_s: float = 900.0) -> str:
    for _ in range(int(timeout_s / 2)):
        row = await get_field(db, field_id)
        status = (row or {}).get("status")
        if status in ("completed", "failed", "cancelled"):
            return str(status)
        await asyncio.sleep(2.0)
    return "timeout"


async def _run_job(runner: JobRunner, db, kind: str, params: dict) -> dict:
    ref = await runner.submit(kind, params.get("field_id"), params)
    for _ in range(3000):
        row = await get_job(db, ref.job_id)
        if row and row["status"] in ("completed", "failed", "cancelled"):
            return row
        await asyncio.sleep(2.0)
    raise RuntimeError(f"job {ref.job_id} ({kind}) did not finish")


async def bake(
    *,
    presets: list[str] | None,
    injections: int | None,
    db_path: Path,
    bundle_dir: Path,
    search_params: dict | None,
) -> int:
    settings = Settings()
    db = await open_database(db_path)
    gateway = IrsaGateway(
        concurrency=settings.irsa_concurrency,
        tap_timeout_s=settings.irsa_tap_timeout_s,
        tap_url=settings.irsa_tap_url,
    )
    bus = EventBus()
    pipeline = PipelineRunner(db, gateway, bus)
    jobs = JobRunner(
        db,
        max_running=settings.jobs_max_running,
        queue_max=settings.jobs_queue_max,
        timeout_s=max(settings.job_timeout_s, 1800.0),
        settings=settings,
    )
    jobs.register("moving_object_search", moving_object_search)
    jobs.register("validation", validation)
    await jobs.sweep_stale()  # reused --db may hold jobs orphaned by a killed bake

    presets_data = yaml.safe_load((REPO_ROOT / "config" / "presets.yaml").read_text())
    wanted = set(presets or [])
    failures = 0
    try:
        for preset in presets_data.get("presets", []):
            name = preset["name"]
            if wanted and name not in wanted:
                continue
            print(f"[bake] preset {name}: building field", flush=True)
            field_over = preset.get("field") or {}
            params = FieldParams(
                ra=float(preset["ra"]),
                dec=float(preset["dec"]),
                radius_deg=float(preset.get("radius_deg") or 0.05),
                release=preset.get("release"),
                preset=True,
                mjd_start=field_over.get("mjd_start"),
                mjd_end=field_over.get("mjd_end"),
                detectors=field_over.get("detectors"),
            )
            field_id = await pipeline.ensure_field(params)
            status = await _wait_field(db, field_id)
            if status != "completed":
                print(f"[bake] {name}: field build ended {status}; skipping", flush=True)
                failures += 1
                continue

            # Search-shaping params apply to BOTH jobs so search and
            # validation always run on the same frames and grid. Precedence:
            # CLI --search-params < preset `params:` (shared) < preset
            # `search:`/`validation:` (job-specific).
            common = {
                **DEFAULT_SEARCH_PARAMS,
                **(search_params or {}),
                **(preset.get("params") or {}),
            }
            preset_search = preset.get("search") or {}
            preset_val = preset.get("validation") or {}

            search_p = {**common, **preset_search, "field_id": field_id}
            search_row = await _run_job(jobs, db, "moving_object_search", search_p)
            print(
                f"[bake] {name}: search -> {search_row['status']} "
                f"({(search_row.get('result') or {}).get('candidates_found')} candidates)",
                flush=True,
            )

            val_p = {**DEFAULT_VALIDATION_PARAMS, **common, **preset_val}
            if injections is not None:
                val_p["n_injections"] = injections
            val_p["field_id"] = field_id
            val_row = await _run_job(jobs, db, "validation", val_p)
            result = val_row.get("result") or {}
            print(
                f"[bake] {name}: validation -> {val_row['status']} recall={result.get('recall')}",
                flush=True,
            )
            if val_row["status"] != "completed":
                failures += 1
                continue

            out = await export_field(db, field_id, bundle_dir)
            print(f"[bake] {name}: exported {out}", flush=True)
    finally:
        await pipeline.close()
        await jobs.close()
        await gateway.aclose()
        await db.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--presets", default=None, help="comma-separated preset names")
    ap.add_argument("--injections", type=int, default=None)
    ap.add_argument("--db", type=Path, default=None, help="scratch DB path")
    ap.add_argument("--bundle-dir", type=Path, default=REPO_ROOT / "demo_bundle")
    ap.add_argument("--search-params", default=None, help="JSON object merged into search params")
    args = ap.parse_args()

    presets = [p.strip() for p in args.presets.split(",")] if args.presets else None
    search_params = json.loads(args.search_params) if args.search_params else None
    db_path = args.db or Settings().db_path
    failures = asyncio.run(
        bake(
            presets=presets,
            injections=args.injections,
            db_path=db_path,
            bundle_dir=args.bundle_dir,
            search_params=search_params,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
