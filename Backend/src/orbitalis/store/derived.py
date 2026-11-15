"""Derived-payload repository (diff images, spectra, classification).

Purpose: CRUD for ``derived`` rows keyed by (field_id, kind).
Inputs: ``Database`` handle plus blob bytes.
Outputs: Blob bytes or ``None``.
Called by: pipeline (diff/spectrum), research classification, api.
"""

from __future__ import annotations

from orbitalis.store.db import Database
from orbitalis.store.errors import StoreError

_KINDS = ("diff", "spectrum", "classification")


async def put_derived(db: Database, field_id: str, kind: str, payload_blob: bytes) -> None:
    if kind not in _KINDS:
        raise StoreError(f"invalid derived kind {kind!r}")
    async with db.write_tx() as conn:
        await conn.execute(
            "INSERT INTO derived (field_id, kind, payload_blob) VALUES (?,?,?) "
            "ON CONFLICT(field_id, kind) DO UPDATE SET payload_blob=excluded.payload_blob",
            (field_id, kind, payload_blob),
        )


async def get_derived(db: Database, field_id: str, kind: str) -> bytes | None:
    conn = await db.read()
    try:
        cur = await conn.execute(
            "SELECT payload_blob FROM derived WHERE field_id=? AND kind=?",
            (field_id, kind),
        )
        row = await cur.fetchone()
        return row[0] if row else None
    finally:
        await conn.close()


__all__ = ["get_derived", "put_derived"]
