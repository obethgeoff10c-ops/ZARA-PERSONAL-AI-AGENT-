"""Integration tests for authentication and session lifecycle."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def test_dev_token_authenticates_me(api: Api) -> None:
    email = unique_email("auth")
    token = api.dev_token(email, org_id=unique_org(), name="Auth Tester")

    response = api.get("/me", token=token)

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert body["display_name"] == "Auth Tester"
    assert body["role"] in {"owner", "admin", "member"}
    assert body["profile"] is not None


def test_me_without_authorization_is_problem_json(api: Api) -> None:
    response = api.get("/me")

    assert response.status_code == 401
    body = problem_body(response)
    assert body["type"] == "https://zara.app/errors/unauthenticated"
    assert body["title"] == "Authentication required"
    assert body["status"] == 401
    assert body["code"] == "unauthenticated"
    assert body["request_id"]
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("headers", "expected_detail_fragment"),
    [
        ({"Authorization": "Basic abc.def"}, "Malformed Authorization header"),
        ({"Authorization": "Bearer"}, "Malformed Authorization header"),
    ],
)
def test_me_rejects_malformed_authorization(
    client: TestClient, headers: dict[str, str], expected_detail_fragment: str
) -> None:
    response = client.get("/api/v1/me", headers=headers)

    assert response.status_code == 401
    body = problem_body(response)
    assert expected_detail_fragment in str(body["detail"])


def test_me_rejects_garbage_bearer_token(api: Api) -> None:
    response = api.get("/me", token="not-a-real-token")

    assert response.status_code == 401
    body = problem_body(response)
    assert body["code"] == "unauthenticated"
    assert body["status"] == 401


def test_session_create_list_and_revoke(api: Api) -> None:
    token = api.dev_token(unique_email("auth"), org_id=unique_org())

    created = api.post("/auth/session", token=token, json={"device_label": "ci"})
    assert created.status_code == 201
    session = created.json()
    session_id = session["session_id"]
    session_token = session["token"]
    assert session_token.startswith("zara_s_")

    me = api.get("/me", token=session_token)
    assert me.status_code == 200

    listed = api.get("/auth/sessions", token=token)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert any(item["id"] == session_id and item["current"] is False for item in items)

    revoked = api.delete(f"/auth/sessions/{session_id}", token=token)
    assert revoked.status_code == 204

    after = api.get("/me", token=session_token)
    assert after.status_code == 401
    assert problem_body(after)["code"] == "unauthenticated"


def test_delete_current_session_revokes_its_token(api: Api) -> None:
    token = api.dev_token(unique_email("auth"), org_id=unique_org())
    created = api.post("/auth/session", token=token, json={})
    session_token = created.json()["token"]

    sign_out = api.delete("/auth/session", token=session_token)
    assert sign_out.status_code == 204

    after = api.get("/me", token=session_token)
    assert after.status_code == 401


def test_session_creation_for_other_user_is_not_found(api: Api) -> None:
    owner = api.dev_token(unique_email("auth"), org_id=unique_org())
    other = api.dev_token(unique_email("auth"), org_id=unique_org())
    created = api.post("/auth/session", token=owner, json={})
    session_id = created.json()["session_id"]

    response = api.delete(f"/auth/sessions/{session_id}", token=other)

    assert response.status_code == 404
    assert problem_body(response)["code"] == "not_found"
