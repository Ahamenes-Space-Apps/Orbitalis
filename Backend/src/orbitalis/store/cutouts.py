"""Cutout repository and eviction.

Purpose: CRUD for ``cutouts`` (native-grid FITS cutouts, size-capped LRU).
Inputs: ``Database`` handle plus blob bytes.
Outputs: Rows as dicts.
Called by: gateway cutout cache, pipeline, api frame PNGs.
"""

from __future__ import annotations

import os

from orbitalis.store.db import Database

_DB_MAX_BYTES = int(os.environ.get("ORB_DB_MAX_BYTES", str(400 * 1024 * 1024)))


async def evict_if_needed(db: Database) -> None:
    """Enforce the DB size cap after writes (SDD §7.4, default 400 MB)."""
    await evict_to_budget(db, _DB_MAX_BYTES)


async def put_cutout(db: Database, cutout_key: str, fits_blob: bytes) -> None:
    async with db.write_tx() as conn:
        await conn.execute(
            "INSERT INTO cutouts (cutout_key, fits_blob, bytes) VALUES (?,?,?) "
            "ON CONFLICT(cutout_key) DO UPDATE SET fits_blob=excluded.fits_blob, "
            "bytes=excluded.bytes, fetched_at=datetime('now')",
            (cutout_key, fits_blob, len(fits_blob)),
        )
    await evict_if_needed(db)


async def get_cutout(db: Database, cutout_key: str) -> bytes | None:
    conn = await db.read()
    try:
        cur = await conn.execute("SELECT fits_blob FROM cutouts WHERE cutout_key=?", (cutout_key,))
        row = await cur.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


async def cutout_size_bytes(db: Database) -> int:
    conn = await db.read()
    try:
        cur = await conn.execute("SELECT COALESCE(SUM(bytes),0) FROM cutouts")
        return (await cur.fetchone())[0]
    finally:
        await conn.close()


async def evict_to_budget(db: Database, max_bytes: int) -> dict[str, int]:
    """ST-04 eviction: oldest cutouts first, then frames of non-preset fields.

    Payload size is approximated by cutout ``bytes`` plus frame
    ``LENGTH(array_blob)`` (frames carry no per-row timestamp, so 'oldest'
    frames means frames of the oldest non-preset fields by ``created_at``).
    Preset fields and their dependent rows are never touched.
    """
    removed = {"cutouts": 0, "frames": 0}
    async with db.write_tx() as conn:
        cur = await conn.execute("SELECT COALESCE(SUM(bytes),0) FROM cutouts")
        size = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT COALESCE(SUM(LENGTH(array_blob) + COALESCE(LENGTH(png_blob),0)),0) FROM frames"
        )
        size += (await cur.fetchone())[0]
        if size <= max_bytes:
            return removed
        cur = await conn.execute("SELECT cutout_key, bytes FROM cutouts ORDER BY fetched_at")
        for key, nbytes in await cur.fetchall():
            if size <= max_bytes:
                break
            await conn.execute("DELETE FROM cutouts WHERE cutout_key=?", (key,))
            size -= nbytes or 0
            removed["cutouts"] += 1
        # Evict whole non-preset fields (oldest first), never single frames:
        # a partially evicted field would silently lose frames. Groups go with
        # their field so group_frames cannot dangle. Fields still running are
        # never evicted — a build in progress must not lose its frames
        # mid-write.
        cur = await conn.execute(
            "SELECT f.field_id, COALESCE(SUM(LENGTH(fr.array_blob)),0) "
            "FROM fields f LEFT JOIN frames fr ON fr.field_id = f.field_id "
            "WHERE f.preset = 0 AND f.status != 'running' "
            "GROUP BY f.field_id ORDER BY f.created_at"
        )
        for fid, nbytes in await cur.fetchall():
            if size <= max_bytes:
                break
            await conn.execute("DELETE FROM groups WHERE field_id=?", (fid,))
            await conn.execute("DELETE FROM frames WHERE field_id=?", (fid,))
            await conn.execute("DELETE FROM candidates WHERE field_id=?", (fid,))
            await conn.execute("DELETE FROM fields WHERE field_id=?", (fid,))
            size -= nbytes or 0
            removed["frames"] += 1
    return removed


__all__ = ["cutout_size_bytes", "evict_to_budget", "get_cutout", "put_cutout"]
