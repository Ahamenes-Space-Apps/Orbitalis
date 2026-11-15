"""Gateway-owned models.

Purpose: Typed frame references returned by TAP discovery.
Inputs: None at import.
Outputs: ``FrameRef`` pydantic model.
Called by: gateway.tap, gateway.client, pipeline, store frame metadata.
"""

from __future__ import annotations

from pydantic import BaseModel


class FrameRef(BaseModel):
    """One SPHEREx plane/artifact pair discovered via TAP.

    ``frame_ref`` follows SDD §7.4: ``{release}.{obs_id}D{detector}``.
    Wavelengths/energy bounds are in metres (IRSA native); times are MJD days.
    """

    frame_ref: str
    release: str
    obs_id: str
    detector: int | None = None
    uri: str
    mjd_start: float
    mjd_end: float
    bandpass: str | None = None
    energy_min_m: float | None = None
    energy_max_m: float | None = None
    resolving_power: float | None = None
    pixel_scale_arcsec: float | None = None
    time_exposure_s: float | None = None
    provenance_version: str | None = None
    calibration_level: int | None = None
    product_type: str | None = None


__all__ = ["FrameRef"]
