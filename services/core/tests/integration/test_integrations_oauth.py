"""End-to-end OAuth connect/callback tests with a mocked provider (no network)."""

from __future__ import annotations

import urllib.parse
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text
from zara.api.app import create_app
from zara.config import Settings, get_settings
from zara.db.engine import create_engine
from zara.models import Integration, OAuthCredential
from zara.services.oauth import PROVIDERS, redirect_uri

from helpers import problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration

_ACCESS = "ya29.super-secret-access-token"
_REFRESH = "1//super-secret-refresh-token"


def _settings() -> Settings:
    return get_settings().model_copy(
        update={
            "redis_url": None,
            "google_oauth_client_id": "google-client-id",
            "google_oauth_client_secret": SecretStr("google-client-secret"),
            "github_oauth_client_id": "github-client-id",
            "github_oauth_client_secret": SecretStr("github-client-secret"),
        }
    )


def _provider_handler(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": _ACCESS,
                    "refresh_token": _REFRESH,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "openid email profile "
                    "https://www.googleapis.com/auth/gmail.readonly "
                    "https://www.googleapis.com/auth/gmail.compose",
                },
            )
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(200, json={"email": "ana@example.com", "sub": "google-sub-123"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _github_handler(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ghu_user_to_server_token",
                    "token_type": "bearer",
                    "expires_in": 28800,
                },
            )
        if request.url.host == "api.github.com" and request.url.path == "/user":
            return httpx.Response(
                200, json={"id": 42, "login": "octocat", "email": "octo@example.com"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _dev_token(client: TestClient) -> tuple[str, str]:
    email = unique_email("oauth")
    process_org = unique_org()
    response = client.post("/api/v1/auth/dev-token", json={"email": email, "org_id": process_org})
    assert response.status_code == 200, response.text
    return response.json()["access_token"], email


def _start(client: TestClient, token: str, provider: str = "gmail") -> tuple[str, dict[str, str]]:
    started = client.post(
        f"/api/v1/integrations/{provider}/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={"redirect_after": "/settings/integrations"},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["authorization_url"]).query))
    return body["authorization_url"], query


async def test_connect_builds_pkce_authorization_url() -> None:
    settings = _settings()
    with TestClient(create_app(settings)) as client:
        token, _ = _dev_token(client)
        url, query = _start(client, token)

    parsed = urllib.parse.urlsplit(url)
    assert parsed.netloc == "accounts.google.com"
    assert query["code_challenge_method"] == "S256"
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["include_granted_scopes"] == "true"
    assert query["redirect_uri"] == redirect_uri(settings, "gmail")
    assert query["state"].count(".") == 3
    scopes = set(query["scope"].split())
    assert set(PROVIDERS["gmail"].scopes) <= scopes


async def test_callback_stores_encrypted_credentials_and_never_returns_tokens() -> None:
    settings = _settings()
    requests: list[httpx.Request] = []
    app = create_app(settings)
    app.state.oauth_transport = _provider_handler(requests)
    with TestClient(app) as client:
        token, _ = _dev_token(client)
        started = client.post(
            "/api/v1/integrations/gmail/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={"redirect_after": "/settings/integrations"},
        )
        state = started.json()["state"]
        callback = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert "connected=gmail" in callback.headers["location"]
        assert "access_token" not in callback.headers["location"]
        assert "state" not in callback.headers["location"]

        listed = client.get("/api/v1/integrations", headers={"Authorization": f"Bearer {token}"})
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["account_identifier"] == "ana@example.com"
        assert items[0]["status"] == "active"
        assert "https://www.googleapis.com/auth/gmail.readonly" in items[0]["granted_scopes"]
        me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(requests) == 2

    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE zara_app"))
            await connection.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tenant, true),"
                    " set_config('app.user_id', :user, true),"
                    " set_config('app.role', 'user', true)"
                ),
                {"tenant": me["tenant_id"], "user": me["id"]},
            )
            integration_id = (
                await connection.execute(
                    select(Integration.id).where(Integration.user_id == uuid.UUID(me["id"]))
                )
            ).scalar_one()
            credential = (
                await connection.execute(
                    select(
                        OAuthCredential.access_token_ct,
                        OAuthCredential.refresh_token_ct,
                        OAuthCredential.kms_key_id,
                    ).where(OAuthCredential.integration_id == integration_id)
                )
            ).one()
            assert credential.access_token_ct is not None
            assert credential.refresh_token_ct is not None
            assert _ACCESS.encode() not in credential.access_token_ct
            assert _REFRESH.encode() not in credential.refresh_token_ct
            assert credential.kms_key_id == settings.kms_key_id
    finally:
        await engine.dispose()


async def test_callback_state_is_single_use_and_tamper_evident() -> None:
    settings = _settings()
    requests: list[httpx.Request] = []
    app = create_app(settings)
    app.state.oauth_transport = _provider_handler(requests)
    with TestClient(app) as client:
        token, _ = _dev_token(client)
        started = client.post(
            "/api/v1/integrations/gmail/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        state = started.json()["state"]

        forged = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": f"{state}-forged"},
        )
        assert forged.status_code == 422
        assert problem_body(forged)["code"] == "validation_error"

        first = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert first.status_code == 302

        replay = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": state},
        )
        assert replay.status_code == 409
        assert problem_body(replay)["code"] == "oauth_state_used"


