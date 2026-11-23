"""End-to-end memory CRUD plus suggestion confirm/reject."""

from __future__ import annotations

import uuid

import pytest

from helpers import Api, SeedRow, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("mem"), org_id=unique_org())


def _identity(api: Api, token: str) -> tuple[uuid.UUID, uuid.UUID]:
    body = api.get("/me", token=token).json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["id"])


async def _seed_suggested(seed_row: SeedRow, tenant_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    memory_id = uuid.uuid4()
    await seed_row(
        tenant_id,
        user_id,
        """
        INSERT INTO memory.memory_items
            (id, tenant_id, user_id, kind, title, content, payload, importance,
             confidence, trust_level, status, user_pinned, content_hash, created_by)
        VALUES (:id, :tenant_id, :user_id, 'semantic', :title, :content, '{}'::jsonb,
                0.5, 0.8, 'zara', 'suggested', false, digest(:content, 'sha256'), 'extractor')
        """,
        {
            "id": memory_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "title": "Suggested fact",
            "content": "suggested content",
        },
    )
    return memory_id


def test_memory_lifecycle(api: Api) -> None:
    token = _token(api)

    created = api.post(
        "/memory",
        token=token,
        json={"kind": "episodic", "content": "met the team", "title": "Meeting"},
    )
    assert created.status_code == 201
    memory = created.json()
    memory_id = memory["id"]
    assert memory["status"] == "active"
    assert memory["kind"] == "episodic"

    fetched = api.get(f"/memory/{memory_id}", token=token)
    assert fetched.status_code == 200
    detail = fetched.json()
    assert detail["content"] == "met the team"
    assert detail["sources"] == []

    patched = api.patch(
        f"/memory/{memory_id}", token=token, json={"content": "met the whole team", "pinned": True}
    )
    assert patched.status_code == 200
    assert patched.json()["content"] == "met the whole team"
    assert patched.json()["user_pinned"] is True

    deleted = api.delete(f"/memory/{memory_id}", token=token)
    assert deleted.status_code == 204
    assert api.get(f"/memory/{memory_id}", token=token).status_code == 404


def test_confirm_on_active_memory_is_conflict(api: Api) -> None:
    token = _token(api)
    memory_id = api.post(
        "/memory", token=token, json={"kind": "semantic", "content": "already active"}
    ).json()["id"]

    response = api.post(f"/memory/{memory_id}/confirm", token=token)

    assert response.status_code == 409
    assert problem_body(response)["code"] == "conflict"


async def test_confirm_and_reject_suggested_memory(api: Api, seed_row: object) -> None:
    token = _token(api)
    tenant_id, user_id = _identity(api, token)
    first_id = await _seed_suggested(seed_row, tenant_id, user_id)
    second_id = await _seed_suggested(seed_row, tenant_id, user_id)

    suggestions = api.get("/memory/suggestions", token=token)
    assert suggestions.status_code == 200
    assert {item["id"] for item in suggestions.json()["items"]} >= {str(first_id), str(second_id)}

    confirmed = api.post(f"/memory/{first_id}/confirm", token=token)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "active"
    assert confirmed.json()["confidence"] >= 0.9

    rejected = api.post(f"/memory/{second_id}/reject", token=token)
    assert rejected.status_code == 204
    assert api.get(f"/memory/{second_id}", token=token).json()["status"] == "deleted"

    active = api.get("/memory", token=token, params={"status": "active"})
    ids = {item["id"] for item in active.json()["items"]}
    assert str(first_id) in ids
    assert str(second_id) not in ids


async def test_memory_patch_if_match_guard(api: Api) -> None:
    token = _token(api)
    memory_id = api.post(
        "/memory", token=token, json={"kind": "semantic", "content": "guarded"}
    ).json()["id"]

    stale = api.patch(
        f"/memory/{memory_id}",
        token=token,
        json={"content": "stale edit"},
        headers={"If-Match": "999"},
    )

    assert stale.status_code == 412
    assert problem_body(stale)["code"] == "precondition_failed"


async def test_invalid_kind_is_rejected_with_field_error(api: Api) -> None:
    token = _token(api)

    response = api.post("/memory", token=token, json={"kind": "nonsense", "content": "x"})

    assert response.status_code == 422
    body = problem_body(response)
    assert body["errors"][0]["field"] == "kind"
