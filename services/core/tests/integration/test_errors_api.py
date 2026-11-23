"""API error-format, validation, and abuse-control integration tests."""

from __future__ import annotations

import uuid

import pytest

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("err"), org_id=unique_org())


def test_unknown_route_is_problem_json(api: Api) -> None:
    response = api.get("/does-not-exist")

    assert response.status_code == 404
    body = problem_body(response)
    assert body["type"] == "https://zara.app/errors/not_found"
    assert body["code"] == "not_found"
    assert body["request_id"]
    assert response.headers["X-Request-Id"]


def test_method_not_allowed_is_problem_json(api: Api) -> None:
    token = _token(api)

    response = api.get("/auth/session", token=token)

    assert response.status_code == 405
    assert problem_body(response)["code"] == "method_not_allowed"


@pytest.mark.parametrize(
    ("method", "path", "payload", "field"),
    [
        ("post", "/conversations", {"title": "t" * 301}, "title"),
        ("post", "/tasks", {}, "title"),
        ("post", "/tasks", {"title": "ok", "priority": 9}, "priority"),
        ("post", "/tasks", {"title": "ok", "due_at": "not-a-date"}, "due_at"),
        ("post", "/runs", {"goal": "ok", "mode": "sideways"}, "mode"),
        ("post", "/memory", {"kind": "semantic", "content": "x", "importance": 2}, "importance"),
        ("put", "/me/preferences/ui", {}, "values"),
    ],
)
def test_invalid_payloads_are_422_with_field_errors(
    api: Api, method: str, path: str, payload: dict[str, object], field: str
) -> None:
    token = _token(api)

    response = getattr(api, method)(path, token=token, json=payload)

    assert response.status_code == 422, path
    body = problem_body(response)
    assert body["code"] == "validation_error"
    assert body["errors"], path
    assert any(error["field"] == field for error in body["errors"]), body
    assert "Traceback" not in response.text
    assert "sqlalchemy" not in response.text.lower()


def test_malformed_uuid_path_is_422(api: Api) -> None:
    token = _token(api)

    response = api.get("/conversations/not-a-uuid", token=token)

    assert response.status_code == 422
    body = problem_body(response)
    assert any(error["field"] == "conversation_id" for error in body["errors"])


def test_validation_error_does_not_leak_sql_or_stack(api: Api) -> None:
    token = _token(api)

    response = api.post("/tasks", token=token, json={"title": "x", "due_at": "not-a-date"})

    assert response.status_code == 422
    text = response.text.lower()
    assert "traceback" not in text
    assert "select" not in text
    assert "insert" not in text
    assert "sqlalchemy" not in text
    assert problem_body(response)["errors"][0]["field"] == "due_at"


def test_auth_rate_limit_headers_on_exhaustion(client: object) -> None:
    from fastapi.testclient import TestClient

    assert isinstance(client, TestClient)
    email = unique_email("rl2")
    response = None
    for _ in range(31):
        response = client.post("/api/v1/auth/dev-token", json={"email": email})

    assert response is not None
    assert response.status_code == 429
    body = problem_body(response)
    assert body["code"] == "rate_limited"
    assert body["status"] == 429
    assert response.headers["RateLimit-Limit"] == "30"
    assert response.headers["RateLimit-Remaining"] == "0"
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers["content-type"].startswith("application/problem+json")


def test_random_uuid_does_not_exist_as_conflict(api: Api) -> None:
    token = _token(api)

    response = api.get(f"/tasks/{uuid.uuid4()}", token=token)

    assert response.status_code == 404
    assert problem_body(response)["code"] == "not_found"
