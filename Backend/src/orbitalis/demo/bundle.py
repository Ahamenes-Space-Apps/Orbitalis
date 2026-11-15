"""Demo bundle export and load (SDD §6.7, §7.7).

Purpose: ``scripts/bake_demo.py`` exports completed preset fields — frames,
search job results, candidates and one validation experiment — into
``demo_bundle/<field_id>/``; at startup ``load_bundle`` inserts the same
contents through the normal repositories so the Research Lab demos offline.

Bundle layout (per field)::

    <field_id>/manifest.json   field params, grid, status, frame metadata
    <field_id>/frames/<ref>.npz   encode_frame_array payload (the stored blob)
    <field_id>/jobs.json       completed job rows (search, validation, ...)
    <field_id>/candidates.json candidate rows (stack_png_blob base64 if present)
    <field_id>/experiments.json {"experiments": [...], "trials": [...]}

Note: SDD §7.7 originally said ``frames/<ref>.png``; the stored-frame contract
(D-29) needs the compressed array payload (image + mask + variance + flags),
so bundles carry the npz blob verbatim. PNG previews remain a frontend concern.

Inputs: ``Database`` handle, bundle directory path, field ids.
Outputs: written bundle files / load counts.
Called by: ``scripts/bake_demo.py`` (export), ``main._build_services`` (load).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from orbitalis.store import (
    Database,
    add_trial,
    get_experiment,
    get_field,
    get_frame,
    get_job,
    list_candidates,
    list_experiments,
    list_frames,
    list_jobs,
    put_candidate,
    put_experiment,
    put_frame,
    put_job,
    set_field_grid,
    set_field_status,
    update_job,
    upsert_field,
)

log = logging.getLogger(__name__)

_SAFE_REF = re.compile(r"[^A-Za-z0-9_.-]+")


def _frame_filename(frame_ref: str) -> str:
    safe = _SAFE_REF.sub("_", frame_ref)
    return f"{safe}.npz"


def _jsonable(value: Any) -> Any:
    """JSON-safe copy: bytes -> {"__b64__": ...}, tuples -> lists."""
    if isinstance(value, bytes):
        return {"__b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _dejsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) == {"__b64__"}:
            return base64.b64decode(value["__b64__"])
        return {k: _dejsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_dejsonable(v) for v in value]
    return value


async def export_field(db: Database, field_id: str, bundle_dir: Path | str) -> Path:
    """Write one field's bundle directory. Returns the field dir path."""
    bundle_dir = Path(bundle_dir)
    field = await get_field(db, field_id)
    if field is None:
        raise ValueError(f"field {field_id!r} not found")
    out = bundle_dir / field_id
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    jobs = [
        j
        for j in await list_jobs(db, field_id)
        if j.get("status") in ("completed", "failed", "cancelled")
    ]
    experiments = await list_experiments(db, field_id)
    exp_details = [await _experiment_with_trials(db, exp["experiment_id"]) for exp in experiments]

    # Export only frames the saved jobs actually consumed. A completed
    # search/validation without an explicit ref list used ALL stored frames,
    # so the bundle must keep them all (frame_window/frame_refs provenance
    # makes the common bake case a strict subset).
    used_refs: set[str] = set()
    unbounded = False
    for job in jobs:
        if job.get("status") != "completed" or job.get("kind") not in (
            "moving_object_search",
            "validation",
        ):
            continue
        params = job.get("params") or {}
        result = job.get("result") or {}
        refs = (result.get("frame_window") or {}).get("frame_refs") or params.get("frame_refs")
        if refs:
            used_refs.update(str(r) for r in refs)
        elif job.get("kind") == "moving_object_search":
            unbounded = True
    for detail in exp_details:
        refs = ((detail.get("params") or {}).get("frame_refs_used")) or []
        used_refs.update(str(r) for r in refs)

    frame_rows = await list_frames(db, field_id)
    if not unbounded and used_refs:
        frame_rows = [r for r in frame_rows if str(r.get("frame_ref")) in used_refs]
    manifest_frames = []
    for row in frame_rows:
        blob = row.get("array_blob")
        if not blob:
            raise ValueError(f"frame {row.get('frame_ref')} has no array_blob to export")
        fname = _frame_filename(str(row["frame_ref"]))
        (frames_dir / fname).write_bytes(bytes(blob))
        manifest_frames.append(
            {
                "frame_ref": row["frame_ref"],
                "file": f"frames/{fname}",
                "mjd": row.get("mjd"),
                "wavelength_um": row.get("wavelength_um"),
                "detector": row.get("detector"),
                "meta": row.get("meta"),
            }
        )

    manifest = {
        "field_id": field_id,
        "preset": bool(field.get("preset")),
        "params": field.get("params") or {},
        "grid": field.get("grid"),
        "status": field.get("status"),
        "frames": manifest_frames,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    (out / "jobs.json").write_text(
        json.dumps([_jsonable(j) for j in jobs], indent=2, sort_keys=True)
    )

    candidates = await list_candidates(db, field_id)
    (out / "candidates.json").write_text(
        json.dumps([_jsonable(c) for c in candidates], indent=2, sort_keys=True)
    )

    # list_experiments returns experiment rows only; trials ride along via
    # get_experiment per row (small N, bundle bake is offline anyway).
    all_trials = []
    exp_rows = []
    for exp, detail in zip(experiments, exp_details, strict=True):
        exp_rows.append({k: v for k, v in exp.items() if k != "trials"})
        all_trials.extend(detail.get("trials") or [])
    (out / "experiments.json").write_text(
        json.dumps(
            {"experiments": _jsonable(exp_rows), "trials": _jsonable(all_trials)},
            indent=2,
            sort_keys=True,
        )
    )
    return out


async def _experiment_with_trials(db: Database, experiment_id: str) -> dict[str, Any]:
    row = await get_experiment(db, experiment_id)
    return row or {}


async def load_bundle(db: Database, bundle_dir: Path | str) -> dict[str, int]:
    """Insert every ``<field_id>/`` bundle into the store. Idempotent.

    Uses the same repositories as live writes (SDD §6.7); fields are marked
    ``preset=true`` so eviction protects them. Returns insert counts.
    """
    bundle_dir = Path(bundle_dir)
    counts = {"fields": 0, "frames": 0, "jobs": 0, "candidates": 0, "experiments": 0, "trials": 0}
    if not bundle_dir.is_dir():
        return counts

    for field_dir in sorted(p for p in bundle_dir.iterdir() if p.is_dir()):
        manifest_path = field_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text())
        field_id = str(manifest["field_id"])

        await upsert_field(db, field_id, manifest.get("params") or {}, preset=True)
        if manifest.get("grid"):
            await set_field_grid(db, field_id, manifest["grid"])
        if manifest.get("status"):
            await set_field_status(db, field_id, str(manifest["status"]))
        counts["fields"] += 1

        for fr in manifest.get("frames") or []:
            frame_ref = str(fr["frame_ref"])
            if await get_frame(db, field_id, frame_ref) is not None:
                continue  # already loaded; startup reload stays cheap
            payload = (field_dir / str(fr["file"])).read_bytes()
            await put_frame(
                db,
                field_id,
                frame_ref,
                array_blob=payload,
                wavelength_um=fr.get("wavelength_um"),
                mjd=fr.get("mjd"),
                detector=fr.get("detector"),
                meta=fr.get("meta"),
            )
            counts["frames"] += 1

        jobs_path = field_dir / "jobs.json"
        if jobs_path.is_file():
            for job in json.loads(jobs_path.read_text()):
                job = _dejsonable(job)
                existing = await get_job(db, str(job["job_id"]))
                if existing is not None and existing.get("status") in (
                    "completed",
                    "failed",
                    "cancelled",
                ):
                    continue  # already loaded; bundle reload is a no-op
                await put_job(
                    db,
                    str(job["job_id"]),
                    str(job["kind"]),
                    job.get("params") or {},
                    field_id=field_id,
                )
                status = job.get("status")
                if status in ("completed", "failed", "cancelled"):
                    await update_job(
                        db,
                        str(job["job_id"]),
                        status=status,
                        progress=job.get("progress"),
                        result=job.get("result"),
                        error_code=job.get("error_code"),
                    )
                counts["jobs"] += 1

        cand_path = field_dir / "candidates.json"
        if cand_path.is_file():
            for cand in json.loads(cand_path.read_text()):
                cand = _dejsonable(cand)
                await put_candidate(
                    db,
                    str(cand["candidate_id"]),
                    str(cand.get("job_id") or ""),
                    field_id,
                    ra=float(cand["ra"]),
                    dec=float(cand["dec"]),
                    motion=cand.get("motion"),
                    stack_snr=cand.get("stack_snr"),
                    stack_png_blob=cand.get("stack_png_blob"),
                    frames=cand.get("frames"),
                    model_score=cand.get("model_score"),
                    model_version=cand.get("model_version"),
                    predicted_category=cand.get("predicted_category"),
                    static_source=cand.get("static_source"),
                )
                counts["candidates"] += 1

        exp_path = field_dir / "experiments.json"
        if exp_path.is_file():
            payload = json.loads(exp_path.read_text())
            for exp in payload.get("experiments") or []:
                exp = _dejsonable(exp)
                await put_experiment(
                    db,
                    str(exp["experiment_id"]),
                    exp.get("job_id"),
                    field_id,
                    params=exp.get("params") or {},
                    metrics=exp.get("metrics"),
                    label=exp.get("label"),
                )
                counts["experiments"] += 1
            for trial in payload.get("trials") or []:
                trial = _dejsonable(trial)
                await add_trial(
                    db,
                    str(trial["experiment_id"]),
                    int(trial["trial_idx"]),
                    truth=trial.get("truth"),
                    outcome=str(trial["outcome"]),
                    detection=trial.get("detection"),
                    position_error_px=trial.get("position_error_px"),
                    n_spurious=trial.get("n_spurious"),
                )
                counts["trials"] += 1

    log.info("demo bundle loaded from %s: %s", bundle_dir, counts)
    return counts


__all__ = ["export_field", "load_bundle"]
