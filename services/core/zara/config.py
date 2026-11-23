"""Typed application configuration.

Every environment variable is validated here. Application code must import
``get_settings`` (or receive ``Settings``) rather than reading ``os.environ``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, RedisDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

Environment = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_DEFAULT_DEV_PASSWORD = "zara_local_dev"  # noqa: S105 - documented local-only default
_INSECURE_PASSWORDS = frozenset(
    {
        _DEFAULT_DEV_PASSWORD,
        "replace-me",
        "postgres",
        "password",
        "changeme",
        "secret",
        "zara",
    }
)

_DATABASE_SCHEMES = ("postgresql+asyncpg://",)


def _origin(value: AnyHttpUrl) -> str:
    """Return a CORS-comparable origin without a trailing slash."""
    default_port = {"http": 80, "https": 443}.get(value.scheme, 0)
    port = value.port
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{value.scheme}://{value.host}{suffix}"


class Settings(BaseSettings):
    """Validated process configuration."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = "local"
    service_name: str = "zara-api"
    api_v1_prefix: str = "/api/v1"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    log_level: LogLevel = "INFO"
    openapi_enabled: bool = True

    database_url: str
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=10, ge=0, le=50)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86_400)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    database_statement_timeout_ms: int = Field(default=15_000, ge=100, le=300_000)

    redis_url: RedisDsn | None = None
    redis_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    # Authentication (D-05): a managed IdP verifies users; ZARA never stores passwords.
    # ``local`` is a self-contained HS256 provider for Compose/tests only and is
    # rejected in production. ``clerk`` and ``oidc`` verify RS256/ES256 JWTs via JWKS.
    auth_provider: Literal["local", "clerk", "oidc"] = "local"
    auth_issuer: str = "http://localhost:8000"
    auth_audience: str = "zara-local"
    auth_client_id: str | None = "zara-local"
    auth_jwks_url: AnyHttpUrl | None = None
    auth_local_secret: SecretStr = SecretStr("replace-me-local-auth-secret-change-me")
    auth_jwt_algorithms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["RS256", "ES256"]
    )
    auth_jwks_cache_seconds: int = Field(default=300, ge=30, le=3600)
    auth_session_ttl_seconds: int = Field(default=2_592_000, ge=300, le=31_536_000)

    # Abuse controls. Redis is used when configured; the in-process fallback keeps
    # single-node development honest without making Redis a hard dependency.
    rate_limit_enabled: bool = True
    auth_rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    global_rate_limit_per_minute: int = Field(default=600, ge=1, le=10_000)

    default_page_size: int = Field(default=50, ge=1, le=100)
    max_page_size: int = Field(default=100, ge=1, le=500)

    sse_heartbeat_seconds: float = Field(default=15.0, gt=0, le=60)
    sse_poll_seconds: float = Field(default=0.5, gt=0, le=5)

    # Comma-separated in the environment, e.g. "http://localhost:3000,https://app.zara.app".
    cors_origins: Annotated[list[AnyHttpUrl], NoDecode] = Field(
        default_factory=lambda: [AnyHttpUrl("http://localhost:3000")]
    )
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"]
    )

    # Set only when the deployment terminates TLS and trusts a proxy header.
    trust_proxy_headers: bool = False

    @field_validator("cors_origins", "allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("auth_jwt_algorithms", mode="before")
    @classmethod
    def _split_algorithms(cls, value: object) -> object:
        if value is None or value == "":
            return ["RS256", "ES256"]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("auth_jwt_algorithms")
    @classmethod
    def _validate_algorithms(cls, values: list[str]) -> list[str]:
        allowed = {"RS256", "RS384", "RS512", "ES256", "ES384", "HS256"}
        invalid = sorted(set(values) - allowed)
        if invalid:
            raise ValueError(f"Unsupported AUTH_JWT_ALGORITHMS values: {invalid}")
        if not values:
            raise ValueError("AUTH_JWT_ALGORITHMS must contain at least one algorithm")
        return values

    @field_validator("auth_local_secret")
    @classmethod
    def _validate_local_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("AUTH_LOCAL_SECRET must be at least 32 characters")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _validate_origins(cls, values: list[AnyHttpUrl]) -> list[AnyHttpUrl]:
        for value in values:
            if value.path not in (None, "", "/") or value.query or value.fragment:
                raise ValueError(f"CORS origin must be scheme://host[:port], got {value}")
            if value.username or value.password:
                raise ValueError("CORS origins must not contain credentials")
        return values

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_hosts(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            host = value.strip().lower()
            if not host:
                continue
            if "://" in host or "/" in host:
                raise ValueError(f"ALLOWED_HOSTS entries must be bare hosts, got {value}")
            cleaned.append(host)
        if not cleaned:
            raise ValueError("ALLOWED_HOSTS must contain at least one host")
        return cleaned

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(_DATABASE_SCHEMES):
            raise ValueError(
                "DATABASE_URL must use the async SQLAlchemy driver: "
                "postgresql+asyncpg://user:password@host:5432/database"
            )
        return value

    @field_validator("api_v1_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/"):
            raise ValueError("API_V1_PREFIX must start with '/' and must not end with '/'")
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.environment != "production":
            return self

        insecure = [str(origin) for origin in self.cors_origins if origin.scheme != "https"]
        if insecure:
            raise ValueError(f"Production CORS origins must use https: {insecure}")

        if "*" in self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must not contain '*' in production")

        parts = urlsplit(self.database_url)
        password = parts.password or ""
        if not password:
            raise ValueError("DATABASE_URL must include a password in production")
        if password.lower() in _INSECURE_PASSWORDS:
            raise ValueError("DATABASE_URL uses an insecure placeholder password")

        if self.auth_provider == "local":
            raise ValueError("AUTH_PROVIDER=local is not allowed in production; use clerk or oidc")
        if self.auth_jwks_url is None:
            raise ValueError("AUTH_JWKS_URL is required in production")
        if not self.auth_issuer.startswith("https://"):
            raise ValueError("AUTH_ISSUER must use https in production")
        if not self.auth_client_id:
            raise ValueError("AUTH_CLIENT_ID is required in production")

        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [_origin(origin) for origin in self.cors_origins]

    @property
    def safe_database_target(self) -> str:
        """Database host/db for logs — never includes credentials."""
        parts = urlsplit(self.database_url)
        database = parts.path.lstrip("/") or "unknown"
        return f"{parts.hostname or 'unknown'}:{parts.port or 5432}/{database}"

    @property
    def redis_url_str(self) -> str | None:
        return str(self.redis_url) if self.redis_url is not None else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, failing fast on invalid configuration."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests and reload tooling)."""
    get_settings.cache_clear()


# ``SecretStr`` is re-exported so future secret fields stay in one module.
__all__ = ["REPO_ROOT", "Environment", "LogLevel", "SecretStr", "Settings", "get_settings"]
