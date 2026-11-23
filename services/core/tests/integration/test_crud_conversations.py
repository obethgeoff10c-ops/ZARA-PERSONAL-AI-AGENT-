"""End-to-end conversation, message, and feedback CRUD."""

from __future__ import annotations

import uuid

import pytest

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("conv"), org_id=unique_org())


def test_conversation_lifecycle(api: Api) -> None:
    token = _token(api)

    created = api.post("/conversations", token=token, json={"title": "Planning"})
    assert created.status_code == 201
    conversation = created.json()
    conversation_id = conversation["id"]
    assert conversation["title"] == "Planning"
    assert conversation["archived_at"] is None

    listed = api.get("/conversations", token=token)
    assert listed.status_code == 200
    assert any(item["id"] == conversation_id for item in listed.json()["items"])

    fetched = api.get(f"/conversations/{conversation_id}", token=token)
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Planning"

    patched = api.patch(
        f"/conversations/{conversation_id}", token=token, json={"title": "Planning v2"}
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Planning v2"

    archived = api.patch(f"/conversations/{conversation_id}", token=token, json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    deleted = api.delete(f"/conversations/{conversation_id}", token=token)
    assert deleted.status_code == 204
    missing = api.get(f"/conversations/{conversation_id}", token=token)
    assert missing.status_code == 404
    assert problem_body(missing)["code"] == "not_found"


def test_conversation_without_title_is_allowed(api: Api) -> None:
    token = _token(api)

    created = api.post("/conversations", token=token, json={})

    assert created.status_code == 201
    assert created.json()["title_source"] == "auto"


def test_message_append_list_and_feedback(api: Api) -> None:
    token = _token(api)
    conversation_id = api.post("/conversations", token=token, json={"title": "Chat"}).json()["id"]

    appended = api.post(
        f"/conversations/{conversation_id}/messages",
        token=token,
        json={"content": "hello there"},
    )
    assert appended.status_code == 201
    message = appended.json()
    message_id = message["id"]
    assert message["role"] == "user"
    assert message["content"] == "hello there"

    listed = api.get(f"/conversations/{conversation_id}/messages", token=token)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [message_id]

    feedback = api.post(f"/messages/{message_id}/feedback", token=token, json={"feedback": "up"})
    assert feedback.status_code == 204

    after = api.get(f"/conversations/{conversation_id}/messages", token=token).json()["items"]
    assert after[0]["feedback"] == "up"

    cleared = api.post(f"/messages/{message_id}/feedback", token=token, json={"feedback": "none"})
    assert cleared.status_code == 204
    final = api.get(f"/conversations/{conversation_id}/messages", token=token).json()["items"]
    assert final[0]["feedback"] is None


def test_messages_of_missing_conversation_are_not_found(api: Api) -> None:
    token = _token(api)

    response = api.get(f"/conversations/{uuid.uuid4()}/messages", token=token)

    assert response.status_code == 404
    assert problem_body(response)["code"] == "not_found"
