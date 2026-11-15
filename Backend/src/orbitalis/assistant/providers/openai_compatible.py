"""OpenAI-compatible streaming LLM adapter.

Purpose: AS.01 provider adapter implementing ``LLMClient`` over the OpenAI
chat-completions streaming API (SSE) via ``httpx``.
Inputs: Injected api key/model/base URL and optional ``httpx.AsyncClient``.
Outputs: Provider-neutral ``LLMStreamChunk`` values.
Called by: assistant composition (AS.03+) and tests (mocked transport).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from orbitalis.assistant.llm import (
    LLMMessage,
    LLMProviderError,
    LLMProviderUnavailableError,
    LLMStreamChunk,
    LLMTimeoutError,
    LLMToolCall,
)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_WAITS_S = (1.0, 3.0)
_RATE_LIMIT_WAIT_CAP_S = 65.0


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_DURATION_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_duration_s(value: str) -> float | None:
    """Parse provider rate-limit resets like ``172ms``, ``30s``, ``1m26.4s``."""
    parts = _DURATION_RE.findall(value.strip())
    if not parts or "".join(f"{n}{u}" for n, u in parts) != value.strip():
        return None
    return sum(float(n) * _DURATION_S[u] for n, u in parts)


def _rate_limit_wait_s(headers: httpx.Headers) -> float | None:
    """Seconds until the provider's rate-limit window resets, if advertised."""
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), _RATE_LIMIT_WAIT_CAP_S)
        except ValueError:
            pass
    resets = []
    for name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(name)
        if raw and (parsed := _parse_duration_s(raw)) is not None:
            resets.append(parsed)
    if not resets:
        return None
    return min(max(resets), _RATE_LIMIT_WAIT_CAP_S)


# Provider name -> OpenAI-compatible base URL (D-13: Grok primary, Gemini
# fallback). Without this map a configured non-OpenAI provider would send
# its API key to api.openai.com (AS.04 review). ``ORB_LLM_BASE_URL`` overrides.
PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
    "grok": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


def _require_credentials(api_key: str | None, model: str | None) -> tuple[str, str]:
    if not api_key:
        raise LLMProviderUnavailableError(
            "LLM API key is not configured",
            detail={"reason": "missing_api_key"},
        )
    if not model:
        raise LLMProviderUnavailableError(
            "LLM model is not configured",
            detail={"reason": "missing_model"},
        )
    return api_key, model


