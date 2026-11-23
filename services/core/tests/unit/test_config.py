from __future__ import annotations

import pytest
from pydantic import ValidationError
from zara.config import Settings

BASE = {
    "_env_file": None,
    "database_url": "postgresql+asyncpg://zara:zara_local_dev@localhost:5432/zara",
}


def test_defaults_are_local_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    settings = Settings(**BASE)

    assert settings.environment == "local"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.cors_origin_list == ["http://localhost:3000"]
    assert settings.openapi_enabled is True
    assert settings.redis_url is None


def test_database_url_must_use_asyncpg() -> None:
    with pytest.raises(ValidationError, match="async SQLAlchemy driver"):
        Settings(_env_file=None, database_url="postgresql://zara:secret@localhost/zara")


def test_cors_origins_accept_comma_separated_values() -> None:
    settings = Settings(
        **{**BASE, "cors_origins": "http://localhost:3000, https://app.zara.app"},
    )

    assert settings.cors_origin_list == ["http://localhost:3000", "https://app.zara.app"]


def test_cors_origin_rejects_paths() -> None:
    with pytest.raises(ValidationError, match="scheme://host"):
        Settings(**{**BASE, "cors_origins": "https://app.zara.app/api"})


def test_allowed_hosts_reject_urls() -> None:
    with pytest.raises(ValidationError, match="bare hosts"):
        Settings(**{**BASE, "allowed_hosts": "https://zara.app"})


def test_empty_allowed_hosts_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one host"):
        Settings(**{**BASE, "allowed_hosts": ""})


def test_invalid_pool_size_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "database_pool_size": 0})


def test_api_prefix_cannot_end_with_slash() -> None:
    with pytest.raises(ValidationError, match="must not end"):
        Settings(**{**BASE, "api_v1_prefix": "/api/v1/"})


def test_production_requires_https_origins() -> None:
    with pytest.raises(ValidationError, match="https"):
        Settings(
            **{
                **BASE,
                "environment": "production",
                "cors_origins": "http://app.zara.app",
                "database_url": "postgresql+asyncpg://zara:Str0ngPass@db:5432/zara",
                "allowed_hosts": "app.zara.app",
            }
        )


def test_production_rejects_placeholder_password() -> None:
    with pytest.raises(ValidationError, match="insecure placeholder password"):
        Settings(
            **{
                **BASE,
                "environment": "production",
                "cors_origins": "https://app.zara.app",
                "allowed_hosts": "app.zara.app",
            }
        )


def test_production_accepts_https_and_real_password() -> None:
    settings = Settings(
        **{
            **BASE,
            "environment": "production",
            "cors_origins": "https://app.zara.app",
            "allowed_hosts": "app.zara.app",
            "database_url": "postgresql+asyncpg://zara:Str0ngPass@db:5432/zara",
            "auth_provider": "clerk",
            "auth_issuer": "https://clerk.zara.app",
            "auth_client_id": "zara_app",
            "auth_jwks_url": "https://clerk.zara.app/.well-known/jwks.json",
        }
    )

    assert settings.cors_origin_list == ["https://app.zara.app"]


def test_safe_database_target_never_includes_credentials() -> None:
    settings = Settings(**BASE)

    assert settings.safe_database_target == "localhost:5432/zara"
    assert "zara_local_dev" not in settings.safe_database_target


def test_redis_url_is_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(**{**BASE, "redis_url": "http://redis:6379"})
