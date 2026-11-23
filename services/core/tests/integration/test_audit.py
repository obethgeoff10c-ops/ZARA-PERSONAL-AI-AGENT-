"""Audit-trail capture, verification, and tenant isolation."""

from __future__ import annotations

import uuid

import pytest

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api, org: str, prefix: str = "audit") -> tuple[str, uuid.UUID]:
    email = unique_email(prefix)
    token = api.dev_token(email, org_id=org)
    body = api.get("/me", token=token).json()
    return token, uuid.UUID(body["tenant_id"])


def _actions(body: dict[str, object]) -> set[str]:
    items = body["items"]
    assert isinstance(items, list)
    return {str(item["action"]) for item in items}


def test_audited_mutations_are_recorded_and_chain_valid(api: Api) -> None:
    token, _ = _token(api, unique_org(), "auditor")

    patched = api.patch(
        "/me/profile", token=token, json={"display_name": "Audited User", "timezone": "UTC"}
    )
    assert patched.status_code == 200
    assert patched.json()["timezone"] == "UTC"

    put = api.put("/me/preferences/ui", token=token, json={"values": {"theme": "dark"}})
    assert put.status_code == 200
    assert put.json()["values"]["theme"] == "dark"

    created = api.post("/conversations", token=token, json={"title": "audited chat"})
    assert created.status_code == 201

    audit = api.get("/activity/audit", token=token)
    assert audit.status_code == 200
    body = audit.json()
    assert {
        "user.profile_updated",
        "user.preferences_updated",
        "conversation.created",
    } <= _actions(body)

    filtered = api.get("/activity/audit", token=token, params={"action": "conversation.created"})
    assert filtered.status_code == 200
    assert _actions(filtered.json()) == {"conversation.created"}

    verified = api.get("/activity/audit/verify", token=token)
    assert verified.status_code == 200
    result = verified.json()
    assert result["valid"] is True
    assert result["broken_ids"] == []


def test_activity_feed_reflects_audit_actions(api: Api) -> None:
    token, _ = _token(api, unique_org(), "activity")

    api.post("/conversations", token=token, json={"title": "feed"})

    response = api.get("/activity", token=token)
    assert response.status_code == 200
    actions = {item["action"] for item in response.json()["items"]}
    assert "conversation.created" in actions


def test_audit_is_tenant_scoped(api: Api) -> None:
    org_a = unique_org()
    token_a, tenant_a = _token(api, org_a, "alice")
    token_b, tenant_b = _token(api, unique_org(), "bob")

    conversation_a = api.post("/conversations", token=token_a, json={"title": "A private"}).json()[
        "id"
    ]
    conversation_b = api.post("/conversations", token=token_b, json={"title": "B private"}).json()[
        "id"
    ]

    audit_a = api.get("/activity/audit", token=token_a)
    audit_b = api.get("/activity/audit", token=token_b)
    assert audit_a.status_code == 200
    assert audit_b.status_code == 200

    items_a = audit_a.json()["items"]
    items_b = audit_b.json()["items"]
    assert items_a and items_b
    assert {item["tenant_id"] for item in items_a} == {str(tenant_a)}
    assert {item["tenant_id"] for item in items_b} == {str(tenant_b)}

    resource_ids_a = {item["resource_id"] for item in items_a}
    resource_ids_b = {item["resource_id"] for item in items_b}
    assert conversation_a in resource_ids_a
    assert conversation_a not in resource_ids_b
    assert conversation_b in resource_ids_b
    assert conversation_b not in resource_ids_a


def test_audit_verify_rejects_member_role(api: Api) -> None:
    org = unique_org()
    owner_token, _ = _token(api, org, "owner")
    member_token, _ = _token(api, org, "member")

    api.post("/conversations", token=owner_token, json={"title": "owner data"})

    forbidden = api.get("/activity/audit/verify", token=member_token)
    assert forbidden.status_code == 403
    assert problem_body(forbidden)["code"] == "forbidden"
