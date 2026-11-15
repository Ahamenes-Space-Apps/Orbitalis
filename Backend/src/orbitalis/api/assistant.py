"""Assistant chat endpoint (AS.04, FR-AS-01/03).

Purpose: ``POST /assistant/chat`` streaming chat with grounding, tools, and limits.
Inputs: ``/api/v1/assistant/chat`` requests with messages and context IDs.
Outputs: ``text/event-stream`` SSE with token, tool_call, done, error events.
Called by: api.router.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from orbitalis.api.deps import Services, services
from orbitalis.api.schemas import ok
from orbitalis.assistant import (
    AssistantLimiter,
    AssistantService,
    LLMMessage,
    assistant_readiness,
)
from orbitalis.assistant.knowledge import KnowledgePackMissingError
from orbitalis.assistant.llm import LLMClient
from orbitalis.assistant.providers.fallback import FallbackLLMClient
from orbitalis.assistant.providers.openai_compatible import (
    PROVIDER_BASE_URLS,
    OpenAICompatibleClient,
)
from orbitalis.assistant.schemas import ChatRequest
from orbitalis.errors import APIError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _get_client_ip(request: Request) -> str:
    """Return the connection peer address.

    ``X-Forwarded-For`` is deliberately NOT trusted: without a configured
    trusted-proxy list its first entry is client-controlled, which would let
    an attacker rotate IPs to bypass the per-IP rate limit (AS.04 review).
    Behind a real proxy, set ``request.client.host`` via the server's proxy
    handling instead of parsing headers here.
    """
    return request.client.host if request.client else "unknown"


def _convert_messages(chat_messages: list) -> list[LLMMessage]:
    """Convert API chat messages to LLMMessage protocol."""
    return [LLMMessage(role=m.role, content=m.content) for m in chat_messages]


def _resolve_base_url(settings) -> str:
    """Pick the provider endpoint: explicit override wins, else the map."""
    if settings.llm_base_url:
        return settings.llm_base_url
    provider = (settings.llm_provider or "").lower()
    return PROVIDER_BASE_URLS.get(provider, PROVIDER_BASE_URLS["openai"])


def _parse_llm_fallbacks(raw: str | None) -> list[dict[str, str]]:
    """Parse ``ORB_LLM_FALLBACKS`` JSON; invalid entries are skipped."""
    import json

    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ORB_LLM_FALLBACKS is not valid JSON; ignoring fallbacks")
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        required = ("api_key", "base_url", "model")
        if isinstance(item, dict) and all(item.get(k) for k in required):
            out.append(
                {
                    "api_key": str(item["api_key"]),
                    "base_url": str(item["base_url"]).rstrip("/"),
                    "model": str(item["model"]),
                }
            )
    return out


def _sse(event: dict) -> str:
    """Format one service event as an SSE frame."""
    import json

    return f"event: {event['type']}\ndata: {json.dumps(event['data'], separators=(',', ':'))}\n\n"


@router.post("/chat")
async def chat(
    request: Request,
    payload: ChatRequest,
    svc: Annotated[Services, Depends(services)],
) -> StreamingResponse:
    """Streaming chat endpoint (AS.04, FR-AS-01/03).

    - Validates request and context IDs
    - Enforces per-IP rate limit (ORB-AS-002) and global daily budget (ORB-AS-003)
    - Requires LLM provider configuration (ORB-AS-001 if unavailable)
    - Streams SSE events: token, tool_call, done, error
    - Uses read-only tools (list_candidates, read_experiment_results, explain_flags)
    - Bounded tool-call loop (assistant_max_tool_iters)
    """
    client_ip = _get_client_ip(request)

    # Build or retrieve assistant service from app state
    # We attach it to app.state for reuse across requests
    if not hasattr(request.app.state, "assistant_service"):
        # Lazy initialization of LLM client and assistant service
        settings = svc.settings
        ready, reason = assistant_readiness(settings)
        if not ready:
            raise APIError(
                "ORB-AS-001",
                "assistant is not available",
                detail={"reason": reason or "not configured"},
            )

        primary = OpenAICompatibleClient(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            base_url=_resolve_base_url(settings),
            # Free-tier providers cap tokens/minute; a tool-call round can
            # need to wait out a full window, so allow more retries than the
            # default short-backoff outage case.
            max_retries=3,
        )
        providers: list[tuple[LLMClient, str | None]] = [(primary, settings.llm_model)]
        for fb in _parse_llm_fallbacks(settings.llm_fallbacks):
            providers.append(
                (
                    OpenAICompatibleClient(
                        api_key=fb["api_key"],
                        model=fb["model"],
                        base_url=fb["base_url"],
                        max_retries=3,
                    ),
                    fb["model"],
                )
            )
        llm_client: LLMClient = primary if len(providers) == 1 else FallbackLLMClient(providers)
        limiter = AssistantLimiter(settings)
        try:
            assistant_service = AssistantService(settings, svc.db, llm_client, limiter)
        except KnowledgePackMissingError as exc:
            await llm_client.aclose()
            raise APIError(
                "ORB-AS-001",
                "assistant is not available",
                detail={"reason": "missing_knowledge_pack"},
            ) from exc
        request.app.state.assistant_service = assistant_service
        request.app.state.assistant_limiter = limiter
    else:
        assistant_service = request.app.state.assistant_service

    # Convert messages
    try:
        messages = _convert_messages(payload.messages)
    except ValidationError as exc:
        raise APIError("ORB-API-001", "invalid messages", detail={"errors": exc.errors()}) from exc

    # Build context dict for service
    context = payload.context.model_dump(exclude_none=True)

    # Peek at the first event before committing to a 200 stream: a
    # pre-flight ORB-AS-001 (unavailable/degraded) becomes a real 503 error
    # envelope; failures after tokens flow stay in-stream error events.
    stream_iter = assistant_service.chat(messages, context, client_ip)
    try:
        first_event = await stream_iter.__anext__()
    except StopAsyncIteration:
        first_event = None
    if (
        first_event is not None
        and first_event["type"] == "error"
        and (first_event["data"].get("code") == "ORB-AS-001")
    ):
        raise APIError(
            "ORB-AS-001",
            first_event["data"].get("message", "assistant is not available"),
            detail=first_event["data"].get("detail") or {},
        )

    # Create SSE generator
    async def event_generator() -> AsyncIterator[str]:
        try:
            if first_event is not None:
                yield _sse(first_event)
            async for event in stream_iter:
                yield _sse(event)
        except Exception:
            logger.exception("Assistant chat stream error")
            # Emit error event if stream hasn't ended
            import json

            error_data = {
                "code": "ORB-SYS-001",
                "message": "Internal error",
                "detail": {},
            }
            yield f"event: error\ndata: {json.dumps(error_data, separators=(',', ':'))}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/capabilities")
async def capabilities(request: Request, svc: Annotated[Services, Depends(services)]) -> dict:
    """Report assistant capability state (enabled/disabled/degraded, limits)."""
    settings = svc.settings
    ready, reason = assistant_readiness(settings)
    service = getattr(request.app.state, "assistant_service", None)
    degraded = bool(service is not None and service.degraded)
    return ok(
        {
            "enabled": ready and not degraded,
            "reason": "provider degraded" if degraded else reason,
            "model": settings.llm_model if ready else None,
            "rate_limit_rpm": settings.assistant_rpm,
            "daily_cap": settings.assistant_daily_cap,
            "max_tool_iters": settings.assistant_max_tool_iters,
            "max_output_tokens": settings.assistant_max_output_tokens,
        }
    )


# Re-export schemas for API use

__all__ = ["router", "ChatRequest"]
