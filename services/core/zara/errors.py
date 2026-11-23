"""Application-level errors shared by services and the API layer.

Keeping this module below ``api`` in the import graph lets services raise
RFC 9457-compatible failures without importing HTTP handlers.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Expected application failure rendered as problem+json."""

    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        title: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        if title is not None:
            self.title = title
        self.errors = errors
        self.headers = headers


def unauthenticated(detail: str = "A valid bearer token is required.") -> AppError:
    return AppError(
        detail,
        status_code=401,
        code="unauthenticated",
        title="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden(detail: str = "You do not have permission to perform this action.") -> AppError:
    return AppError(detail, status_code=403, code="forbidden", title="Forbidden")


def step_up_required(
    detail: str = "This action requires recent step-up authentication.",
) -> AppError:
    return AppError(detail, status_code=403, code="step_up_required", title="Step-up required")


def not_found(detail: str = "The requested resource was not found.") -> AppError:
    return AppError(detail, status_code=404, code="not_found", title="Not found")


def conflict(detail: str, *, code: str = "conflict") -> AppError:
    return AppError(detail, status_code=409, code=code, title="Conflict")


def precondition_failed(detail: str) -> AppError:
    return AppError(
        detail,
        status_code=412,
        code="precondition_failed",
        title="Precondition failed",
    )


def validation_failed(
    detail: str = "One or more fields are invalid.",
    *,
    errors: list[dict[str, Any]] | None = None,
) -> AppError:
    return AppError(
        detail,
        status_code=422,
        code="validation_error",
        title="Validation failed",
        errors=errors,
    )


def rate_limited(
    detail: str, *, retry_after: int, headers: dict[str, str] | None = None
) -> AppError:
    merged = {"Retry-After": str(max(1, retry_after))}
    if headers:
        merged.update(headers)
    return AppError(
        detail,
        status_code=429,
        code="rate_limited",
        title="Rate limit exceeded",
        headers=merged,
    )
