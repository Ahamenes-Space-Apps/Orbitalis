"""Pipeline-owned models.

Purpose: Field build parameters and run-state vocabulary.
Inputs: None at import.
Outputs: pydantic models used by the runner and (later) the API layer.
Called by: pipeline.runner, api routers.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RunState = Literal["queued", "running", "completed", "failed", "cancelled"]


class FieldParams(BaseModel):
    """Canonical parameters identifying a field (hashed into field_id)."""

    ra: float = Field(ge=-360.0, le=360.0)
    dec: float = Field(ge=-90.0, le=90.0)
    radius_deg: float = Field(default=0.05, gt=0.0, le=0.5)
    release: str | None = None
    preset: bool = False
    mjd_start: float | None = None
    mjd_end: float | None = None
    # Optional detector restriction (1-6). Filters FrameRefs after TAP
    # discovery but before any cutout download — the expensive step. Used by
    # demo presets to bake only the band a frame_window will keep.
    detectors: list[int] | None = None
    # When true, cutouts and the common grid are padded outward by
    # ``max_search_rate * baseline + margin`` so a candidate's full motion
    # track stays inside every frame (shift-and-stack edge completeness).
    # Part of field identity only when enabled, so existing field ids are
    # unchanged (see PipelineRunner.field_id_for).
    search_pad: bool = False

    @field_validator("ra")
    @classmethod
    def _wrap_ra(cls, v: float) -> float:
        # RA is periodic: -350 and +10 are the same meridian, so canonical
        # params (and therefore field_id) must not depend on the input range.
        return v % 360.0

    def id_dict(self) -> dict:
        """Params dict used for ``field_id`` hashing.

        ``search_pad=False`` is omitted so the default-off flag does not
        perturb field ids minted before the option existed; ``True`` stays
        in the hash because padded fields hold different data.
        """
        dumped = self.model_dump(mode="json")
        if not self.search_pad:
            dumped.pop("search_pad", None)
        return dumped


__all__ = ["FieldParams", "RunState"]
