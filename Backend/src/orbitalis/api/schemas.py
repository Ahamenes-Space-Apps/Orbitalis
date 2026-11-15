"""API-owned pydantic models (the OpenAPI contract).

Purpose: Request/response shapes for /api/v1 (SDD §8). Change these first;
the generated OpenAPI is the frontend contract.
Inputs: None at import.
Outputs: FieldCreate, JobSubmit, view models, envelope helpers.
Called by: api routers, tests.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# -- requests ---------------------------------------------------------------


class FieldCreate(BaseModel):
    """``POST /fields`` body (SDD §8.2). ``time_range`` is optional ISO-8601
    dates ``[start, end]`` converted to MJD before hitting the gateway."""

    # ra/dec are optional when ``preset`` names a curated field; explicit
    # values override the preset's (preset still supplies radius/release).
    ra: float | None = Field(default=None, ge=-360.0, le=360.0)
    dec: float | None = Field(default=None, ge=-90.0, le=90.0)
    radius_deg: float = Field(default=0.05, gt=0.0, le=0.5)
    release: str | None = None
    time_range: list[str] | None = Field(default=None, min_length=2, max_length=2)
    preset: str | None = None
    # Pad cutouts + common grid by max search motion over the frame baseline
    # (+5 px margin) so candidate tracks cannot leave the field. Off by
    # default; enabling it is part of the field's identity (new field_id).
    search_pad: bool = False


class JobSubmit(BaseModel):
    """``POST /jobs`` body (SDD §8.2b)."""

    kind: str = Field(min_length=1)
    field_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# -- views ------------------------------------------------------------------


class GridView(BaseModel):
    """U-05: the field's common tangent-plane grid (all frame PNGs share it)."""

    ra: float
    dec: float
    pixel_scale_arcsec: float
    rotation_deg: float = 0.0
    width: int
    height: int


class FieldView(BaseModel):
    field_id: str
    status: str
    params: dict[str, Any]
    grid: GridView | None = None
    frames_done: int = 0
    error_code: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class JobView(BaseModel):
    job_id: str
    kind: str
    field_id: str | None = None
    status: str
    progress: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class FrameMetaView(BaseModel):
    frame_ref: str
    field_id: str
    wavelength_um: float | None = None
    mjd: float | None = None
    detector: int | None = None
    release: str | None = None
    has_png: bool = False
    meta: dict[str, Any] | None = None


class GroupView(BaseModel):
    group_id: str
    rule: str | None = None
    frame_refs: list[str] = Field(default_factory=list)
    meta: dict[str, Any] | None = None


class ReleaseView(BaseModel):
    id: str
    label: str
    enabled: bool


class PresetView(BaseModel):
    name: str
    ra: float
    dec: float
    radius_deg: float | None = None
    release: str | None = None
    thumbnail_url: str | None = None
    why: str | None = None


class CapabilitiesView(BaseModel):
    ranking: dict[str, Any]
    assistant: dict[str, Any]
    classification: dict[str, Any]
    jobs: dict[str, Any]


HealthStatus = Literal["ok", "degraded", "offline"]


class PositionView(BaseModel):
    ra: float
    dec: float
    kind: Literal["candidate", "predicted", "observed"] = "candidate"


class RankValueView(BaseModel):
    """U-02: the single number the UI orders and displays."""

    value: float
    basis: Literal["model_score", "stack_snr"]


class ModelScoreView(BaseModel):
    """§8.5: a model score is always an object, never a bare number."""

    value: float
    model_version: str | None = None
    kind: Literal["model_prediction"] = "model_prediction"


class SkyMotionView(BaseModel):
    """Canonical sky-motion pair (SDD D-17).

    Cartesian tangent-plane components in arcseconds/day: ``mu_e`` eastward and
    ``mu_n`` northward. Not a rate plus position angle.
    """

    mu_e_arcsec_per_day: float = Field(
        ...,
        description="Eastward sky motion in arcseconds/day",
        allow_inf_nan=False,
    )
    mu_n_arcsec_per_day: float = Field(
        ...,
        description="Northward sky motion in arcseconds/day",
        allow_inf_nan=False,
    )


class CandidateSummary(BaseModel):
    candidate_id: str
    status: Literal["unconfirmed"] = "unconfirmed"
    position: PositionView
    motion: SkyMotionView | None = None
    stack_snr: float | None = None
    model_score: ModelScoreView | None = None
    rank_value: RankValueView | None = None
    ordinal: int | None = Field(
        default=None,
        description="U-12: stable 1-based rank within the candidate's job "
        "(stack_snr order), independent of the requested list sort.",
    )
    background_variant: str | None = Field(
        default=None,
        description="Background/static variant of the producing job (D-24).",
    )
    snr_calibrated: bool = Field(
        default=False,
        description="False: stack_snr is relative, not sigma-calibrated "
        "(variance understates noise ~2-3x; see D-24).",
    )
    predicted_category: str | None = None
    static_source: bool | None = Field(
        default=None,
        description="Provisional D-24 heuristic flag: likely a stationary "
        "source rather than a mover. None = not evaluated.",
    )
    created_at: str | None = None


