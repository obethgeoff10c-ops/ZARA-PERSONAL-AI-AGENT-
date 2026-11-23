"""Shared helpers for the test suite (no database access at import time)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.testclient import TestClient
from httpx import Response

API_V1 = "/api/v1"

SeedRow = Callable[..., Awaitable[None]]


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex}@example.com"


def unique_org() -> str:
    return f"org-{uuid.uuid4().hex}"


class Api:
    """Thin convenience wrapper around :class:`TestClient` for API tests."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    @staticmethod
    def headers(token: str | None) -> dict[str, str]:
        return {} if token is None else {"Authorization": f"Bearer {token}"}

    def _merged_headers(self, token: str | None, extra: dict[str, str] | None) -> dict[str, str]:
        merged = self.headers(token)
        if extra:
            merged.update(extra)
        return merged

    def dev_token(self, email: str, *, org_id: str | None = None, name: str | None = None) -> str:
        payload: dict[str, str] = {"email": email}
        if org_id is not None:
            payload["org_id"] = org_id
        if name is not None:
            payload["display_name"] = name
        response = self.client.post(f"{API_V1}/auth/dev-token", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def get(
        self,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        return self.client.get(
            f"{API_V1}{path}", headers=self._merged_headers(token, headers), **kwargs
        )

    def post(
        self,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        return self.client.post(
            f"{API_V1}{path}", headers=self._merged_headers(token, headers), **kwargs
        )

    def patch(
        self,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        return self.client.patch(
            f"{API_V1}{path}", headers=self._merged_headers(token, headers), **kwargs
        )

    def put(
        self,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        return self.client.put(
            f"{API_V1}{path}", headers=self._merged_headers(token, headers), **kwargs
        )

    def delete(
        self,
        path: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Response:
        return self.client.delete(
            f"{API_V1}{path}", headers=self._merged_headers(token, headers), **kwargs
        )


def problem_body(response: Response) -> dict[str, Any]:
    """Assert an RFC 9457 response and return its decoded body."""
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = response.json()
    return body
