"""LLMClient protocol and provider-neutral streaming types.

Purpose: Define the assistant LLM boundary (SDD §6.10, AS.01): a single
streaming protocol, shared message/chunk models, and typed LLM errors.
Inputs: Caller-built ``LLMMessage`` lists and OpenAI-style tool schemas.
Outputs: ``LLMStreamChunk`` values (token, tool_call, done, error) and
``AssistantLLMError`` subclasses.
Called by: assistant service (AS.03+), provider adapters, and tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class AssistantLLMError(Exception):
    """Base error for the LLM client boundary.

    ``code`` follows SDD Appendix B and is mapped to HTTP in one place
    (``orbitalis.errors``) when it reaches the API layer.
    """

    code: ClassVar[str] = "ORB-AS-004"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class LLMProviderUnavailableError(AssistantLLMError):
    """LLM client cannot be used because the provider is unavailable.

    Maps to ``ORB-AS-001``. The assistant service translates a disabled or
    unavailable assistant into this condition at the integration boundary
    (AS.03+); the adapter/client never silently pretends to work.
    """

    code = "ORB-AS-001"


class LLMProviderError(AssistantLLMError):
    """Provider request failed (auth, rate limit, server, malformed)."""

    code = "ORB-AS-004"


class LLMTimeoutError(AssistantLLMError):
    """Provider request timed out."""

    code = "ORB-AS-004"


class LLMToolCall(BaseModel):
    """One provider-independent tool invocation."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: str = Field(description="JSON-encoded arguments string as produced by the provider.")


class LLMMessage(BaseModel):
    """Provider-independent chat message."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[LLMToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class LLMStreamChunk(BaseModel):
    """One streamed LLM event.

    Exactly one semantic payload is expected per chunk:
    - ``token``: incremental text in ``content``.
    - ``tool_call``: a complete tool call in ``tool_call``.
    - ``done``: successful end of stream.
    - ``error``: provider failure carried as an ``AssistantLLMError`` in ``error``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: Literal["token", "tool_call", "done", "error"]
    content: str | None = None
    tool_call: LLMToolCall | None = None
    error: AssistantLLMError | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.type == "token" and self.content is None:
            raise ValueError("token chunk requires content")
        if self.type == "tool_call" and self.tool_call is None:
            raise ValueError("tool_call chunk requires tool_call")
        if self.type == "error" and self.error is None:
            raise ValueError("error chunk requires error")


@runtime_checkable
class LLMClient(Protocol):
    """Streaming, provider-neutral LLM client (SDD §6.10)."""

    def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Yield token/tool_call/done/error chunks for one completion."""
        ...


__all__ = [
    "AssistantLLMError",
    "LLMClient",
    "LLMMessage",
    "LLMProviderError",
    "LLMProviderUnavailableError",
    "LLMStreamChunk",
    "LLMTimeoutError",
    "LLMToolCall",
]
