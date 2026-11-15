"""Per-IP rate limit and global daily token budget.

Purpose: Enforce mechanical cost controls for the assistant (SDD §6.10, D-13).
Inputs: Request client IP, settings limits, token usage estimates.
Outputs: Allow/deny decisions with ORB-AS-002 (rate limit) or ORB-AS-003 (daily cap).
Called by: assistant chat endpoint (AS.04).

Design notes (AS.04 review):
- The daily budget is a **single global counter**, not per-IP: D-13 asks for
  one global daily cap and per-IP budgets are trivially bypassed by rotating
  the client-controlled X-Forwarded-For header.
- The per-IP rate-limit table is bounded: stale entries are pruned so the
  dict cannot grow without limit under IP spoofing.
- Token accounting is an estimate (chars/4 over input+output). Provider
  ``usage`` fields are not yet consumed by the stream adapter; revisit when
  a provider exposes them reliably.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

from orbitalis.settings import Settings

_MAX_TRACKED_IPS = 4096


@dataclass
class _RateLimitState:
    """Per-IP sliding-window request timestamps (seconds since epoch)."""

    requests: list[float] = field(default_factory=list)


@dataclass
class _DailyBudgetState:
    """Global daily token counter with UTC-midnight reset."""

    tokens_used: int = 0
    reset_ts: float = 0.0


class AssistantLimiter:
    """In-memory per-IP rate limiter and global daily budget enforcer.

    Thread-safety: asyncio is single-threaded; dict operations are atomic
    for single operations. ``asyncio.Lock`` guards compound read-modify-write.
    """

    def __init__(self, settings: Settings) -> None:
        self._rpm = max(1, settings.assistant_rpm)
        self._daily_cap: int | None = settings.assistant_daily_cap
        self._max_tool_iters = settings.assistant_max_tool_iters
        self._max_output_tokens = settings.assistant_max_output_tokens
        self._rate_limits: dict[str, _RateLimitState] = defaultdict(_RateLimitState)
        self._daily_budget = _DailyBudgetState()
        self._lock = asyncio.Lock()

    @property
    def rpm(self) -> int:
        return self._rpm

    @property
    def daily_cap(self) -> int | None:
        return self._daily_cap

    @property
    def max_tool_iters(self) -> int:
        return self._max_tool_iters

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

    def _now(self) -> float:
        return time.time()

    def _window_start(self) -> float:
        return self._now() - 60.0

    def _daily_reset_ts(self) -> float:
        """Next UTC midnight as epoch seconds."""
        now = self._now()
        # UTC midnight of next day
        import datetime

        dt = datetime.datetime.fromtimestamp(now, tz=datetime.UTC)
        next_midnight = (dt + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return next_midnight.timestamp()

    def _prune_rate_limits(self, cutoff: float) -> None:
        """Drop IPs with no requests inside the window; hard-cap the table."""
        stale = [
            ip
            for ip, st in self._rate_limits.items()
            if not st.requests or st.requests[-1] <= cutoff
        ]
        for ip in stale:
            del self._rate_limits[ip]
        if len(self._rate_limits) > _MAX_TRACKED_IPS:
            # Evict the oldest-touched entries first.
            by_age = sorted(self._rate_limits.items(), key=lambda kv: kv[1].requests[-1])
            for ip, _ in by_age[: len(self._rate_limits) - _MAX_TRACKED_IPS]:
                del self._rate_limits[ip]

    async def check_rate_limit(self, client_ip: str) -> None:
        """Raise if the client has exceeded the per-minute request limit.

        Error: ``ORB-AS-002`` with message "Too many questions; try again in a minute."
        """
        async with self._lock:
            cutoff = self._window_start()
            self._prune_rate_limits(cutoff)
            state = self._rate_limits[client_ip]
            state.requests = [ts for ts in state.requests if ts > cutoff]
            if len(state.requests) >= self._rpm:
                raise AssistantRateLimitError(
                    "Too many questions; try again in a minute.",
                    detail={"retry_after_seconds": 60},
                )
            state.requests.append(self._now())

    def _budget_state(self) -> _DailyBudgetState:
        state = self._daily_budget
        if state.reset_ts == 0.0 or state.reset_ts <= self._now():
            state.tokens_used = 0
            state.reset_ts = self._daily_reset_ts()
        return state

    async def check_daily_budget(self, client_ip: str, tokens: int) -> None:
        """Raise if adding ``tokens`` would exceed the global daily cap.

        ``client_ip`` is accepted for call-site compatibility but the cap is
        global (D-13): per-IP budgets are bypassable via header spoofing.

        Error: ``ORB-AS-003`` with message "The daily assistant limit is used up;
        it resets tomorrow."
        """
        if self._daily_cap is None:
            return
        async with self._lock:
            state = self._budget_state()
            if state.tokens_used + tokens > self._daily_cap:
                raise AssistantDailyBudgetError(
                    "The daily assistant limit is used up; it resets tomorrow.",
                    detail={"reset_at_utc": state.reset_ts},
                )
            # No reservation is taken here: record_usage() adds the actual
            # estimate once per completed request, so a check+record pair
            # cannot double-count (AS.04 review).

    async def record_usage(self, client_ip: str, tokens: int) -> None:
        """Record token usage after a successful request (no limit check)."""
        if self._daily_cap is None:
            return
        async with self._lock:
            self._budget_state().tokens_used += tokens

    def get_usage(self, client_ip: str) -> tuple[int, int | None, float]:
        """Return (tokens_used, daily_cap, reset_timestamp) — global counter."""
        if self._daily_cap is None:
            return 0, None, 0.0
        state = self._daily_budget
        return state.tokens_used, self._daily_cap, state.reset_ts


class AssistantRateLimitError(Exception):
    """Raised when per-IP rate limit is exceeded (ORB-AS-002)."""

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.code = "ORB-AS-002"


class AssistantDailyBudgetError(Exception):
    """Raised when the global daily token budget is exceeded (ORB-AS-003)."""

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.code = "ORB-AS-003"


__all__ = [
    "AssistantDailyBudgetError",
    "AssistantLimiter",
    "AssistantRateLimitError",
]
