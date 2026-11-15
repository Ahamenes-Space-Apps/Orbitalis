"""Read-only assistant tools over store reads and flag definitions (AS.03).

Purpose: Implement ``list_candidates``, ``read_experiment_results`` and
``explain_flags`` (Roadmap AS.03, FR-AS-02) as strictly read-only operations.
Inputs: Narrow read dependencies (Database for store reads) or a flag integer.
Outputs: Typed, assistant-facing dicts/models; no binary blobs, no mutations.
Called by: Future assistant tool-calling orchestration (AS.01/AS.04).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from orbitalis.assistant.schemas import (
    CandidatePosition,
    CandidateView,
    ExperimentDetail,
    ExperimentDetailResult,
    ExperimentListResult,
    ExperimentSummary,
    ExperimentTrialView,
    ExplainFlagsResult,
    FlagInfo,
    ListCandidatesInput,
    ListCandidatesResult,
    ModelScore,
    ReadExperimentResultsInput,
    SkyMotion,
    UnknownFlagBit,
)
from orbitalis.store.candidates import list_candidates as store_list_candidates
from orbitalis.store.db import Database
from orbitalis.store.experiments import get_experiment as store_get_experiment
from orbitalis.store.experiments import list_experiments as store_list_experiments
from orbitalis.store.jobs import get_job as store_get_job
from orbitalis_core.flags import ALLOWED_BITS, KNOWN_BITS
from orbitalis_core.flags.bits import FLAG_BITS

# Source-grounded concise descriptions (DS spec §8 / Explanatory Supplement
# Table 16, pipeline R7) used only to explain known flag bits.
_FLAG_DESCRIPTIONS: dict[str, str] = {
    "TRANSIENT": (
        "Transient event during exposure (e.g. cosmic-ray hit); can be triggered by "
        "charge spill from very bright stars."
    ),
    "OVERFLOW": "Overflow threshold reached (about half of full-well saturation).",
    "SUR_ERROR": (
        "Onboard data-handling error; combined with TRANSIENT or OVERFLOW can mark an early event."
    ),
    "NONFUNC": "Permanently unusable pixel.",
    "DICHROIC": "Low-throughput corners of detectors 3 and 4 (dichroic region).",
    "MISSING_DATA": "Data lost before ingest.",
    "HOT": "Noisy or spurious pixel in this exposure.",
    "COLD": "Unresponsive or abnormally low-response pixel in this exposure.",
    "FULLSAMPLE": "Full sample history available (informational).",
    "PHANMISS": "Phantom-pixel correction could not be applied.",
    "NONLINEAR": "No reliable nonlinearity correction.",
    "PERSIST": "Persistent charge from an earlier bright exposure.",
    "OUTLIER": "Flagged by the outlier-detection module.",
    "CROSSTALK": "Electrical crosstalk (types 1 and 2).",
    "SOURCE": (
        "Pixel mapped to a known source from the reference or solar-system "
        "catalogue (label, not a quality defect)."
    ),
    "GHOST": "Optical ghost of a bright source inside the frame.",
    "GHOST_FPA": (
        "Ghost/FPA flag (unused per DS spec Table 16 note); masked by SDD default "
        "policy except SOURCE/FULLSAMPLE."
    ),
    "GHOST_EXT": "Ghost of a bright source outside the field of view.",
    "STREAK": "Satellite streak.",
    "BLOOM": "Source blooming.",
    "SNOWBALL": "Very energetic cosmic-ray or decay event.",
    "HALO": "Halo around a transient; use with caution.",
    "SATELLITE_HALO": "Halo around a satellite streak; not recommended for science use.",
}

_DEFAULT_MASK_POLICY = (
    "Default mask masks every flag bit except SOURCE (bit 21, value 2,097,152) "
    "and FULLSAMPLE (bit 12, value 4,096)."
)
_MASK_BEHAVIOR = (
    "Masked pixels are NaN in scientific arrays and alpha 0 in PNG "
    "visualization, never zero-filled values."
)


class CandidateStoreReader(Protocol):
    """Narrow read-only dependency for candidate listing."""

    async def __call__(
        self,
        db: Database,
        field_id: str,
        *,
        job_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...


class ExperimentGetReader(Protocol):
    async def __call__(self, db: Database, experiment_id: str) -> dict[str, Any] | None: ...


class ExperimentListReader(Protocol):
    async def __call__(self, db: Database, field_id: str) -> list[dict[str, Any]]: ...


class JobGetReader(Protocol):
    """Narrow read-only dependency for job lookup (background_variant)."""

    async def __call__(self, db: Database, job_id: str) -> dict[str, Any] | None: ...


def _model_score(row: dict[str, Any]) -> ModelScore | None:
    value = row.get("model_score")
    if value is None:
        return None
    return ModelScore(value=float(value), model_version=row.get("model_version"))


def _motion(row: dict[str, Any]) -> SkyMotion | dict[str, Any] | None:
    motion = row.get("motion")
    if motion is None:
        return None
    if isinstance(motion, dict):
        keys = {"mu_e_arcsec_per_day", "mu_n_arcsec_per_day"}
        if keys.issubset(motion):
            try:
                return SkyMotion(
                    mu_e_arcsec_per_day=float(motion["mu_e_arcsec_per_day"]),
                    mu_n_arcsec_per_day=float(motion["mu_n_arcsec_per_day"]),
                )
            except (TypeError, ValueError):
                return dict(motion)
        return dict(motion)
    return None


def _candidate_view(
    row: dict[str, Any],
    *,
    include_frames: bool = True,
    background_variant: str | None = None,
) -> CandidateView:
    static = row.get("static_source")
    if static is not None:
        static = bool(static)
    frames = row.get("frames")
    if frames is not None and not isinstance(frames, list):
        frames = None
    if not include_frames:
        frames = None
    return CandidateView(
        candidate_id=str(row["candidate_id"]),
        job_id=str(row["job_id"]),
        field_id=str(row["field_id"]),
        status="unconfirmed",
        position=CandidatePosition(ra=float(row["ra"]), dec=float(row["dec"])),
        motion=_motion(row),
        stack_snr=(float(row["stack_snr"]) if row.get("stack_snr") is not None else None),
        model_score=_model_score(row),
        predicted_category=row.get("predicted_category"),
        static_source=static,
        created_at=row.get("created_at"),
        frames=frames,
        background_variant=background_variant,
    )


async def list_candidates(
    db: Database,
    field_id: str,
    *,
    job_id: str | None = None,
    limit: int = 25,
    include_frames: bool = False,
    _list: (
        CandidateStoreReader | Callable[..., Awaitable[list[dict[str, Any]]]]
    ) = store_list_candidates,
    _get_job: (
        JobGetReader | Callable[[Database, str], Awaitable[dict[str, Any] | None]]
    ) = store_get_job,
) -> ListCandidatesResult:
    """List stored candidates for a field (read-only, stack_snr order).

    Defaults to the top ``limit`` candidates so the model context cannot be
    overflowed by a busy field; ``include_frames`` is opt-in because per-frame
    detail is large. Each candidate carries its job's ``background_variant``
    alongside ``stack_snr`` — under difference imaging (default) the score is
    relative, not a calibrated significance.
    """
    params = ListCandidatesInput(
        field_id=field_id, job_id=job_id, limit=limit, include_frames=include_frames
    )
    rows = await _list(db, params.field_id, job_id=params.job_id, limit=params.limit)
    variants: dict[str, str | None] = {}
    for row in rows:
        jid = row.get("job_id")
        if jid is not None and jid not in variants:
            job = await _get_job(db, str(jid))
            result = (job or {}).get("result") or {}
            variants[str(jid)] = result.get("background_variant")
    candidates = [
        _candidate_view(
            row,
            include_frames=params.include_frames,
            background_variant=variants.get(str(row.get("job_id"))),
        )
        for row in rows
    ]
    return ListCandidatesResult(
        field_id=params.field_id, candidates=candidates, count=len(candidates)
    )


def _trial_view(row: dict[str, Any]) -> ExperimentTrialView:
    return ExperimentTrialView(
        trial_idx=int(row["trial_idx"]),
        truth=row.get("truth"),
        outcome=row["outcome"],
        detection=row.get("detection"),
        position_error_px=row.get("position_error_px"),
        n_spurious=row.get("n_spurious"),
    )


def _experiment_summary(row: dict[str, Any]) -> ExperimentSummary:
    return ExperimentSummary(
        experiment_id=str(row["experiment_id"]),
        job_id=row.get("job_id"),
        field_id=str(row["field_id"]),
        params=row.get("params") or {},
        metrics=row.get("metrics"),
        label=row.get("label"),
        created_at=row.get("created_at"),
    )


async def read_experiment_results(
    db: Database,
    *,
    experiment_id: str | None = None,
    field_id: str | None = None,
    _get: (
        ExperimentGetReader | Callable[[Database, str], Awaitable[dict[str, Any] | None]]
    ) = store_get_experiment,
    _list: (
        ExperimentListReader | Callable[[Database, str], Awaitable[list[dict[str, Any]]]]
    ) = store_list_experiments,
) -> ExperimentDetailResult | ExperimentListResult | None:
    """Read stored experiment results (read-only).

    Roadmap AS.03 name: ``read_experiment_results``. SDD §6.10 refers to a
    related read operation as ``get_experiment_metrics``; this implementation
    uses the roadmap name and does not add a duplicate model-facing tool.

    - ``experiment_id``: returns detailed result incl. trials (or ``None`` if
      missing).
    - ``field_id``: returns experiment summaries for the field (empty list if
      none). Exactly one selector must be provided.
    """
    params = ReadExperimentResultsInput(experiment_id=experiment_id, field_id=field_id)
    if (params.experiment_id is None) == (params.field_id is None):
        raise ValueError("provide exactly one of experiment_id or field_id")

    if params.experiment_id is not None:
        row = await _get(db, params.experiment_id)
        if row is None:
            return None
        summary = _experiment_summary(row)
        trials = [_trial_view(t) for t in (row.get("trials") or [])]
        detail = ExperimentDetail(**summary.model_dump(), trials=trials)
        return ExperimentDetailResult(experiment=detail)

    rows = await _list(db, params.field_id or "")
    summaries = [_experiment_summary(r) for r in rows]
    return ExperimentListResult(
        field_id=params.field_id or "",
        experiments=summaries,
        count=len(summaries),
    )


def _validate_flag_value(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("flag value must be a non-negative integer")
    if not isinstance(value, int):
        raise ValueError("flag value must be a non-negative integer")
    if value < 0:
        raise ValueError("flag value must be a non-negative integer")
    return value


def explain_flags(value: int) -> ExplainFlagsResult:
    """Explain a SPHEREx FLAGS integer value (pure, read-only)."""
    ivalue = _validate_flag_value(value)
    known_set: list[FlagInfo] = []
    for name, bit in FLAG_BITS.items():
        if ivalue & bit:
            masked = (ivalue & bit) != 0 and (bit & ALLOWED_BITS) == 0
            allowed = not masked
            known_set.append(
                FlagInfo(
                    name=name,
                    bit=bit.bit_length() - 1,
                    value=bit,
                    masked_by_default=masked,
                    allowed_by_default=allowed,
                    description=_FLAG_DESCRIPTIONS.get(name, "Known SPHEREx flag bit."),
                )
            )

    unknown_bits: list[UnknownFlagBit] = []
    unknown_mask = ivalue & ~KNOWN_BITS
    bit_index = 0
    remaining = unknown_mask
    while remaining:
        if remaining & 1:
            unknown_bits.append(UnknownFlagBit(bit=bit_index, value=1 << bit_index))
        remaining >>= 1
        bit_index += 1

    return ExplainFlagsResult(
        value=ivalue,
        known_flags_set=known_set,
        unknown_bits_set=unknown_bits,
        masked_by_default=(ivalue & ALLOWED_BITS) != ivalue,
        allowed_exceptions=[
            "SOURCE (bit 21, value 2097152): allowed/unmasked by default; label for known sources.",
            "FULLSAMPLE (bit 12, value 4096): allowed/unmasked by default; informational.",
        ],
        policy=_DEFAULT_MASK_POLICY,
        behavior=_MASK_BEHAVIOR,
        notes=[
            "Unknown bits are not treated as allowed; they are masked by default.",
            "For arrays, mask application follows orbitalis_core.flags.mask.default_mask.",
        ],
    )


__all__ = [
    "explain_flags",
    "list_candidates",
    "read_experiment_results",
]
