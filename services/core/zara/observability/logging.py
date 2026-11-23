"""JSON logging with request correlation and credential redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from zara.config import Settings
from zara.observability.context import get_request_id

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "client_secret",
        "credential",
        "credentials",
    }
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*=\s*[^&\s,;]+")

_EXTRA_FIELDS = (
    "event",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "client_ip",
    "error_code",
)


def redact(value: Any) -> Any:
    """Recursively redact sensitive keys and common secret-shaped strings."""
    if isinstance(value, Mapping):
        return {
            key: (_REDACTED if str(key).lower() in _SENSITIVE_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        masked_bearer = _BEARER.sub(r"\1" + _REDACTED, value)
        return _KV_SECRET.sub(lambda match: f"{match.group(1)}={_REDACTED}", masked_bearer)
    return value


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line, including request metadata when present."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._service = settings.service_name
        self._environment = settings.environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "environment": self._environment,
        }

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id

        for field in _EXTRA_FIELDS:
            extra = getattr(record, field, None)
            if extra is not None:
                payload[field] = extra

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(settings: Settings, *, force: bool = True) -> None:
    """Configure the root logger for the process.

    ``force`` replaces handlers installed by libraries or a previous call. It is
    safe to call once per process; tests that need to inspect log records should
    attach their own handler instead of calling this function.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings))

    logging.basicConfig(
        level=settings.log_level,
        handlers=[handler],
        force=force,
    )

    for noisy_logger in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(noisy_logger)
        logger.handlers.clear()
        logger.propagate = True

    logging.captureWarnings(True)