def _messages_payload(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            item["content"] = m.content
        if m.name is not None:
            item["name"] = m.name
        if m.tool_call_id is not None:
            item["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in m.tool_calls
            ]
        out.append(item)
    return out


class OpenAICompatibleClient:
    """Async streaming client for OpenAI-compatible chat completions."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
        max_retries: int = 2,
        retry_waits_s: tuple[float, ...] = _RETRY_WAITS_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max(0, max_retries)
        self._retry_waits_s = retry_waits_s
        self._client = client
        self._owns_client = client is None
        # AS.05 breaker: after _BREAKER_THRESHOLD consecutive unreachable
        # failures, fail fast for _BREAKER_SECONDS instead of paying the
        # ~4 s retry cost on every request during an outage.
        self._consecutive_unavailable = 0
        self._degraded_until = 0.0

    def _http_client(self) -> httpx.AsyncClient:
        """Lazily create the owned client once; safe for concurrent streams.

        ``httpx.AsyncClient`` supports concurrent requests, so all streams
        share it; it is closed only in ``aclose()`` — never per stream, which
        would break a second in-flight stream (AS.01 review).
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    _BREAKER_THRESHOLD = 3
    _BREAKER_SECONDS = 30.0

    @property
    def degraded(self) -> bool:
        """True while the breaker is open (provider recently unreachable)."""
        import time

        return time.monotonic() < self._degraded_until

    def _note_unavailable(self) -> None:
        self._consecutive_unavailable += 1
        if self._consecutive_unavailable >= self._BREAKER_THRESHOLD:
            import time

            self._degraded_until = time.monotonic() + self._BREAKER_SECONDS

    def _note_success(self) -> None:
        self._consecutive_unavailable = 0

    def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        async def _gen() -> AsyncIterator[LLMStreamChunk]:
            if self.degraded:
                yield LLMStreamChunk(
                    type="error",
                    error=LLMProviderUnavailableError(
                        "LLM provider is degraded (recent failures)",
                        detail={"reason": "provider_degraded"},
                    ),
                )
                return
            try:
                api_key, resolved_model = _require_credentials(self._api_key, model or self._model)
                payload: dict[str, Any] = {
                    "model": resolved_model,
                    "messages": _messages_payload(messages),
                    "max_tokens": max_tokens,
                    "stream": True,
                }
                if tools:
                    payload["tools"] = tools
                async for chunk in self._stream_with_retries(api_key, payload):
                    yield chunk
                self._note_success()
            except LLMProviderUnavailableError as exc:
                self._note_unavailable()
                yield LLMStreamChunk(type="error", error=exc)
            except (LLMProviderError, LLMTimeoutError) as exc:
                yield LLMStreamChunk(type="error", error=exc)

        return _gen()

    async def _stream_with_retries(
        self, api_key: str, payload: dict[str, Any]
    ) -> AsyncIterator[LLMStreamChunk]:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        attempts = self._max_retries + 1
        client = self._http_client()
        emitted = False
        # The shared client stays open for concurrent/other streams; it is
        # closed only by aclose().
        for attempt in range(attempts):
            try:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        if resp.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                            await self._sleep(attempt, resp)
                            continue
                        raise self._http_error(resp.status_code, body)
                    async for chunk in self._iter_sse(resp):
                        emitted = True
                        yield chunk
                    return
            except httpx.TimeoutException as exc:
                if not emitted and attempt < attempts - 1:
                    await self._sleep(attempt)
                    continue
                raise LLMTimeoutError(
                    "LLM provider request timed out",
                    detail={"reason": "timeout"},
                ) from exc
            except httpx.ConnectError as exc:
                if not emitted and attempt < attempts - 1:
                    await self._sleep(attempt)
                    continue
                if emitted:
                    raise LLMProviderError(
                        "LLM provider request failed",
                        detail={"reason": "transport_error"},
                    ) from exc
                raise LLMProviderUnavailableError(
                    "LLM provider is unavailable",
                    detail={"reason": "provider_unreachable"},
                ) from exc
            except httpx.HTTPError as exc:
                if not emitted and attempt < attempts - 1:
                    await self._sleep(attempt)
                    continue
                raise LLMProviderError(
                    "LLM provider request failed",
                    detail={"reason": "transport_error"},
                ) from exc

    async def _sleep(self, attempt: int, resp: httpx.Response | None = None) -> None:
        import asyncio

        wait = self._retry_waits_s[min(attempt, len(self._retry_waits_s) - 1)]
        if resp is not None and resp.status_code == 429:
            # Token/request windows reset on the order of a minute; the fixed
            # short backoff cannot outlast them, so honor the advertised reset.
            advertised = _rate_limit_wait_s(resp.headers)
            if advertised is not None:
                wait = max(wait, advertised)
        await asyncio.sleep(wait)

    def _http_error(self, status: int, body: bytes) -> LLMProviderError:
        reason = "provider_error"
        if status in (401, 403):
            reason = "auth_failed"
        elif status == 429:
            reason = "rate_limited"
        elif status >= 500:
            reason = "server_error"
        elif status >= 400:
            reason = "bad_request"
        detail: dict[str, Any] = {"reason": reason, "status_code": status}
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict) and "type" in err:
                    detail["provider_error_type"] = err["type"]
        except Exception:
            pass
        return LLMProviderError("LLM provider request failed", detail=detail)

    async def _iter_sse(self, resp: httpx.Response) -> AsyncIterator[LLMStreamChunk]:
        tc_buf: dict[int, dict[str, str]] = {}
        done = False
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                done = True
                for chunk in self._flush_tool_calls(tc_buf):
                    yield chunk
                yield LLMStreamChunk(type="done")
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LLMProviderError(
                    "Malformed provider response",
                    detail={"reason": "invalid_json"},
                ) from exc
            if not isinstance(event, dict):
                raise LLMProviderError(
                    "Malformed provider response",
                    detail={"reason": "invalid_event"},
                )
            for chunk in self._chunks_from_event(event, tc_buf):
                yield chunk
        if not done:
            raise LLMProviderError(
                "Malformed provider response",
                detail={"reason": "stream_not_terminated"},
            )

    def _chunks_from_event(
        self, event: dict[str, Any], tc_buf: dict[int, dict[str, str]]
    ) -> list[LLMStreamChunk]:
        if "error" in event:
            raise LLMProviderError(
                "LLM provider request failed",
                detail={"reason": "provider_error_event"},
            )
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LLMProviderError(
                "Malformed provider response",
                detail={"reason": "invalid_choice"},
            )
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise LLMProviderError(
                "Malformed provider response",
                detail={"reason": "invalid_delta"},
            )
        out: list[LLMStreamChunk] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            out.append(LLMStreamChunk(type="token", content=content))
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                try:
                    idx_i = int(idx)
                except Exception:
                    idx_i = 0
                buf = tc_buf.setdefault(idx_i, {"id": "", "name": "", "arguments": ""})
                if "id" in tc and tc["id"]:
                    buf["id"] = str(tc["id"])
                fn = tc.get("function") or {}
                if isinstance(fn, dict):
                    if "name" in fn and fn["name"]:
                        buf["name"] = str(fn["name"])
                    if "arguments" in fn and fn["arguments"] is not None:
                        buf["arguments"] += str(fn["arguments"])
        finish = choice.get("finish_reason")
        if finish == "tool_calls":
            out.extend(self._flush_tool_calls(tc_buf))
        return out

    def _flush_tool_calls(self, tc_buf: dict[int, dict[str, str]]) -> list[LLMStreamChunk]:
        if not tc_buf:
            return []
        out: list[LLMStreamChunk] = []
        for idx in sorted(tc_buf):
            buf = tc_buf[idx]
            if not buf.get("id") or not buf.get("name"):
                raise LLMProviderError(
                    "Malformed provider response",
                    detail={"reason": "incomplete_tool_call"},
                )
            out.append(
                LLMStreamChunk(
                    type="tool_call",
                    tool_call=LLMToolCall(
                        id=buf["id"],
                        name=buf["name"],
                        arguments=buf.get("arguments") or "",
                    ),
                )
            )
        tc_buf.clear()
        return out


__all__ = ["PROVIDER_BASE_URLS", "OpenAICompatibleClient"]
