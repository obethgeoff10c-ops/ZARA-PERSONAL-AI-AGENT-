"""RFC 9457 problem+json error handling.

Handlers never leak stack traces, SQL, or upstream payloads to clients. Full
details stay in structured server logs keyed by request ID (07-API.md §0).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from zara.errors import AppError
from zara.observability.context import get_request_id

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
_ERROR_BASE = "https://zara.app/errors"

_STATUS_CODES: dict[int, tuple[str, str]] = {
    400: ("bad_request", "Bad request"),
    401: ("unauthenticated", "Authentication required"),
    403: ("forbidden", "Forbidden"),
    404: ("not_found", "Not found"),
    405: ("method_not_allowed", "Method not allowed"),
    409: ("conflict", "Conflict"),
    412: ("precondition_failed", "Precondition failed"),
    413: ("payload_too_large", "Payload too large"),
    415: ("unsupported_media_type", "Unsupported media type"),
    422: ("validation_error", "Validation failed"),
    429: ("rate_limited", "Rate limit exceeded"),
    500: ("internal_error", "Internal server error"),
    502: ("upstream_error", "Upstream error"),
    503: ("service_unavailable", "Service unavailable"),
}


class FieldError(BaseModel):
    field: str
    message: str


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str | None = None
    errors: list[FieldError] | None = Field(default=None)


def _problem_response(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ProblemDetail(
        type=f"{_ERROR_BASE}/{code}",
        title=title,
        status=status_code,
        detail=detail,
        code=code,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error(
            "application error",
            extra={"error_code": exc.code},
            exc_info=exc,
        )
    return _problem_response(
        status_code=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        errors=[FieldError(**item) for item in exc.errors] if exc.errors else None,
        headers=exc.headers,
    )


def _field_path(location: tuple[Any, ...]) -> str:
    parts = [str(item) for item in location if item not in ("body", "query", "path", "header")]
    return ".".join(parts) or "request"


async def validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = [
        FieldError(
            field=_field_path(tuple(error.get("loc", ()))),
            message=str(error.get("msg", "Invalid value")),
        )
        for error in exc.errors()
    ]
    return _problem_response(
        status_code=422,
        code="validation_error",
        title="Validation failed",
        detail="One or more fields are invalid.",
        errors=errors,
    )


async def http_exception_handler(
    _request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    code, default_title = _STATUS_CODES.get(exc.status_code, ("http_error", "Request failed"))
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else default_title
    headers = dict(exc.headers) if exc.headers else None
    return _problem_response(
        status_code=exc.status_code,
        code=code,
        title=default_title,
        detail=detail,
        headers=headers,
    )


async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception", extra={"error_code": "internal_error"})
    return _problem_response(
        status_code=500,
        code="internal_error",
        title="Internal server error",
        detail="An unexpected error occurred. Quote the request ID when reporting it.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
