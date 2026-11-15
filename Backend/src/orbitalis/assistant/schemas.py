"""Assistant-owned models for read-only tools (AS.03, FR-AS-02) and
chat streaming (AS.04, FR-AS-01/03).

Purpose: Typed request/response shapes for assistant read-only tools and
streaming chat endpoint.
Inputs: Validated tool arguments, store read results, chat messages.
Outputs: JSON-serializable, assistant-facing views and SSE events.
Called by: ``assistant.tools``, ``assistant.service``, API chat endpoint.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CandidateStatus = Literal["unconfirmed"]


class CandidatePosition(BaseModel):
    """Distinct candidate position (never mixed with observed/predicted)."""

    ra: float
    dec: float
    kind: Literal["candidate"] = "candidate"


class SkyMotion(BaseModel):
    """Canonical sky motion (μE, μN) in arcseconds/day (SDD D-17)."""

    mu_e_arcsec_per_day: float
    mu_n_arcsec_per_day: float


class ModelScore(BaseModel):
    """Model prediction score; never a bare number or calibrated confidence."""

    value: float
    model_version: str | None = None
    kind: Literal["model_prediction"] = "model_prediction"


class CandidateFrameRef(BaseModel):
    """Minimal per-frame observation reference (no blobs)."""

    model_config = ConfigDict(extra="allow")

    frame_ref: str | None = None


class CandidateView(BaseModel):
    """Assistant-facing candidate view (read-only, no stack PNG blob)."""

    candidate_id: str
    job_id: str
    field_id: str
    status: CandidateStatus = "unconfirmed"
    position: CandidatePosition
    motion: SkyMotion | dict[str, Any] | None = None
    stack_snr: float | None = None
    model_score: ModelScore | None = None
    predicted_category: str | None = None
    static_source: bool | None = None
    created_at: str | None = None
    frames: list[dict[str, Any]] | None = None
    background_variant: str | None = None


class ListCandidatesInput(BaseModel):
    """Input for ``list_candidates`` (field-scoped, store order)."""

    field_id: str = Field(min_length=1)
    job_id: str | None = None
    limit: int = Field(default=25, ge=1, le=200)
    include_frames: bool = False


class ListCandidatesResult(BaseModel):
    field_id: str
    candidates: list[CandidateView]
    count: int


class ExperimentTrialView(BaseModel):
    """One stored experiment trial (D-15 outcome; n_spurious is a count)."""

    trial_idx: int
    truth: dict[str, Any] | None = None
    outcome: Literal["recovered", "missed", "preexisting"]
    detection: dict[str, Any] | None = None
    position_error_px: float | None = None
    n_spurious: int | None = None


class ExperimentSummary(BaseModel):
    experiment_id: str
    job_id: str | None = None
    field_id: str
    params: dict[str, Any]
    metrics: dict[str, Any] | None = None
    label: str | None = None
    created_at: str | None = None


class ExperimentDetail(ExperimentSummary):
    trials: list[ExperimentTrialView] = Field(default_factory=list)


class ReadExperimentResultsInput(BaseModel):
    """Input for ``read_experiment_results`` (exactly one selector)."""

    experiment_id: str | None = Field(default=None, min_length=1)
    field_id: str | None = Field(default=None, min_length=1)


class ExperimentDetailResult(BaseModel):
    mode: Literal["experiment"] = "experiment"
    experiment: ExperimentDetail


class ExperimentListResult(BaseModel):
    mode: Literal["field"] = "field"
    field_id: str
    experiments: list[ExperimentSummary]
    count: int


class ExplainFlagsInput(BaseModel):
    """Input for ``explain_flags`` (pure flag-value explanation)."""

    value: int = Field(ge=0)


class FlagInfo(BaseModel):
    name: str
    bit: int
    value: int
    masked_by_default: bool
    allowed_by_default: bool
    description: str


class UnknownFlagBit(BaseModel):
    bit: int
    value: int


class ExplainFlagsResult(BaseModel):
    value: int
    known_flags_set: list[FlagInfo]
    unknown_bits_set: list[UnknownFlagBit]
    masked_by_default: bool
    allowed_exceptions: list[str]
    policy: str
    behavior: str
    notes: list[str]


# --- AS.04 Chat streaming models ---


class ChatContext(BaseModel):
    """Client-provided context identifiers for grounding.

    The client sends identifiers for what the user currently has selected.
    The server resolves them through tools so the model sees real, current data.
    """

    field_id: str | None = None
    frame_ref: str | None = None
    candidate_id: str | None = None
    job_id: str | None = None
    experiment_id: str | None = None


class ChatMessage(BaseModel):
    """Chat message accepted from the client (AS.04 review hardening).

    Only ``user``/``assistant`` text roles are accepted: client-supplied
    ``system`` or ``tool`` messages could override the grounding rules or
    forge tool results. Tool-call payloads are provider-side state and are
    never accepted from the client. ``extra="forbid"`` rejects forged fields.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str | None = Field(default=None, max_length=4000)


class ChatRequest(BaseModel):
    """``POST /assistant/chat`` request body."""

    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    context: ChatContext = Field(default_factory=ChatContext)


class ChatTokenEvent(BaseModel):
    """``token`` SSE event: incremental text delta."""

    type: Literal["token"] = "token"
    delta: str


class ChatToolCallEvent(BaseModel):
    """``tool_call`` SSE event: tool name only (indicator)."""

    type: Literal["tool_call"] = "tool_call"
    name: str


class ChatDoneEvent(BaseModel):
    """``done`` SSE event: successful completion."""

    type: Literal["done"] = "done"
    status: Literal["completed"] = "completed"


class ChatErrorEvent(BaseModel):
    """``error`` SSE event: structured error."""

    type: Literal["error"] = "error"
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


# Union of all possible SSE event types
ChatEvent = ChatTokenEvent | ChatToolCallEvent | ChatDoneEvent | ChatErrorEvent


__all__ = [
    "CandidatePosition",
    "CandidateView",
    "ChatContext",
    "ChatDoneEvent",
    "ChatErrorEvent",
    "ChatEvent",
    "ChatMessage",
    "ChatRequest",
    "ChatToolCallEvent",
    "ChatTokenEvent",
    "ExperimentDetail",
    "ExperimentDetailResult",
    "ExperimentListResult",
    "ExperimentSummary",
    "ExperimentTrialView",
    "ExplainFlagsInput",
    "ExplainFlagsResult",
    "FlagInfo",
    "ListCandidatesInput",
    "ListCandidatesResult",
    "ModelScore",
    "ReadExperimentResultsInput",
    "SkyMotion",
    "UnknownFlagBit",
]
