"""End-to-end notification listing, acknowledgement, and SSE transport."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from helpers import Api, SeedRow, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("note"), org_id=unique_org())


def _identity(api: Api, token: str) -> tuple[uuid.UUID, uuid.UUID]:
    body = api.get("/me", token=token).json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["id"])


async def _seed_notification(
    seed_row: SeedRow, tenant_id: uuid.UUID, user_id: uuid.UUID, title: str
) -> uuid.UUID:
    notification_id = uuid.uuid4()
    await seed_row(
        tenant_id,
        user_id,
        """
        INSERT INTO proactive.notifications
            (id, tenant_id, user_id, kind, channel, title, body, status)
        VALUES (:id, :tenant_id, :user_id, 'system', 'in_app', :title, 'body', 'sent')
        """,
        {"id": notification_id, "tenant_id": tenant_id, "user_id": user_id, "title": title},
    )
    return notification_id


def test_notifications_empty_for_new_tenant(api: Api) -> None:
    token = _token(api)

    response = api.get("/notifications", token=token)

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["unread_count"] == 0
    assert body["next_cursor"] is None


async def test_mark_read_and_read_all(api: Api, seed_row: SeedRow) -> None:
    token = _token(api)
    tenant_id, user_id = _identity(api, token)
    first_id = await _seed_notification(seed_row, tenant_id, user_id, "First")
    second_id = await _seed_notification(seed_row, tenant_id, user_id, "Second")

    listed = api.get("/notifications", token=token)
    assert listed.status_code == 200
    body = listed.json()
    assert {item["id"] for item in body["items"]} == {str(first_id), str(second_id)}
    assert body["unread_count"] == 2

    marked = api.post(f"/notifications/{first_id}/read", token=token)
    assert marked.status_code == 204
    after_one = api.get("/notifications", token=token).json()
    assert after_one["unread_count"] == 1
    assert api.get("/notifications", token=token, params={"unread": True}).json()["items"][0][
        "id"
    ] == str(second_id)

    read_all = api.post("/notifications/read-all", token=token)
    assert read_all.status_code == 204
    final = api.get("/notifications", token=token).json()
    assert final["unread_count"] == 0
    assert all(item["read_at"] is not None for item in final["items"])


async def test_mark_read_unknown_or_foreign_is_not_found(api: Api, seed_row: SeedRow) -> None:
    token = _token(api)
    tenant_id, user_id = _identity(api, token)
    notification_id = await _seed_notification(seed_row, tenant_id, user_id, "Owned")
    stranger = _token(api)

    unknown = api.post(f"/notifications/{uuid.uuid4()}/read", token=token)
    assert unknown.status_code == 404
    foreign = api.post(f"/notifications/{notification_id}/read", token=stranger)
    assert foreign.status_code == 404
    assert problem_body(unknown)["code"] == "not_found"


def test_notification_stream_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/notifications/stream")

    assert response.status_code == 401
    assert problem_body(response)["code"] == "unauthenticated"


def test_notification_stream_starts_with_retry_event(api: Api, client: TestClient) -> None:
    token = _token(api)

    with client.stream(
        "GET", "/api/v1/notifications/stream", headers={"Authorization": f"Bearer {token}"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        first_line = next(response.iter_lines())
        assert first_line == "retry: 3000"
