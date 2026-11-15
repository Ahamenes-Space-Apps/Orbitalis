"""IRSA gateway client: TAP discovery with cache, limiter and single-flight.

Purpose: Async httpx client implementing ``find_frames`` per SDD §6.2 and
D-01/D-02 decisions. TAP results are cached in ``tap_cache`` (6 h TTL).
Inputs: ``Database`` (optional, for caching), httpx transport (tests: respx).
Outputs: ``list[FrameRef]``; typed ``GatewayError``s on failure.
Called by: pipeline field builder; ``fetch_cutout`` lands with GW.02.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Protocol

import httpx

from orbitalis.gateway.cutout import (
    IRSA_ALLOWED_HOSTS,
    IRSA_BASE_URL,
    check_allowed_host,
    cutout_url,
    trim_cutout,
)
from orbitalis.gateway.errors import (
    CutoutFetchError,
    GatewayTimeoutError,
    TapQueryError,
)
from orbitalis.gateway.limiter import Limiter
from orbitalis.gateway.schemas import FrameRef
from orbitalis.gateway.tap import (
    IRSA_TAP_URL,
    TAP_CACHE_TTL_S,
    TAP_TIMEOUT_S,
    build_adql,
    parse_votable,
)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _is_retryable(exc: BaseException) -> bool:
    """D-02: retry on timeout and 5xx only (not 4xx, not parse errors)."""
    if getattr(exc, "retryable", False):
        return True
    return isinstance(exc, (httpx.TimeoutException, GatewayTimeoutError))


class TapCache(Protocol):
    """Injected TAP result cache (store-backed adapter lives in pipeline)."""

    async def get(self, key: str) -> Any | None: ...
    async def put(self, key: str, payload: Any, ttl_s: int) -> None: ...


class IrsaGateway:
    """Async IRSA client. One instance per process; shares one Limiter."""

    def __init__(
        self,
        cache: TapCache | None = None,
        *,
        tap_url: str = IRSA_TAP_URL,
        client: httpx.AsyncClient | None = None,
        concurrency: int | None = None,
        tap_timeout_s: float | None = None,
        cache_ttl_s: int = TAP_CACHE_TTL_S,
        limiter: Limiter | None = None,
        allowed_hosts: str | None = None,
    ) -> None:
        self.cache = cache
        self.tap_url = tap_url
        self.cache_ttl_s = cache_ttl_s
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self.limiter = limiter or Limiter(
            max_concurrency=concurrency or _env_int("ORB_IRSA_CONCURRENCY", 4),
            max_retries=2,
            retry_waits_s=(1.0, 3.0),
            breaker_threshold=_env_int("ORB_IRSA_BREAKER_THRESHOLD", 5),
            breaker_cooldown_s=_env_float("ORB_IRSA_BREAKER_COOLDOWN_S", 30.0),
        )
        self._tap_timeout = tap_timeout_s or _env_float("ORB_IRSA_TAP_TIMEOUT_S", TAP_TIMEOUT_S)
        self.cutout_base = IRSA_BASE_URL
        self._cutout_timeout = _env_float("ORB_IRSA_CUTOUT_TIMEOUT_S", 60.0)
        hosts = allowed_hosts or _env_str("ORB_IRSA_ALLOWED_HOSTS", IRSA_ALLOWED_HOSTS)
        self._allowed_hosts = frozenset(h.strip().lower() for h in hosts.split(",") if h.strip())

    async def aclose(self) -> None:
        await self._client.aclose()

    async def find_frames(
        self,
        ra: float,
        dec: float,
        radius_deg: float = 0.05,
        *,
        release: str | None = None,
        mjd_start: float | None = None,
        mjd_end: float | None = None,
    ) -> list[FrameRef]:
        """Resolve a sky position to SPHEREx frame references via TAP.

        ``radius_deg`` is accepted for API compatibility; plane footprints are
        degrees across so coverage is tested at the position itself (the field
        radius is realised by cutout size).
        """
        adql = build_adql(ra, dec, release=release, mjd_start=mjd_start, mjd_end=mjd_end)
        cache_key = self._cache_key(adql)
        if self.cache is not None:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return [FrameRef(**row) for row in cached]
        rows = await self.limiter.run(
            cache_key,
            lambda: self._tap_query(adql),
            retryable=_is_retryable,
        )
        payload = [f.model_dump(mode="json") for f in rows]
        if self.cache is not None:
            await self.cache.put(cache_key, payload, ttl_s=self.cache_ttl_s)
        return rows

    async def _tap_query(self, adql: str) -> list[FrameRef]:
        form = {
            "REQUEST": "doQuery",
            "LANG": "ADQL",
            "FORMAT": "votable",
            "QUERY": adql,
        }
        try:
            resp = await self._client.post(self.tap_url, data=form, timeout=self._tap_timeout)
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(f"TAP query timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TapQueryError(f"TAP request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise TapQueryError(
                f"TAP returned HTTP {resp.status_code}", retryable=resp.status_code >= 500
            )
        try:
            return parse_votable(resp.content)
        except Exception as exc:
            raise TapQueryError(f"TAP response parse failed: {exc}") from exc

    @staticmethod
    def _cache_key(adql: str) -> str:
        return "tap:" + hashlib.sha256(adql.encode()).hexdigest()[:32]

    async def fetch_cutout(
        self,
        uri: str,
        ra: float,
        dec: float,
        size_deg: float = 0.1,
    ) -> bytes:
        """Fetch one cutout and return trimmed FITS bytes (D-27).

        The ~5 MB PSF HDU and other non-science extensions are dropped in
        memory; only IMAGE/FLAGS/VARIANCE/ZODI/WCS-WAVE plus the primary HDU
        are kept. Failures map to ORB-GW-002, timeouts to ORB-GW-004, an open
        breaker to ORB-GW-003.
        """
        url = cutout_url(self.cutout_base, uri, ra, dec, size_deg)
        check_allowed_host(url, self._allowed_hosts)  # GW-12: before any fetch
        raw = await self.limiter.run(
            f"cutout:{url}",
            lambda: self._get_cutout(url),
            retryable=_is_retryable,
        )
        return trim_cutout(raw)

    async def _get_cutout(self, url: str) -> bytes:
        try:
            resp = await self._client.get(url, timeout=self._cutout_timeout)
        except httpx.TimeoutException as exc:
            raise GatewayTimeoutError(f"cutout timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CutoutFetchError(
                f"cutout request failed: {type(exc).__name__}: {exc}", retryable=False
            ) from exc
        if resp.status_code >= 400:
            raise CutoutFetchError(
                f"cutout returned HTTP {resp.status_code}", retryable=resp.status_code >= 500
            )
        return resp.content

    async def health(self) -> dict[str, Any]:
        """Lightweight liveness for /health: TAP endpoint reachability."""
        try:
            resp = await self._client.get(self.tap_url, timeout=10.0)
            return {
                "irsa": "ok" if resp.status_code < 500 else "degraded",
                "status_code": resp.status_code,
            }
        except httpx.HTTPError as exc:
            return {"irsa": "down", "error": type(exc).__name__}


__all__ = ["IrsaGateway"]
