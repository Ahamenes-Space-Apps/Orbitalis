"""Chat orchestration and grounding rules.

Purpose: Implement the grounded assistant chat flow (AS.04, FR-AS-01/03).
Inputs: User messages, context identifiers, LLM client, read-only tools.
Outputs: Streamed SSE events (token, tool_call, done, error).
Called by: API chat endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from orbitalis.assistant.knowledge import load_knowledge_pack
from orbitalis.assistant.limits import (
    AssistantDailyBudgetError,
    AssistantLimiter,
    AssistantRateLimitError,
)
from orbitalis.assistant.llm import (
    LLMClient,
    LLMMessage,
    LLMProviderError,
    LLMProviderUnavailableError,
    LLMTimeoutError,
    LLMToolCall,
)
from orbitalis.assistant.tools import (
    explain_flags,
    list_candidates,
    read_experiment_results,
)
from orbitalis.settings import Settings
from orbitalis.store.db import Database

logger = logging.getLogger(__name__)

# System prompt template with knowledge pack injection and grounding rules.
# Tool schemas are sent once via the provider ``tools`` parameter — repeating
# them here doubled the prompt (AS.04 review).
_SYSTEM_PROMPT_TEMPLATE = """You are the ORBITALIS assistant, a grounded chatbot for the NASA Space Apps "Planet X and SPHEREx" challenge.

Your answers must be grounded in:
1. The project knowledge pack below
2. Tool results from the current field's stored data
3. The honesty rules that govern all project communication

{knowledge_pack}

## Current Context

The user currently has these items selected in the Explorer. When they refer
to "this field", "this candidate", "this job" or "this experiment", they mean
these IDs — pass them to tools instead of asking which one:

{context_block}

## Grounding Rules (Non-Negotiable)

1. **Candidates are always `unconfirmed`.** There is no "confirmed" state in v1. Never present a candidate as a confirmed object, source, or discovery.

2. **Model scores are predictions, not confirmations.** They are always objects `{{value, model_version, kind: "model_prediction"}}`. Never present an uncalibrated score as confidence or probability.

3. **Observed, predicted, and candidate positions are three separate types.** They never share a field or column. Candidate positions are detections from shift-and-stack (unconfirmed).

4. **Unmatched detections in real data are NOT "false positives".** In real data, detections matching nothing known are candidates, not errors. "Spurious" is an outcome class only inside injection experiments.

5. **Validation outcomes are `recovered`, `missed`, `preexisting`, `spurious`.** Precision is always "relative to injected sources in this experiment". Results apply to this experiment only.

6. **Stack SNR is relative, not absolute significance.** The default background variant (difference imaging plus high-pass) correlates noise between frames and inflates score-map values. Always read `background_variant` alongside `stack_snr`. Never present it as a detection significance or confidence.

7. **Figures in answers must come from tool results or resolved context.** If a tool returns nothing, say so.

8. **The assistant footnote applies to every answer:** "AI answer based on this field's stored results. Candidates are unconfirmed."

9. **Banned words in user-facing text:** confirmed, discovery, found a planet, false positive (outside experiments), confidence (unless calibrated).

## Available Tools

