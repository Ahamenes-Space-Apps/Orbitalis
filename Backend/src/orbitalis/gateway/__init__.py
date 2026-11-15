"""IRSA gateway public surface.

Purpose: Re-exports for the IRSA gateway (TAP discovery; cutouts in GW.02).
Inputs: None at import.
Outputs: IrsaGateway, FrameRef, gateway errors, limiter primitives.
Called by: pipeline, api services.
"""

from orbitalis.gateway.client import IrsaGateway
from orbitalis.gateway.cutout import (
    IRSA_BASE_URL,
    CutoutData,
    cutout_url,
    extract_cutout,
    trim_cutout,
)
from orbitalis.gateway.errors import (
    CircuitOpenError,
    CutoutFetchError,
    GatewayError,
    GatewayTimeoutError,
    TapQueryError,
)
from orbitalis.gateway.limiter import CircuitBreaker, Limiter
from orbitalis.gateway.schemas import FrameRef
from orbitalis.gateway.tap import IRSA_TAP_URL, build_adql, parse_votable

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CutoutData",
    "CutoutFetchError",
    "FrameRef",
    "GatewayError",
    "GatewayTimeoutError",
    "IRSA_BASE_URL",
    "IRSA_TAP_URL",
    "IrsaGateway",
    "Limiter",
    "TapQueryError",
    "build_adql",
    "cutout_url",
    "extract_cutout",
    "trim_cutout",
    "parse_votable",
]
