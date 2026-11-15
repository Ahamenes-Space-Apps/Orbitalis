#!/usr/bin/env python3
"""Re-run search + validation jobs on the STORED demo-bundle frames.

Purpose: ``scripts/bake_demo.py`` rebuilds fields from live IRSA, which is
unavailable or undesirable when only the job parameters changed (e.g. the
D-24 default ``background_variant`` E -> F). This script loads each bundle
field's manifest + frame blobs into a scratch DB — fields and frames ONLY,
never the old jobs/candidates/experiments — then re-runs
``moving_object_search`` and ``validation`` with the same parameter merge
as ``bake_demo`` and exports fresh bundle directories.

The frame blobs are byte-identical to the existing bundle, so the rebaked
fields hold exactly the same data; only job results differ. Field ids are
recomputed from ``FieldParams`` exactly as ``bake_demo`` builds them, so
the output layout and ids are unchanged.

Inputs: ``demo_bundle/`` (read), ``config/presets.yaml``, scratch DB.
Outputs: ``<out-dir>/<field_id>/`` bundle directories.
Called by::

    uv run python scripts/rebake_bundle_offline.py \
        [--presets a,b] [--injections N] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bake_demo import DEFAULT_SEARCH_PARAMS, DEFAULT_VALIDATION_PARAMS  # noqa: E402

from orbitalis.demo.bundle import export_field  # noqa: E402
from orbitalis.jobs import JobRunner  # noqa: E402
from orbitalis.pipeline.schemas import FieldParams  # noqa: E402
from orbitalis.research.search import moving_object_search  # noqa: E402
from orbitalis.research.validation import validation  # noqa: E402
from orbitalis.settings import Settings  # noqa: E402
from orbitalis.store import (  # noqa: E402
    get_job,
    open_database,
    put_frame,
    set_field_grid,
    set_field_status,
    upsert_field,
)
from orbitalis.store.fields import make_field_id  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "demo_bundle"


async def _load_fields_frames_only(db, bundle_dir: Path) -> dict[str, str]:
    """Insert manifest fields + frame blobs; returns field_id -> preset name.

    Deliberately skips jobs/candidates/experiments so ``export_field``
    writes a clean bundle containing only the jobs run below.
    """
    fid_to_dir: dict[str, str] = {}
    for field_dir in sorted(p for p in bundle_dir.iterdir() if p.is_dir()):
        manifest_path = field_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        field_id = str(manifest["field_id"])
        fid_to_dir[field_id] = field_dir.name
        await upsert_field(db, field_id, manifest.get("params") or {}, preset=True)
        if manifest.get("grid"):
            await set_field_grid(db, field_id, manifest["grid"])
        if manifest.get("status"):
            await set_field_status(db, field_id, str(manifest["status"]))
        for fr in manifest.get("frames") or []:
            payload = (field_dir / str(fr["file"])).read_bytes()
            await put_frame(
                db,
                field_id,
                str(fr["frame_ref"]),
                array_blob=payload,
                wavelength_um=fr.get("wavelength_um"),
                mjd=fr.get("mjd"),
                detector=fr.get("detector"),
                meta=fr.get("meta"),
            )
    return fid_to_dir


async def _run_job(jobs, db, kind: str, params: dict) -> dict:
    ref = await jobs.submit(kind, params.get("field_id"), params)
    for _ in range(3000):
        row = await get_job(db, ref.job_id)
        if row and row["status"] in ("completed", "failed", "cancelled"):
            return row
        await asyncio.sleep(2.0)
    raise RuntimeError(f"job {ref.job_id} ({kind}) did not finish")


async def rebake(
    *,
    presets: list[str] | None,
    injections: int | None,
    db_path: Path,
    bundle_dir: Path,
    out_dir: Path,
    search_params: dict | None,
) -> int:
    settings = Settings()
    db = await open_database(db_path)
    jobs = JobRunner(
        db,
        max_running=1,
        queue_max=10,
        timeout_s=max(settings.job_timeout_s, 1800.0),
        settings=settings,
    )
    jobs.register("moving_object_search", moving_object_search)
    jobs.register("validation", validation)
    await jobs.sweep_stale()

    presets_data = yaml.safe_load((REPO_ROOT / "config" / "presets.yaml").read_text())
    wanted = set(presets or [])
    failures = 0
    try:
        await _load_fields_frames_only(db, bundle_dir)
        for preset in presets_data.get("presets", []):
            name = preset["name"]
            if wanted and name not in wanted:
                continue
            # Field identity exactly as bake_demo builds it.
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
            field_id = make_field_id(params.id_dict())

            common = {
                **DEFAULT_SEARCH_PARAMS,
                **(search_params or {}),
                **(preset.get("params") or {}),
            }
            search_p = {**common, **(preset.get("search") or {}), "field_id": field_id}
            print(f"[rebake] {name}: search {field_id}", flush=True)
            search_row = await _run_job(jobs, db, "moving_object_search", search_p)
            print(
                f"[rebake] {name}: search -> {search_row['status']} "
                f"({(search_row.get('result') or {}).get('candidates_found')} candidates)",
                flush=True,
            )

            val_p = {
                **DEFAULT_VALIDATION_PARAMS,
                **common,
                **(preset.get("validation") or {}),
                "field_id": field_id,
            }
            if injections is not None:
                val_p["n_injections"] = injections
            print(f"[rebake] {name}: validation {field_id}", flush=True)
            val_row = await _run_job(jobs, db, "validation", val_p)
            result = val_row.get("result") or {}
            print(
                f"[rebake] {name}: validation -> {val_row['status']} recall={result.get('recall')}",
                flush=True,
            )
            if val_row["status"] != "completed":
                failures += 1
                continue

            out = await export_field(db, field_id, out_dir)
            print(f"[rebake] {name}: exported {out}", flush=True)
    finally:
        await jobs.close()
        await db.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--presets", default=None, help="comma-separated preset names")
    ap.add_argument("--injections", type=int, default=None)
    ap.add_argument("--db", type=Path, default=None, help="scratch DB path")
    ap.add_argument("--bundle-dir", type=Path, default=BUNDLE)
    ap.add_argument("--out-dir", type=Path, default=REPO_ROOT / "demo_bundle_rebaked")
    ap.add_argument("--search-params", default=None, help="JSON object merged into search params")
    args = ap.parse_args()

    presets = [p.strip() for p in args.presets.split(",")] if args.presets else None
    search_params = json.loads(args.search_params) if args.search_params else None
    db_path = args.db or Path(tempfile.mkdtemp(prefix="orbitalis_rebake_")) / "rebake.db"
    failures = asyncio.run(
        rebake(
            presets=presets,
            injections=args.injections,
            db_path=db_path,
            bundle_dir=args.bundle_dir,
            out_dir=args.out_dir,
            search_params=search_params,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