You have access to read-only tools over stored data (schemas are provided in
the request's ``tools`` parameter). Use them to answer questions grounded in
actual results. Do not answer from general knowledge when tools can provide
field-specific data.
"""  # noqa: E501

# Tool schema definitions for the LLM (OpenAI function-calling format)
_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_candidates",
            "description": (
                "List stored candidates for a field. Returns candidate_id, position, motion, "
                "stack_snr, model_score, predicted_category, static_source, "
                "and background_variant. Does NOT return stack_png_blob or frames "
                "unless explicitly requested."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "string",
                        "description": "The field ID to list candidates for",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Optional: filter candidates by job ID",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max candidates to return (default 25)",
                    },
                    "include_frames": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include per-frame observation references (opt-in)",
                    },
                },
                "required": ["field_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_experiment_results",
            "description": (
                "Read stored injection-and-recovery experiment results. "
                "Provide exactly one of experiment_id or field_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "experiment_id": {
                        "type": "string",
                        "description": "Specific experiment to read (includes trials)",
                    },
                    "field_id": {
                        "type": "string",
                        "description": "List all experiments for a field (summaries only)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_flags",
            "description": (
                "Explain a SPHEREx FLAGS integer value. Returns which known bits are set, "
                "which are masked by default, unknown bits, and the masking policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "FLAGS integer value to decode",
                    },
                },
                "required": ["value"],
            },
        },
    },
]

# Map tool names to actual async functions
_TOOL_DISPATCH = {
    "list_candidates": list_candidates,
    "read_experiment_results": read_experiment_results,
    "explain_flags": explain_flags,
}

_CHARS_PER_TOKEN = 4  # rough estimate; provider usage fields are not consumed yet


def _estimate_text_tokens(text: str | None) -> int:
    return len(text or "") // _CHARS_PER_TOKEN


def _estimate_call_tokens(messages: list[LLMMessage]) -> int:
    """Rough input-token estimate for one provider call (history + tools)."""
    total = sum(_estimate_text_tokens(m.content) for m in messages)
    total += len(json.dumps(_TOOL_SCHEMAS)) // _CHARS_PER_TOKEN
    return max(total, 1)


def _context_block(context: dict[str, Any]) -> str:
    """Render the user's current selection for the system prompt."""
    keys = ("field_id", "job_id", "candidate_id", "frame_ref", "experiment_id")
    lines = [f"- {k}: {context[k]}" for k in keys if context.get(k)]
    return "\n".join(lines) if lines else "(nothing selected)"


class AssistantService:
    """Orchestrates the grounded assistant chat flow."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm_client: LLMClient,
        limiter: AssistantLimiter,
    ) -> None:
        self._settings = settings
        self._db = db
        self._llm_client = llm_client
        self._limiter = limiter
        self._knowledge_pack = load_knowledge_pack(settings.config_dir)

    async def aclose(self) -> None:
        """Release the underlying LLM HTTP client if it owns one."""
        aclose = getattr(self._llm_client, "aclose", None)
        if aclose is not None:
            await aclose()

    @property
    def degraded(self) -> bool:
        """True while the provider breaker is open (AS.05)."""
        return bool(getattr(self._llm_client, "degraded", False))

    async def chat(
        self,
        messages: list[LLMMessage],
        context: dict[str, Any],
        client_ip: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run the grounded chat flow, yielding SSE-ready event dicts.

        Yields events with keys: type (token|tool_call|done|error), data (dict).
        """
        # 1. Check rate limit before any expensive work
        try:
            await self._limiter.check_rate_limit(client_ip)
        except AssistantRateLimitError as exc:
            yield {
                "type": "error",
                "data": {"code": exc.code, "message": exc.message, "detail": exc.detail},
            }
            return

        # 2. Sanitize client messages: only user/assistant text roles survive.
        #    The API schema already rejects system/tool roles; this is the
        #    service-level guard so a forged history cannot inject grounding
        #    rules or fake tool results (AS.04 review).
        safe_messages = [
            LLMMessage(role=m.role, content=m.content)
            for m in messages
            if m.role in ("user", "assistant") and m.content
        ]
        if not safe_messages or safe_messages[-1].role != "user":
            yield {
                "type": "error",
                "data": {
                    "code": "ORB-API-001",
                    "message": "chat requires at least one user message",
                    "detail": {},
                },
            }
            return

        # 3. Build system prompt with knowledge pack, current context, and
        #    grounding rules. Tool schemas go through the ``tools`` parameter
        #    only — they are not duplicated into the prompt.
        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_pack=self._knowledge_pack,
            context_block=_context_block(context),
        )
        full_messages = [LLMMessage(role="system", content=system_prompt)] + safe_messages
        field_id = context.get("field_id")

        # 4. Budget estimate covers input (full history is resent every
        #    provider call) plus accumulated output across tool iterations.
        input_tokens = _estimate_call_tokens(full_messages)
        output_tokens = 0
        try:
            await self._limiter.check_daily_budget(client_ip, input_tokens)
        except AssistantDailyBudgetError as exc:
            yield {
                "type": "error",
                "data": {"code": exc.code, "message": exc.message, "detail": exc.detail},
            }
            return

        # 5. Bounded tool-call loop — every provider call counts as an
        #    iteration so a stream that ends without ``done`` cannot loop.
        tool_iter = 0
        max_iters = self._limiter.max_tool_iters

        while tool_iter < max_iters:
            tool_iter += 1
            tool_calls_in_stream: list[LLMToolCall] = []
            assistant_text: list[str] = []
            saw_done = False
            try:
                stream = self._llm_client.stream(
                    messages=full_messages,
                    tools=_TOOL_SCHEMAS,
                    max_tokens=self._limiter.max_output_tokens,
                    model=self._settings.llm_model,
                )

                async for chunk in stream:
                    if chunk.type == "token":
                        assistant_text.append(chunk.content or "")
                        output_tokens += _estimate_text_tokens(chunk.content)
                        yield {"type": "token", "data": {"delta": chunk.content}}
                    elif chunk.type == "tool_call":
                        tool_calls_in_stream.append(chunk.tool_call)
                        # Emit tool_call notification (name only per SDD)
                        yield {"type": "tool_call", "data": {"name": chunk.tool_call.name}}
                    elif chunk.type == "error":
                        yield {
                            "type": "error",
                            "data": {
                                "code": chunk.error.code,
                                "message": chunk.error.message,
                                "detail": chunk.error.detail,
                            },
                        }
                        return
                    elif chunk.type == "done":
                        saw_done = True

                if not saw_done:
                    yield {
                        "type": "error",
                        "data": {
                            "code": "ORB-AS-004",
                            "message": "LLM stream ended without completing",
                            "detail": {},
                        },
                    }
                    return

                if tool_calls_in_stream:
                    # The provider contract requires the assistant message
                    # carrying tool_calls to precede the role="tool" results —
                    # OpenAI-compatible APIs reject a bare tool message.
                    full_messages.append(
                        LLMMessage(
                            role="assistant",
                            content="".join(assistant_text) or None,
                            tool_calls=list(tool_calls_in_stream),
                        )
                    )
                    for tc in tool_calls_in_stream:
                        tool_result = await self._execute_tool(tc, field_id)
                        full_messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tc.id,
                                name=tc.name,
                                content=json.dumps(tool_result),
                            )
                        )
                    # History grew: re-estimate input for the next call and
                    # re-check the budget before spending more tokens.
                    input_tokens = _estimate_call_tokens(full_messages)
                    try:
                        await self._limiter.check_daily_budget(
                            client_ip, input_tokens + output_tokens
                        )
                    except AssistantDailyBudgetError as exc:
                        yield {
                            "type": "error",
                            "data": {
                                "code": exc.code,
                                "message": exc.message,
                                "detail": exc.detail,
                            },
                        }
                        return
                    continue

                # No tool calls: the answer is complete.
                yield {"type": "done", "data": {"status": "completed"}}
                await self._limiter.record_usage(client_ip, input_tokens + output_tokens)
                return

            except (LLMProviderUnavailableError, LLMProviderError, LLMTimeoutError) as exc:
                yield {
                    "type": "error",
                    "data": {"code": exc.code, "message": exc.message, "detail": exc.detail},
                }
                return
            except asyncio.CancelledError:
                # Client disconnected — propagate so the task actually cancels.
                logger.info("Assistant chat cancelled")
                raise
            except Exception:
                logger.exception("Unexpected error in assistant chat")
                yield {
                    "type": "error",
                    "data": {"code": "ORB-SYS-001", "message": "Internal error", "detail": {}},
                }
                return

        # Max tool iterations reached
        yield {
            "type": "error",
            "data": {
                "code": "ORB-AS-004",
                "message": "Too many tool iterations",
                "detail": {"max_iterations": max_iters},
            },
        }

    async def _execute_tool(self, tool_call: LLMToolCall, field_id: str | None) -> dict[str, Any]:
        """Execute a single read-only tool call and return JSON-serializable result.

        Bad arguments return ``{"error": ...}`` to the model so it can retry
        with corrected input instead of ending the whole chat (AS.04 review).
        """
        func = _TOOL_DISPATCH.get(tool_call.name)
        if func is None:
            return {"error": f"Unknown tool: {tool_call.name}"}

        try:
            args = json.loads(tool_call.arguments) if tool_call.arguments else {}
        except json.JSONDecodeError:
            return {"error": "Invalid tool arguments JSON"}
        if not isinstance(args, dict):
            return {"error": "Tool arguments must be a JSON object"}

        # Inject the current field when the model omits it.
        if tool_call.name == "list_candidates" and "field_id" not in args and field_id:
            args["field_id"] = field_id

        try:
            if tool_call.name == "explain_flags":
                # Pure function, no DB needed
                result = func(**args)
            else:
                result = await func(self._db, **args)
        except Exception as exc:
            logger.info("Tool %s failed: %s", tool_call.name, exc)
            return {"error": f"{type(exc).__name__}: {exc}"[:500]}

        # Convert Pydantic models to dict for JSON serialization
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        return result


__all__ = ["AssistantService"]