class CandidateDetail(CandidateSummary):
    job_id: str | None = None
    field_id: str
    reference_mjd: float | None = Field(
        default=None,
        description="U-04: shift-stack reference epoch (earliest frame MJD); "
        "``ra``/``dec`` and ``motion`` are valid at this time.",
    )
    frames: list[dict[str, Any]] = Field(default_factory=list)


class ExperimentView(BaseModel):
    experiment_id: str
    job_id: str | None = None
    field_id: str
    params: dict[str, Any]
    metrics: dict[str, Any] | None = None
    label: str | None = None
    created_at: str | None = None
    trials: list[dict[str, Any]] | None = None


class OverlayItem(BaseModel):
    """U-08 overlay entry. ``path`` is present for moving known objects.

    Hypothesis items may carry ``shape`` (``{"type": "box"|"circle", ...}``)
    and ``contains_field`` — whether the field centre lies inside the
    published region. ``shape``/``contains_field`` are ``None`` for
    note-only items. Every hypothesis item is a published probability
    region, never an observation.
    """

    id: str
    name: str | None = None
    type: str | None = None
    ra: float | None = None
    dec: float | None = None
    path: list[dict[str, Any]] | None = None
    shape: dict[str, Any] | None = None
    contains_field: bool | None = None
    citation: str | None = None
    as_of: str | None = None
    detail: str | None = None


class OverlayView(BaseModel):
    kind: str
    items: list[OverlayItem] = Field(default_factory=list)
    source: str | None = None
    as_of: str | None = None


class SpectrumView(BaseModel):
    """§8.5 honesty: an estimate with data, or insufficient_data — never a guess.

    ``points`` are wavelength bins (µm) with median aperture flux across the
    frames sharing that band — a quick-look SED, not a calibrated spectrum.
    """

    status: Literal["ok", "insufficient_data"]
    reason: str | None = None
    points: list[dict[str, Any]] | None = None
    method: dict[str, Any] | None = None


class PhotometryPointView(BaseModel):
    """One frame's quick-look measurement at a source position.

    ``x``/``y``/``ra``/``dec`` are the *measured* centroid on the common
    grid and are ``null`` unless ``detected`` is true — a sub-threshold
    centroid is a noise peak, not a position. ``flux_mjy`` is the aperture
    sum times the pixel solid angle; it and ``upper_limit_mjy`` are
    reported even without a detection (quick-look, no aperture correction —
    see ``method``).
    """

    frame_ref: str
    mjd: float | None = None
    wavelength_um: float | None = None
    detector: int | None = None
    x: float | None = None
    y: float | None = None
    ra: float | None = None
    dec: float | None = None
    predicted_x: float | None = None
    predicted_y: float | None = None
    flux_mjy: float | None = None
    flux_err_mjy: float | None = None
    upper_limit_mjy: float | None = Field(
        default=None,
        description="3-sigma upper limit on flux_mjy; present whenever a "
        "flux error could be measured, including non-detections.",
    )
    snr: float | None = None
    detected: bool = False
    aperture_coverage: float | None = Field(
        default=None,
        description="Fraction of expected aperture pixels that are finite; "
        "detections require >= method.min_aperture_coverage.",
    )
    masked_bits: list[str] | None = Field(
        default=None,
        description="Flag-bit names (e.g. BLOOM, OVERFLOW) found on masked "
        "pixels inside the aperture; explains a low aperture_coverage. "
        "Null when the aperture is fully covered or no flags plane exists.",
    )
    background_mjy_sr: float | None = None


class LightCurveView(BaseModel):
    """Per-frame centroid + aperture photometry for one source.

    ``kind`` is ``fixed`` (a sky position queried on ``/fields``) or
    ``candidate`` (a stored candidate's predicted track). ``method``
    carries the measurement parameters and the honesty note — this is not
    calibrated photometry.
    """

    kind: Literal["fixed", "candidate"]
    reference: dict[str, Any]
    method: dict[str, Any]
    points: list[PhotometryPointView] = Field(default_factory=list)


# -- envelopes ---------------------------------------------------------------


def ok(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Success envelope ``{data, meta}`` (SDD §8.1)."""
    return {"data": data, "meta": meta or {}}


__all__ = [
    "CapabilitiesView",
    "CandidateDetail",
    "CandidateSummary",
    "ExperimentView",
    "FieldCreate",
    "FieldView",
    "FrameMetaView",
    "GridView",
    "GroupView",
    "HealthStatus",
    "JobSubmit",
    "JobView",
    "LightCurveView",
    "OverlayItem",
    "OverlayView",
    "PhotometryPointView",
    "RankValueView",
    "PositionView",
    "PresetView",
    "ReleaseView",
    "SkyMotionView",
    "SpectrumView",
    "ok",
]
