from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from zara.api.app import create_app
from zara.api.errors import AppError
from zara.config import Settings


def _client_with(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_unknown_route_uses_problem_details(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"X-Request-Id": "req-404"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "https://zara.app/errors/not_found"
    assert body["title"] == "Not found"
    assert body["status"] == 404
    assert body["code"] == "not_found"
    assert body["request_id"] == "req-404"


def test_validation_errors_list_fields(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/items/{item_id}")
    def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    with _client_with(app) as client:
        response = client.get("/items/not-a-number")

    assert response.status_code == 422
    body = response.json()
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "validation_error"
    assert body["errors"][0]["field"] == "item_id"
    assert body["errors"][0]["message"]


def test_app_error_maps_to_problem_details(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/conflict")
    def conflict() -> None:
        raise AppError(
            "The memory item changed while you were editing it.",
            status_code=409,
            code="conflict",
            title="Conflict",
        )

    with _client_with(app) as client:
        response = client.get("/conflict")

    assert response.status_code == 409
    body = response.json()
    assert body["type"] == "https://zara.app/errors/conflict"
    assert body["status"] == 409
    assert body["detail"] == "The memory item changed while you were editing it."


def test_unhandled_errors_do_not_leak_internals(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("database password is hunter2")

    with _client_with(app) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


def test_cors_preflight_allows_configured_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_openapi_documents_problem_details(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert "ProblemDetail" in schema["components"]["schemas"]
    assert "FieldError" in schema["components"]["schemas"]
    responses = schema["paths"]["/health"]["get"]["responses"]
    assert "default" in responses
    assert (
        responses["default"]["content"]["application/problem+json"]["schema"]["$ref"]
        == "#/components/schemas/ProblemDetail"
    )
