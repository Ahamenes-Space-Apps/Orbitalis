"""Gateway error types carrying SDD Appendix B codes.

Purpose: Typed errors for IRSA access failures.
Inputs: None at import.
Outputs: GatewayError subclasses; ``code`` is the ORB-GW-* string.
Called by: gateway modules (raise), api/errors.py (map to HTTP later).
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base gateway error; ``code`` follows SDD Appendix B."""

    code = "ORB-GW-001"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TapQueryError(GatewayError):
    """ORB-GW-001: IRSA TAP query failed."""

    code = "ORB-GW-001"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class CutoutFetchError(GatewayError):
    """ORB-GW-002: IRSA cutout fetch failed."""

    code = "ORB-GW-002"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class CircuitOpenError(GatewayError):
    """ORB-GW-003: circuit breaker open; failing fast."""

    code = "ORB-GW-003"


class GatewayTimeoutError(GatewayError):
    """ORB-GW-004: IRSA request timed out."""

    code = "ORB-GW-004"


__all__ = [
    "CircuitOpenError",
    "CutoutFetchError",
    "GatewayError",
    "GatewayTimeoutError",
    "TapQueryError",
]
