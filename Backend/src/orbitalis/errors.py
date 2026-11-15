"""ORB-* codes, HTTP mapping and the single error-envelope handler.

Purpose: Every ORB error becomes ``{"error": {code, message, detail}}`` here
and nowhere else (SDD §8.1, §11.1). Routers raise typed errors or
``APIError``; they never build envelopes themselves.
Inputs: Exceptions raised anywhere under ``api``.
Outputs: JSON error responses; ``status_for_code`` for programmatic use.
Called by: ``main.create_app`` (handler registration), api routers (raise).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

logger = logging.getLogger(__name__)

# SDD Appendix B: code -> HTTP status. Codes not listed fall back by module.
HTTP_STATUS: dict[str, int] = {
    "ORB-API-001": 422,
    "ORB-API-002": 404,
    "ORB-API-003": 409,
    "ORB-GW-001": 502,
    "ORB-GW-002": 502,
    "ORB-GW-003": 503,
    "ORB-GW-004": 504,
    "ORB-PL-001": 500,
    "ORB-PL-002": 429,
    "ORB-PL-003": 409,
    "ORB-ST-001": 500,
    "ORB-ST-002": 507,
    "ORB-CORE-001": 500,
    "ORB-CORE-002": 422,
    "ORB-OV-001": 502,
    "ORB-JOB-001": 404,
    "ORB-JOB-002": 429,
    # ORB-JOB-003/004 are job-state codes (Appendix B: "200, job failed"),
    # carried in job rows and SSE events — never raised to the HTTP layer.
    # If one ever reaches _unhandled_handler the JOB fallback (500) applies.
    "ORB-RS-001": 409,
    "ORB-RS-002": 422,
    "ORB-RS-003": 422,
    "ORB-ML-001": 503,
    "ORB-ML-002": 500,
    "ORB-AS-001": 503,
    "ORB-AS-002": 429,
    "ORB-AS-003": 429,
    "ORB-AS-004": 502,
    "ORB-SYS-001": 500,
    "ORB-SYS-002": 503,
}

# Module-prefix fallback for codes missing from the table.
_MODULE_STATUS = {
    "API": 500,
    "GW": 502,
    "PL": 500,
    "ST": 500,
    "CORE": 500,
    "OV": 502,
    "JOB": 500,
    "RS": 409,
    "ML": 503,
    "AS": 503,
    "SYS": 500,
}


class APIError(Exception):
    """Error raised by api code; carries its Appendix B code and detail."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status if status is not None else status_for_code(code)
        self.detail = detail or {}


def status_for_code(code: str) -> int:
    """HTTP status for an ORB code (Appendix B, with prefix fallback)."""
    if code in HTTP_STATUS:
        return HTTP_STATUS[code]
    module = code.split("-", 2)[1] if code.count("-") >= 2 else "SYS"
    return _MODULE_STATUS.get(module, 500)


def error_payload(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """The §8.1 error envelope."""
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def _respond(request: Request, code: str, message: str, status: int, detail=None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    payload = error_payload(code, message, detail)
    if request_id:
        payload["error"]["request_id"] = request_id
    response = JSONResponse(status_code=status, content=payload)
    if request_id:
        # ServerErrorMiddleware builds 500s outside user middleware, so the
        # header must be set here as well as by RequestIdMiddleware.
        response.headers["X-Request-ID"] = request_id
    return response


async def _api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return _respond(request, exc.code, exc.message, exc.status, exc.detail)


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _respond(
        request, "ORB-API-001", "invalid request parameters", 422, {"errors": exc.errors()}
    )


async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Framework-level errors (e.g. malformed path) still get the envelope.
    code = "ORB-API-002" if exc.status_code == 404 else "ORB-API-001"
    return _respond(request, code, str(exc.detail), exc.status_code)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Domain errors (GatewayError, PipelineError, JobError, CoreError) all
    # expose ``.code``; anything else becomes ORB-SYS-001. Never leak stack
    # traces (SDD §8.1). No domain imports here: api must not touch gateway.
    logger.exception("unhandled request error")
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code.startswith("ORB-"):
        code = "ORB-SYS-001"
    message = getattr(exc, "message", None) or (str(exc) if code != "ORB-SYS-001" else "")
    return _respond(request, code, message or "internal error", status_for_code(code))


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign ``request.state.request_id`` and echo it as ``X-Request-ID``.

    Honors an inbound ``X-Request-ID`` so upstream callers can correlate;
    otherwise mints a uuid4 hex. Error envelopes pick the id up via
    ``request.state`` (SDD §8.1: unexpected errors carry a request id).
    """

    async def dispatch(self, request: StarletteRequest, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def register_error_handlers(app: FastAPI) -> None:
    """Wire every exception type to the ORB envelope — the only mapping point."""
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(APIError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(Exception, _unhandled_handler)


__all__ = [
    "APIError",
    "HTTP_STATUS",
    "RequestIdMiddleware",
    "error_payload",
    "register_error_handlers",
    "status_for_code",
]
