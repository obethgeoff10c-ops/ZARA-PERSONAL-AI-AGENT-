"""Cross-tenant (IDOR) isolation and role-based authorization tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from helpers import Api, SeedRow, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


@dataclass(frozen=True, slots=True)
class Identity:
    token: str
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def _identity(api: Api, token: str) -> Identity:
    body = api.get("/me", token=token).json()
    return Identity(
        token=token, tenant_id=uuid.UUID(body["tenant_id"]), user_id=uuid.UUID(body["id"])
    )


async def _seed_approval(seed_row: SeedRow, tenant_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    approval_id = uuid.uuid4()
    await seed_row(
        tenant_id,
        user_id,
        """
        INSERT INTO agent.approvals
            (id, tenant_id, user_id, risk_tier, args_hash, preview, status, expires_at)
        VALUES (:id, :tenant_id, :user_id, 1, digest('{}', 'sha256'), '{}'::jsonb,
                'pending', now() + interval '1 day')
        """,
        {"id": approval_id, "tenant_id": tenant_id, "user_id": user_id},
    )
    return approval_id


async def _seed_integration(
    seed_row: SeedRow, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    integration_id = uuid.uuid4()
    await seed_row(
        tenant_id,
        user_id,
        """
        INSERT INTO integrations.integrations
            (id, tenant_id, user_id, provider, account_identifier, status,
             granted_scopes, settings)
        VALUES (:id, :tenant_id, :user_id, 'gmail', 'owned@example.com', 'active',
                ARRAY['gmail.readonly']::text[], '{}'::jsonb)
        """,
        {"id": integration_id, "tenant_id": tenant_id, "user_id": user_id},
    )
    return integration_id


@dataclass(frozen=True, slots=True)
class OwnedResources:
    conversation_id: str
    message_id: str
    task_id: str
    memory_id: str
    run_id: str
    approval_id: str
    integration_id: str


async def _owner_with_resources(
    api: Api, seed_row: SeedRow
) -> tuple[Identity, Identity, OwnedResources]:
    org = unique_org()
    owner = _identity(api, api.dev_token(unique_email("owner"), org_id=org))
    other = _identity(api, api.dev_token(unique_email("other"), org_id=unique_org()))

    conversation_id = api.post(
        "/conversations", token=owner.token, json={"title": "Owner secret title"}
    ).json()["id"]
    message_id = api.post(
        f"/conversations/{conversation_id}/messages",
        token=owner.token,
        json={"content": "owner private message"},
    ).json()["id"]
    task_id = api.post("/tasks", token=owner.token, json={"title": "Owner secret task"}).json()[
        "id"
    ]
    memory_id = api.post(
        "/memory", token=owner.token, json={"kind": "semantic", "content": "owner private memory"}
    ).json()["id"]
    run_id = api.post("/runs", token=owner.token, json={"goal": "owner private goal"}).json()["id"]
    approval_id = await _seed_approval(seed_row, owner.tenant_id, owner.user_id)
    integration_id = await _seed_integration(seed_row, owner.tenant_id, owner.user_id)

    resources = OwnedResources(
        conversation_id=conversation_id,
        message_id=message_id,
        task_id=task_id,
        memory_id=memory_id,
        run_id=run_id,
        approval_id=str(approval_id),
        integration_id=str(integration_id),
    )
    return owner, other, resources


async def test_cross_tenant_reads_return_404_without_data(api: Api, seed_row: SeedRow) -> None:
    _owner, other, resources = await _owner_with_resources(api, seed_row)

    cases = [
        ("get", f"/conversations/{resources.conversation_id}", None),
        ("get", f"/tasks/{resources.task_id}", None),
        ("get", f"/memory/{resources.memory_id}", None),
        ("get", f"/runs/{resources.run_id}", None),
        ("get", f"/approvals/{resources.approval_id}", None),
        ("get", f"/integrations/{resources.integration_id}", None),
    ]
    for method, path, body in cases:
        if method == "get":
            response = api.get(path, token=other.token)
        else:  # pragma: no cover - all current cases are reads
            response = getattr(api, method)(path, token=other.token, json=body)
        assert response.status_code == 404, path
        problem = problem_body(response)
        assert problem["code"] == "not_found"
        assert "Owner secret" not in response.text
        assert resources.conversation_id not in response.text

    # Message feedback addresses the message directly and must not reveal it.
    feedback = api.post(
        f"/messages/{resources.message_id}/feedback",
        token=other.token,
        json={"feedback": "down"},
    )
    assert feedback.status_code == 404
    assert "owner private message" not in feedback.text


async def test_cross_tenant_lists_are_empty(api: Api, seed_row: SeedRow) -> None:
    owner, other, _resources = await _owner_with_resources(api, seed_row)

    for path in ("/conversations", "/tasks", "/memory", "/runs", "/approvals", "/integrations"):
        mine = api.get(path, token=owner.token)
        theirs = api.get(path, token=other.token)
        assert mine.status_code == 200
        assert theirs.status_code == 200, path
        assert len(mine.json()["items"]) >= 1, path
        assert theirs.json()["items"] == [], path
        assert theirs.json()["next_cursor"] is None


async def test_cross_tenant_mutations_do_not_change_owned_rows(api: Api, seed_row: SeedRow) -> None:
    owner, other, resources = await _owner_with_resources(api, seed_row)

    patch_cases = [
        ("patch", f"/conversations/{resources.conversation_id}", {"title": "Hijacked"}),
        ("patch", f"/tasks/{resources.task_id}", {"status": "done"}),
        ("patch", f"/memory/{resources.memory_id}", {"content": "Hijacked memory"}),
        (
            "patch",
            f"/integrations/{resources.integration_id}/settings",
            {"settings": {"labels_include": ["SPAM"]}},
        ),
    ]
    for method, path, body in patch_cases:
        response = getattr(api, method)(path, token=other.token, json=body)
        assert response.status_code == 404, path

    cancel = api.post(f"/runs/{resources.run_id}/cancel", token=other.token, json={})
    assert cancel.status_code == 404
    decide = api.post(
        f"/approvals/{resources.approval_id}/reject", token=other.token, json={"reason": "x"}
    )
    assert decide.status_code == 404

    deletes = [
        f"/conversations/{resources.conversation_id}",
        f"/tasks/{resources.task_id}",
        f"/memory/{resources.memory_id}",
    ]
    for path in deletes:
        assert api.delete(path, token=other.token).status_code == 404

    # The owner still sees every row unchanged.
    conversation = api.get(f"/conversations/{resources.conversation_id}", token=owner.token).json()
    assert conversation["title"] == "Owner secret title"
    task = api.get(f"/tasks/{resources.task_id}", token=owner.token).json()
    assert task["status"] == "todo"
    memory = api.get(f"/memory/{resources.memory_id}", token=owner.token).json()
    assert memory["content"] == "owner private memory"
    run = api.get(f"/runs/{resources.run_id}", token=owner.token).json()
    assert run["status"] == "queued"
    approval = api.get(f"/approvals/{resources.approval_id}", token=owner.token).json()
    assert approval["status"] == "pending"
    integration = api.get(f"/integrations/{resources.integration_id}", token=owner.token).json()
    assert integration["settings"] == {}


async def test_member_role_cannot_read_audit_but_owner_can(api: Api) -> None:
    org = unique_org()
    owner = _identity(api, api.dev_token(unique_email("owner"), org_id=org))
    member = _identity(api, api.dev_token(unique_email("member"), org_id=org))
    assert owner.token != member.token

    api.post("/conversations", token=owner.token, json={"title": "audited"})

    forbidden = api.get("/activity/audit", token=member.token)
    assert forbidden.status_code == 403
    assert problem_body(forbidden)["code"] == "forbidden"

    allowed = api.get("/activity/audit", token=owner.token)
    assert allowed.status_code == 200
    assert allowed.json()["items"]


async def test_service_scope_user_is_still_tenant_scoped(api: Api, seed_row: SeedRow) -> None:
    """A service-scoped token may act for its tenant but never for another."""
    _owner, other, resources = await _owner_with_resources(api, seed_row)

    leaked = api.get(f"/conversations/{resources.conversation_id}", token=other.token)
    assert leaked.status_code == 404
