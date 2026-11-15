"""Semaphore, retries, circuit breaker and single-flight for IRSA calls.

Purpose: Shared throttling primitives used by the gateway client.
Inputs: None at import.
Outputs: ``Limiter`` combining concurrency cap, retry policy, breaker and
single-flight dedup of identical in-flight requests.
Called by: gateway.client.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-open probe after cooldown.

    States: closed (counting failures) → open (cooldown, fail fast) →
    half-open (exactly one probe request allowed; success closes, failure
    reopens for another cooldown). ``is_open`` is side-effect free; call
    ``allow()`` before each request so the probe slot is claimed.
    """

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._failures = 0
        self._open_until = 0.0
        self._probe_in_flight = False

    @property
    def is_open(self) -> bool:
        if self._failures < self.threshold:
            return False
        if time.monotonic() < self._open_until:
            return True
        # Cooldown elapsed: open only while the single probe is in flight.
        return self._probe_in_flight

    def allow(self) -> bool:
        """Claim permission for one request; marks the half-open probe."""
        if self.is_open:
            return False
        if self._failures >= self.threshold:
            # Cooldown elapsed and no probe outstanding: this request is it.
            self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0
        self._probe_in_flight = False

    def abandon_probe(self) -> None:
        """Release the half-open probe slot without verdict (non-infra error)."""
        self._probe_in_flight = False

    def record_failure(self) -> None:
        if self._probe_in_flight:
            # Probe failed: reopen for a fresh cooldown.
            self._probe_in_flight = False
            self._failures = self.threshold
            self._open_until = time.monotonic() + self.cooldown_s
            return
        self._failures += 1
        if self._failures >= self.threshold:
            self._open_until = time.monotonic() + self.cooldown_s


class Limiter:
    """Concurrency cap + circuit breaker + single-flight + retry policy."""

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        max_retries: int = 2,
        retry_waits_s: tuple[float, ...] = (1.0, 3.0),
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 30.0,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self.max_retries = max_retries
        self.retry_waits_s = retry_waits_s
        self.breaker = CircuitBreaker(breaker_threshold, breaker_cooldown_s)
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def run(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        retryable: Callable[[BaseException], bool],
    ) -> T:
        """Single-flight ``fn`` under the semaphore with retries and breaker."""
        task = self._inflight.get(key)
        if task is not None and not task.done():
            return await asyncio.shield(task)
        task = asyncio.create_task(self._execute(fn, retryable))
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight.pop(key, None)

    async def _execute(
        self, fn: Callable[[], Awaitable[T]], retryable: Callable[[BaseException], bool]
    ) -> T:
        if not self.breaker.allow():
            from orbitalis.gateway.errors import CircuitOpenError

            raise CircuitOpenError("IRSA circuit breaker open")
        # Did *this* request claim the half-open probe? Only the claimant may
        # release the slot — a closed-path request must not clear a probe
        # owned by a concurrent caller.
        claimed_probe = self.breaker._probe_in_flight
        attempts = self.max_retries + 1
        last_exc: BaseException | None = None
        try:
            for attempt in range(attempts):
                async with self._sem:
                    try:
                        result = await fn()
                    except BaseException as exc:
                        last_exc = exc
                        # Only infrastructure failures (the same class the retry
                        # policy retries: timeouts, 5xx, transport errors) count
                        # toward the breaker. Client errors (4xx), validation
                        # failures and cancellations must not trip it.
                        counts = retryable(exc)
                        if not counts:
                            # Not an IRSA-health signal (4xx, validation,
                            # cancel): release the probe slot, no verdict.
                            if claimed_probe:
                                self.breaker.abandon_probe()
                            raise
                        if attempt == attempts - 1:
                            self.breaker.record_failure()
                            raise
                        # retry outside the semaphore slot
                    else:
                        self.breaker.record_success()
                        return result
                wait = self.retry_waits_s[min(attempt, len(self.retry_waits_s) - 1)]
                await asyncio.sleep(wait * (0.5 + random.random()))
        except BaseException:
            # Cancellation during the retry sleep or semaphore wait escapes
            # without a breaker verdict — release the probe slot so the
            # breaker cannot stay half-open forever. No-op when a verdict
            # was already recorded (success/failure clear the flag too).
            if claimed_probe:
                self.breaker.abandon_probe()
            raise
        assert last_exc is not None
        raise last_exc


__all__ = ["CircuitBreaker", "Limiter"]
