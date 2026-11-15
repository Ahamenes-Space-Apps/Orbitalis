"""Candidate repository.

Purpose: CRUD for ``candidates`` — always ``unconfirmed`` (SDD §7.4, §6).
Inputs: ``Database`` handle plus candidate fields.
Outputs: Rows as dicts.
Called by: research search handler, api services, demo loader.
"""

from __future__ import annotations

import json
from typing import Any

from orbitalis.store.db import Database


async def put_candidate(
    db: Database,
    candidate_id: str,
    job_id: str,
    field_id: str,
    *,
    ra: float,
    dec: float,
    motion: dict[str, Any] | None = None,
    stack_snr: float | None = None,
    stack_png_blob: bytes | None = None,
    frames: list[dict[str, Any]] | None = None,
    model_score: float | None = None,
    model_version: str | None = None,
    predicted_category: str | None = None,
    static_source: bool | None = None,
    reference_mjd: float | None = None,
) -> None:
    """Insert a candidate. There is no status column: v1 has no confirmed state."""
    async with db.write_tx() as conn:
        await conn.execute(
            "INSERT INTO candidates (candidate_id, job_id, field_id, ra, dec, "
            "motion_json, stack_snr, stack_png_blob, frames_json, model_score, "
            "model_version, predicted_category, static_source, reference_mjd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET stack_snr=excluded.stack_snr, "
            "stack_png_blob=excluded.stack_png_blob, frames_json=excluded.frames_json, "
            "model_score=excluded.model_score, model_version=excluded.model_version, "
            "predicted_category=excluded.predicted_category, "
            "static_source=excluded.static_source, "
            "reference_mjd=excluded.reference_mjd",
            (
                candidate_id,
                job_id,
                field_id,
                ra,
                dec,
                json.dumps(motion, sort_keys=True) if motion is not None else None,
                stack_snr,
                stack_png_blob,
                json.dumps(frames, sort_keys=True) if frames is not None else None,
                model_score,
                model_version,
                predicted_category,
                None if static_source is None else int(static_source),
                reference_mjd,
            ),
        )


async def get_candidate(db: Database, candidate_id: str) -> dict[str, Any] | None:
    conn = await db.read()
    try:
        cur = await conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
        row = await cur.fetchone()
        return _cand_row(row, [d[0] for d in cur.description]) if row else None
    finally:
        await conn.close()


async def list_candidates(
    db: Database,
    field_id: str,
    *,
    job_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    conn = await db.read()
    try:
        sql = "SELECT * FROM candidates WHERE field_id=?"
        args: list[Any] = [field_id]
        if job_id is not None:
            sql += " AND job_id=?"
            args.append(job_id)
        sql += " ORDER BY stack_snr DESC NULLS LAST"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        cur = await conn.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [_cand_row(r, cols) for r in await cur.fetchall()]
    finally:
        await conn.close()


def _cand_row(row: tuple, cols: list[str]) -> dict[str, Any]:
    d = dict(zip(cols, row, strict=True))
    for key in ("motion_json", "frames_json"):
        if d.get(key) is not None:
            d[key.removesuffix("_json")] = json.loads(d.pop(key))
        else:
            d.pop(key, None)
    return d


__all__ = ["get_candidate", "list_candidates", "put_candidate"]
