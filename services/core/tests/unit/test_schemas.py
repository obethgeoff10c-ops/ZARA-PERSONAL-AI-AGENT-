"""Unit tests for request-schema validation boundaries."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from zara.schemas import (
    ConversationCreate,
    ConversationUpdate,
    DevTokenIn,
    FeedbackIn,
    MemoryCreate,
    MemoryUpdate,
    MessageCreate,
    ProfileUpdate,
    RunCreate,
    TaskCreate,
    TaskUpdate,
)


def _invalid(model: type[object], **payload: object) -> str:
    with pytest.raises(ValidationError) as excinfo:
        model(**payload)  # type: ignore[call-arg]
    return str(excinfo.value)


def test_dev_token_rejects_malformed_email() -> None:
    message = _invalid(DevTokenIn, email="not-an-email")
    assert "email" in message


def test_dev_token_rejects_over_length_display_name_and_org_id() -> None:
    assert "display_name" in _invalid(DevTokenIn, email="a@example.com", display_name="x" * 201)
    assert "org_id" in _invalid(DevTokenIn, email="a@example.com", org_id="o" * 201)


def test_task_create_requires_non_empty_title_and_bounded_priority() -> None:
    assert "title" in _invalid(TaskCreate)
    assert "title" in _invalid(TaskCreate, title="")
    assert "title" in _invalid(TaskCreate, title="t" * 301)
    assert "priority" in _invalid(TaskCreate, title="ok", priority=9)
    assert "description" in _invalid(TaskCreate, title="ok", description="d" * 10_001)


def test_task_update_rejects_invalid_status_enum() -> None:
    message = _invalid(TaskUpdate, status="sleeping")
    assert "status" in message


def test_conversation_schemas_validate_lengths() -> None:
    assert "title" in _invalid(ConversationCreate, title="t" * 301)
    assert "title" in _invalid(ConversationUpdate, title="")


def test_message_and_feedback_schemas_validate_content() -> None:
    assert "content" in _invalid(MessageCreate, content="")
    assert "content" in _invalid(MessageCreate, content="c" * 20_001)
    assert "feedback" in _invalid(FeedbackIn, feedback="meh")


def test_memory_schemas_validate_kind_content_and_importance() -> None:
    assert "kind" in _invalid(MemoryCreate, kind="", content="body")
    assert "kind" in _invalid(MemoryCreate, kind="k" * 33, content="body")
    assert "content" in _invalid(MemoryCreate, kind="semantic")
    assert "importance" in _invalid(MemoryCreate, kind="semantic", content="body", importance=2)
    assert "content" in _invalid(MemoryUpdate, content="")


def test_profile_update_rejects_over_length_fields() -> None:
    assert "bio_summary" in _invalid(ProfileUpdate, bio_summary="b" * 2001)
    assert "timezone" in _invalid(ProfileUpdate, timezone="z" * 65)
    assert "locale" in _invalid(ProfileUpdate, locale="l" * 17)


def test_run_create_validates_goal_mode_and_domain_count() -> None:
    assert "goal" in _invalid(RunCreate, goal="")
    assert "goal" in _invalid(RunCreate, goal="g" * 2001)
    assert "mode" in _invalid(RunCreate, goal="ok", mode="sideways")
    assert "domains" in _invalid(RunCreate, goal="ok", domains=["personal"] * 9)
    assert "conversation_id" in _invalid(RunCreate, goal="ok", conversation_id="not-a-uuid")


def test_valid_payloads_pass() -> None:
    token = DevTokenIn(email="user@example.com")
    assert token.email == "user@example.com"
    task = TaskCreate(title="Write tests", priority=4)
    assert task.priority == 4
    memory = MemoryCreate(kind="semantic", content="remember this")
    assert memory.importance == 0.5
    conversation = ConversationCreate(title="Chat")
    assert conversation.title == "Chat"
    run = RunCreate(goal="do it", conversation_id=uuid.uuid4())
    assert run.mode is None
