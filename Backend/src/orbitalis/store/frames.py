"""Frame repository.

Purpose: CRUD for ``frames`` rows; ``array_blob`` is required (ST-09).
Inputs: ``Database`` handle plus frame metadata and blobs.
Outputs: Rows as dicts.
Called by: pipeline (frame ingest), research handlers, demo loader.
"""

from __future__ import annotations

import io
import json
from typing import Any, NamedTuple

import numpy as np

from orbitalis.store.cutouts import evict_if_needed
from orbitalis.store.db import Database
from orbitalis.store.errors import StoreError


async def put_frame(
    db: Database,
    field_id: str,
    frame_ref: str,
    *,
    array_blob: bytes,
    wavelength_um: float | None = None,
    mjd: float | None = None,
    detector: int | None = None,
    png_blob: bytes | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Insert or replace a frame. ``array_blob`` is mandatory (ST-09).

    The blob must decode via :func:`decode_frame_array` — the D-29 contract
    rejects payloads that do not unpack rather than storing them (roadmap
    ST.02 follow-up).
    """
    if not array_blob:
        raise StoreError("frame requires a non-empty array_blob")
    try:
        decode_frame_array(array_blob)
    except Exception as exc:
        raise StoreError(f"array_blob does not decode: {exc}") from exc
    async with db.write_tx() as conn:
        await conn.execute(
            "INSERT INTO frames (field_id, frame_ref, wavelength_um, mjd, detector, "
            "png_blob, meta_json, array_blob) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(field_id, frame_ref) DO UPDATE SET "
            "wavelength_um=excluded.wavelength_um, mjd=excluded.mjd, "
            "detector=excluded.detector, png_blob=excluded.png_blob, "
            "meta_json=excluded.meta_json, array_blob=excluded.array_blob",
            (
                field_id,
                frame_ref,
                wavelength_um,
                mjd,
                detector,
                png_blob,
                json.dumps(meta, sort_keys=True) if meta is not None else None,
                array_blob,
            ),
        )
    await evict_if_needed(db)


async def get_frame(db: Database, field_id: str, frame_ref: str) -> dict[str, Any] | None:
    conn = await db.read()
    try:
        cur = await conn.execute(
            "SELECT * FROM frames WHERE field_id=? AND frame_ref=?", (field_id, frame_ref)
        )
        row = await cur.fetchone()
        return _frame_row(row, [d[0] for d in cur.description]) if row else None
    finally:
        await conn.close()


async def list_frames(db: Database, field_id: str) -> list[dict[str, Any]]:
    conn = await db.read()
    try:
        cur = await conn.execute("SELECT * FROM frames WHERE field_id=? ORDER BY mjd", (field_id,))
        cols = [d[0] for d in cur.description]
        return [_frame_row(r, cols) for r in await cur.fetchall()]
    finally:
        await conn.close()


class FrameBlob(NamedTuple):
    """Decoded ``frames.array_blob`` contents."""

    data: np.ndarray  # float32, NaN where masked
    mask: np.ndarray  # bool validity footprint
    variance: np.ndarray | None = None  # float32, same grid as data
    flags: np.ndarray | None = None  # int32 raw FLAGS, same grid as data


def encode_frame_array(
    data: np.ndarray,
    valid: np.ndarray | None = None,
    variance: np.ndarray | None = None,
    flags: np.ndarray | None = None,
) -> bytes:
    """Codec for ``frames.array_blob``: compressed arrays in one npz.

    ``data`` is float32 with NaN where masked; ``valid`` records the
    pre-NaN footprint. ``variance``/``flags`` are optional and must share
    ``data``'s shape when present.
    """
    arr = np.asarray(data, dtype=np.float32)
    mask = np.isfinite(arr) if valid is None else np.asarray(valid, dtype=bool)
    payload = {"data": arr, "mask": mask}
    if variance is not None:
        var = np.asarray(variance, dtype=np.float32)
        if var.shape != arr.shape:
            raise StoreError("variance must match data shape")
        payload["variance"] = var
    if flags is not None:
        fl = np.asarray(flags)
        if fl.shape != arr.shape:
            raise StoreError("flags must match data shape")
        payload["flags"] = fl
    buf = io.BytesIO()
    np.savez_compressed(buf, **payload)
    return buf.getvalue()


def decode_frame_array(blob: bytes) -> FrameBlob:
    """Inverse of :func:`encode_frame_array`."""
    with np.load(io.BytesIO(blob)) as z:
        keys = set(z.files)
        return FrameBlob(
            data=z["data"].astype(np.float32),
            mask=z["mask"].astype(bool),
            variance=z["variance"].astype(np.float32) if "variance" in keys else None,
            flags=z["flags"] if "flags" in keys else None,
        )


def _frame_row(row: tuple, cols: list[str]) -> dict[str, Any]:
    d = dict(zip(cols, row, strict=True))
    d["meta"] = json.loads(d.pop("meta_json")) if d.get("meta_json") else None
    return d


__all__ = [
    "FrameBlob",
    "decode_frame_array",
    "encode_frame_array",
    "get_frame",
    "list_frames",
    "put_frame",
]
