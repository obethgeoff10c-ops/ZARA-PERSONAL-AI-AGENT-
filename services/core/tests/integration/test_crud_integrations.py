"""End-to-end integration metadata lifecycle."""

from __future__ import annotations

import uuid

import pytest

from helpers import Api, SeedRow, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("int"), org_id=unique_org())


def _identity(api: Api, token: str) -> tuple[uuid.UUID, uuid.UUID]:
    body = api.get("/me", token=token).json()
    return uuid.UUID(body["tenant_id"]), uuid.UUID(body["id"])


async def _seed_integration(
    seed_row: SeedRow, tenant_id: uuid.UUID, user_id: uuid.UUID, account: str
) -> uuid.UUID:
    integration_id = uuid.uuid4()
    await seed_row(
        tenant_id,
        user_id,
        """
        INSERT INTO integrations.integrations
            (id, tenant_id, user_id, provider, account_identifier, status,
             granted_scopes, settings)
        VALUES (:id, :tenant_id, :user_id, 'gmail', :account, 'active',
                ARRAY['gmail.readonly']::text[], '{}'::jsonb)
        """,
        {"id": integration_id, "tenant_id": tenant_id, "user_id": user_id, "account": account},
    )
    return integration_id


def test_integrations_empty_for_new_tenant(api: Api) -> None:
    token = _token(api)

    response = api.get("/integrations", token=token)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_integration_list_get_and_settings(api: Api, seed_row: SeedRow) -> None:
    token = _token(api)
    tenant_id, user_id = _identity(api, token)
    integration_id = await _seed_integration(seed_row, tenant_id, user_id, "a@example.com")

    listed = api.get("/integrations", token=token)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [str(integration_id)]
    assert items[0]["provider"] == "gmail"
    assert items[0]["granted_scopes"] == ["gmail.readonly"]

    fetched = api.get(f"/integrations/{integration_id}", token=token)
    assert fetched.status_code == 200
    assert fetched.json()["account_identifier"] == "a@example.com"

    updated = api.patch(
        f"/integrations/{integration_id}/settings",
        token=token,
        json={"settings": {"labels_include": ["INBOX"], "backfill_days": 7}},
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["labels_include"] == ["INBOX"]

    invalid = api.patch(
        f"/integrations/{integration_id}/settings",
        token=token,
        json={"settings": {"not_a_real_key": True}},
    )
    assert invalid.status_code == 422
    assert problem_body(invalid)["errors"][0]["field"] == "settings"


async def test_pause_and_resume(api: Api, seed_row: SeedRow) -> None:
    token = _token(api)
    tenant_id, user_id = _identity(api, token)
    integration_id = await _seed_integration(seed_row, tenant_id, user_id, "p@example.com")

    paused = api.post(f"/integrations/{integration_id}/pause", token=token)
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = api.post(f"/integrations/{integration_id}/resume", token=token)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"


async def test_get_unknown_integration_is_not_found(api: Api) -> None:
    token = _token(api)

    response = api.get(f"/integrations/{uuid.uuid4()}", token=token)

    assert response.status_code == 404
    assert problem_body(response)["code"] == "not_found"
