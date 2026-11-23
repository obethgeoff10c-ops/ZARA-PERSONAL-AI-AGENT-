from __future__ import annotations

from fastapi.testclient import TestClient
from zara.api import health
from zara.api.health import CheckResult


def test_health_is_dependency_free(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "environment": "test"}
    assert response.headers["content-type"].startswith("application/json")


def test_request_id_is_echoed_when_safe(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-Id": "req-abc-123"})

    assert response.headers["x-request-id"] == "req-abc-123"


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-Id": "../../etc/passwd"})

    returned = response.headers["x-request-id"]
    assert returned != "../../etc/passwd"
    assert len(returned) == 32


def test_readiness_reports_ready_without_redis(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def ok(_settings):  # type: ignore[no-untyped-def]
        return CheckResult(status="ok", latency_ms=1.0)

    monkeypatch.setattr(health, "check_database", ok)

    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["status"] == "ok"
    assert "redis" not in body["checks"]


def test_readiness_returns_503_when_database_fails(client: TestClient, monkeypatch) -> None:
    async def failed(_settings):  # type: ignore[no-untyped-def]
        return CheckResult(status="error", latency_ms=1.0, detail="OperationalError")

    monkeypatch.setattr(health, "check_database", failed)

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["detail"] == "OperationalError"


def test_readiness_includes_and_fails_on_redis_when_configured(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"redis_url": "redis://localhost:6379/0"})
    from zara.api.app import create_app

    async def db_ok(_settings):  # type: ignore[no-untyped-def]
        return CheckResult(status="ok", latency_ms=1.0)

    async def redis_failed(_settings):  # type: ignore[no-untyped-def]
        return CheckResult(status="error", latency_ms=1.0, detail="ConnectionError")

    monkeypatch.setattr(health, "check_database", db_ok)
    monkeypatch.setattr(health, "check_redis", redis_failed)

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["redis"]["detail"] == "ConnectionError"