async def test_github_connect_uses_app_flow_and_persists_read_scope() -> None:
    settings = _settings()
    requests: list[httpx.Request] = []
    app = create_app(settings)
    app.state.oauth_transport = _github_handler(requests)
    with TestClient(app) as client:
        token, _ = _dev_token(client)
        _url, query = _start(client, token, "github")
        assert "scope" not in query
        assert query["code_challenge_method"] == "S256"
        started = client.post(
            "/api/v1/integrations/github/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        state = started.json()["state"]
        callback = client.get(
            "/api/v1/integrations/github/callback",
            params={"code": "github-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert "connected=github" in callback.headers["location"]
        listed = client.get(
            "/api/v1/integrations", headers={"Authorization": f"Bearer {token}"}
        ).json()["items"]
        assert listed[0]["provider"] == "github"
        assert listed[0]["account_identifier"] == "octo@example.com"
        assert listed[0]["granted_scopes"] == ["repo:read"]


async def test_callback_rejects_provider_mismatch() -> None:
    settings = _settings()
    requests: list[httpx.Request] = []
    app = create_app(settings)
    app.state.oauth_transport = _provider_handler(requests)
    with TestClient(app) as client:
        token, _ = _dev_token(client)
        started = client.post(
            "/api/v1/integrations/gcal/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        state = started.json()["state"]
        response = client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": state},
        )
        assert response.status_code == 422


async def test_disconnect_revokes_provider_and_deletes_ciphertext() -> None:
    settings = _settings()
    requests: list[httpx.Request] = []
    app = create_app(settings)
    app.state.oauth_transport = _provider_handler(requests)
    with TestClient(app) as client:
        token, _ = _dev_token(client)
        started = client.post(
            "/api/v1/integrations/gmail/connect",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        state = started.json()["state"]
        client.get(
            "/api/v1/integrations/gmail/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        integration_id = client.get(
            "/api/v1/integrations", headers={"Authorization": f"Bearer {token}"}
        ).json()["items"][0]["id"]
        revoked = client.delete(
            f"/api/v1/integrations/{integration_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revoked.status_code == 204

    paths = [request.url.path for request in requests]
    assert paths[-1] == "/revoke"

    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            remaining = (
                await connection.execute(
                    select(OAuthCredential).where(
                        OAuthCredential.integration_id == uuid.UUID(integration_id)
                    )
                )
            ).scalar_one_or_none()
            assert remaining is None
    finally:
        await engine.dispose()
