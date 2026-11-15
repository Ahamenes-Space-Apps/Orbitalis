"""Fallback LLM client: try providers in order until one streams content.

Purpose: Keep the assistant alive when a provider is rate-limited or down
(AS.05 ops hardening). A fallback is only attempted while the current
stream has emitted nothing — once tokens/tool calls flow, the stream is
committed and a later error passes through honestly.
Inputs: Ordered ``(LLMClient, model)`` pairs; each provider keeps its own
model because base URLs and model names differ across providers.
Outputs: ``LLMStreamChunk`` values; the last provider's error chunk if all
fail.
Called by: assistant composition root (``api/assistant.py``), tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from orbitalis.assistant.llm import LLMClient, LLMMessage, LLMStreamChunk


class FallbackLLMClient:
    """``LLMClient`` that walks an ordered provider list per request."""

    def __init__(self, providers: list[tuple[LLMClient, str | None]]) -> None:
        if not providers:
            raise ValueError("FallbackLLMClient requires at least one provider")
        self._providers = providers

    @property
    def degraded(self) -> bool:
        """True only when every provider's breaker is open."""
        return all(bool(getattr(c, "degraded", False)) for c, _ in self._providers)

    async def aclose(self) -> None:
        for client, _ in self._providers:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        # ``model`` from the caller is ignored: each provider was configured
        # with the model its endpoint actually serves.
        last_error: LLMStreamChunk | None = None
        for client, provider_model in self._providers:
            emitted = False
            saw_chunk = False
            async for chunk in client.stream(
                messages, tools, max_tokens=max_tokens, model=provider_model
            ):
                saw_chunk = True
                if chunk.type == "error" and not emitted:
                    last_error = chunk
                    break  # try the next provider before any content flowed
                emitted = True
                yield chunk
            if not saw_chunk:
                continue  # empty stream is a provider failure — try the next
            if emitted or chunk.type != "error":
                return  # content flowed or stream completed — stop here
        if last_error is not None:
            yield last_error


__all__ = ["FallbackLLMClient"]
