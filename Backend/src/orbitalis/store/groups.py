"""Group repository (combined-frame groups, SDD §7.4).

Purpose: CRUD for ``groups`` and ``group_frames`` — coadds per grouping rule.
Inputs: ``Database`` handle plus group metadata.
Outputs: Group rows with their member frame refs.
Called by: pipeline combine step, api group endpoints.
"""

from __future__ import annotations

import json
from typing import Any

from orbitalis.store.db import Database


async def put_group(
    db: Database,
    field_id: str,
    group_id: str,
    *,
    rule: str | None = None,
    png_blob: bytes | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    async with db.write_tx() as conn:
        await conn.execute(
            "INSERT INTO groups (field_id, group_id, rule, png_blob, meta_json) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(field_id, group_id) DO UPDATE SET rule=excluded.rule, "
            "png_blob=excluded.png_blob, meta_json=excluded.meta_json",
            (
                field_id,
                group_id,
                rule,
                png_blob,
                json.dumps(meta, sort_keys=True) if meta is not None else None,
            ),
        )


async def add_group_frame(db: Database, field_id: str, group_id: str, frame_ref: str) -> None:
    async with db.write_tx() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO group_frames (field_id, group_id, frame_ref) VALUES (?,?,?)",
            (field_id, group_id, frame_ref),
        )


async def list_groups(db: Database, field_id: str) -> list[dict[str, Any]]:
    conn = await db.read()
    try:
        cur = await conn.execute(
            "SELECT * FROM groups WHERE field_id=? ORDER BY group_id", (field_id,)
        )
        cols = [d[0] for d in cur.description]
        groups = []
        for r in await cur.fetchall():
            g = dict(zip(cols, r, strict=True))
            g["meta"] = json.loads(g.pop("meta_json")) if g.get("meta_json") else None
            fcur = await conn.execute(
                "SELECT frame_ref FROM group_frames WHERE field_id=? AND group_id=? "
                "ORDER BY frame_ref",
                (field_id, g["group_id"]),
            )
            g["frame_refs"] = [row[0] for row in await fcur.fetchall()]
            groups.append(g)
        return groups
    finally:
        await conn.close()


__all__ = ["add_group_frame", "list_groups", "put_group"]
