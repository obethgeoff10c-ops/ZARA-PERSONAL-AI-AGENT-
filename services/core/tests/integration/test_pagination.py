"""Cursor pagination behaviour against live endpoints."""

from __future__ import annotations

import pytest

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("page"), org_id=unique_org())


def test_empty_first_page_envelope(api: Api) -> None:
    token = _token(api)

    response = api.get("/tasks", token=token)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert body["items"] == []
    assert body["next_cursor"] is None


def test_tasks_paginate_with_cursor(api: Api) -> None:
    token = _token(api)
    for index in range(7):
        assert api.post("/tasks", token=token, json={"title": f"Task {index}"}).status_code == 201

    first = api.get("/tasks", token=token, params={"limit": 5})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 5
    assert first_body["next_cursor"]

    second = api.get(
        "/tasks", token=token, params={"limit": 5, "cursor": first_body["next_cursor"]}
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["items"]) == 2
    assert second_body["next_cursor"] is None

    all_ids = [item["id"] for item in first_body["items"] + second_body["items"]]
    assert len(set(all_ids)) == 7


def test_messages_paginate_oldest_first(api: Api) -> None:
    token = _token(api)
    conversation_id = api.post("/conversations", token=token, json={}).json()["id"]
    ids = []
    for index in range(3):
        response = api.post(
            f"/conversations/{conversation_id}/messages",
            token=token,
            json={"content": f"message {index}"},
        )
        ids.append(response.json()["id"])

    first = api.get(f"/conversations/{conversation_id}/messages", token=token, params={"limit": 2})
    second = api.get(
        f"/conversations/{conversation_id}/messages",
        token=token,
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )

    assert [item["id"] for item in first.json()["items"]] == ids[:2]
    assert [item["id"] for item in second.json()["items"]] == ids[2:]


def test_limit_above_cap_is_rejected_with_field_error(api: Api) -> None:
    token = _token(api)

    response = api.get("/tasks", token=token, params={"limit": 10_000})

    assert response.status_code == 422
    body = problem_body(response)
    assert body["errors"][0]["field"] == "limit"
    assert body["code"] == "validation_error"


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_is_rejected(api: Api, limit: int) -> None:
    token = _token(api)

    response = api.get("/tasks", token=token, params={"limit": limit})

    assert response.status_code == 422
    assert problem_body(response)["errors"][0]["field"] == "limit"


def test_forged_cursor_is_rejected_with_field_error(api: Api) -> None:
    token = _token(api)

    response = api.get("/tasks", token=token, params={"cursor": "forged-cursor"})

    assert response.status_code == 422
    body = problem_body(response)
    assert body["code"] == "validation_error"
    assert body["errors"][0]["field"] == "cursor"


def test_max_page_size_is_capped_in_helper() -> None:
    from zara.api.pagination import MAX_PAGE_SIZE

    assert MAX_PAGE_SIZE == 100
