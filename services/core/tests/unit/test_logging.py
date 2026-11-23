from __future__ import annotations

import json
import logging

from zara.config import Settings
from zara.observability.context import reset_request_id, set_request_id
from zara.observability.logging import JsonFormatter, redact


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url="postgresql+asyncpg://zara:zara_local_dev@localhost:5432/zara",
        log_level="INFO",
    )


def test_redact_removes_nested_secret_values() -> None:
    payload = {
        "authorization": "Bearer abcdefghijklmnop",
        "user": {"email": "a@b.co", "password": "hunter2"},
        "items": [{"access_token": "tok_123"}],
        "message": "password=hunter2 token=abc",
    }

    result = redact(payload)

    assert result["authorization"] == "[REDACTED]"
    assert result["user"]["password"] == "[REDACTED]"
    assert result["items"][0]["access_token"] == "[REDACTED]"
    assert "hunter2" not in json.dumps(result)
    assert "tok_123" not in json.dumps(result)
    assert "a@b.co" in json.dumps(result)


def test_redact_masks_bearer_tokens_in_free_text() -> None:
    value = "upstream rejected Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"

    result = redact(value)

    assert result == "upstream rejected Bearer [REDACTED]"


def test_json_formatter_emits_structured_record_with_request_id() -> None:
    token = set_request_id("req-123")
    try:
        record = logging.LogRecord(
            name="zara.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        record.event = "test.event"
        record.duration_ms = 1.5
        formatted = JsonFormatter(_settings()).format(record)
    finally:
        reset_request_id(token)

    payload = json.loads(formatted)
    assert payload["message"] == "hello world"
    assert payload["level"] == "info"
    assert payload["service"] == "zara-api"
    assert payload["environment"] == "test"
    assert payload["request_id"] == "req-123"
    assert payload["event"] == "test.event"
    assert payload["duration_ms"] == 1.5
    assert payload["timestamp"].endswith("Z")
