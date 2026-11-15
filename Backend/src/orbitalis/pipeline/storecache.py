"""Store-backed cache adapters for the gateway's injected cache protocols.

Purpose: Bridge ``store`` repositories to the gateway's ``TapCache`` protocol
and provide cutout lookup for the pipeline. Lives in ``pipeline`` because
``gateway`` must not import ``store`` (import-linter contract).
Inputs: ``Database`` handle.
Outputs: ``StoreTapCache``; cutout key helpers.
Called by: pipeline.runner, main.py composition root.
"""

from __future__ import annotations

from typing import Any

from orbitalis.store import Database, cache_get, cache_put, get_cutout, put_cutout


class StoreTapCache:
    """``TapCache`` protocol implementation over the ``tap_cache`` table."""

    def __init__(self, db: Database, ttl_s: int = 6 * 3600) -> None:
        self._db = db
        self._ttl_s = ttl_s

    async def get(self, key: str) -> Any | None:
        return await cache_get(self._db, "tap_cache", key)

    async def put(self, key: str, payload: Any, ttl_s: int | None = None) -> None:
        await cache_put(self._db, "tap_cache", key, payload, ttl_s=ttl_s or self._ttl_s)


def cutout_key(frame_ref: str, ra: float, dec: float, size_deg: float) -> str:
    """Deterministic cutout cache key (SDD §7.4: frame_ref + center + size)."""
    return f"{frame_ref}|{ra:.6f},{dec:.6f}|{size_deg:.4f}"


async def get_cached_cutout(db: Database, key: str) -> bytes | None:
    row = await get_cutout(db, key)
    return row["fits_blob"] if row else None


async def store_cutout(db: Database, key: str, fits_blob: bytes) -> None:
    await put_cutout(db, key, fits_blob)


__all__ = [
    "StoreTapCache",
    "cutout_key",
    "get_cached_cutout",
    "store_cutout",
]
