"""Google tool definitions with typed input/output schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from zara.tools.spec import ApprovalPolicy, ToolSpec

_GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
_CALENDAR = "https://www.googleapis.com/auth/calendar.events"
_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"


class SearchThreadsIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=10, ge=1, le=25)
    newer_than: str | None = None


class ThreadOut(BaseModel):
    id: str
    subject: str | None
    participants: list[str]
    snippet: str | None
    last_message_at: datetime | None


class SearchThreadsOut(BaseModel):
    threads: list[ThreadOut]


class ReadThreadIn(BaseModel):
    thread_id: str = Field(min_length=1)
    max_messages: int = Field(default=10, ge=1, le=20)


class MessageOut(BaseModel):
    id: str
    subject: str | None
    from_: str | None = Field(default=None, alias="from")
    to: list[str]
    body_text: str | None


class ReadThreadOut(BaseModel):
    thread_id: str
    messages: list[MessageOut]


class CreateDraftIn(BaseModel):
    to: list[str] = Field(min_length=1, max_length=25)
    cc: list[str] = Field(default_factory=list, max_length=25)
    subject: str = Field(min_length=1, max_length=300)
    body_text: str = Field(min_length=1, max_length=50_000)
    reply_to_thread_id: str | None = None


class DraftOut(BaseModel):
    draft_id: str
    gmail_url: str | None = None


class ListEventsIn(BaseModel):
    time_min: datetime
    time_max: datetime
    calendar_ids: list[str] = Field(default_factory=list)
    query: str | None = None


class EventOut(BaseModel):
    id: str
    title: str | None
    start: datetime | None
    end: datetime | None
    attendees: list[str]
    location: str | None


class ListEventsOut(BaseModel):
    events: list[EventOut]


class CreateHoldIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    start: datetime
    end: datetime
    description: str | None = Field(default=None, max_length=5_000)


class EventRefOut(BaseModel):
    event_id: str


class CreateEventIn(CreateHoldIn):
    attendees: list[str] = Field(default_factory=list, max_length=50)
    send_updates: Literal["all", "externalOnly", "none"] = "none"


class RespondToInviteIn(BaseModel):
    event_id: str = Field(min_length=1)
    response: Literal["accepted", "declined", "tentative"]


class DeleteEventIn(BaseModel):
    event_id: str = Field(min_length=1)


class SearchFilesIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    folder_ids: list[str] = Field(default_factory=list)
    mime_types: list[str] = Field(default_factory=list)


class FileOut(BaseModel):
    id: str
    name: str
    mime_type: str | None
    modified_at: datetime | None


class SearchFilesOut(BaseModel):
    files: list[FileOut]


class ReadFileIn(BaseModel):
    file_id: str = Field(min_length=1)
    max_chars: int = Field(default=10_000, ge=1, le=20_000)


class FileTextOut(BaseModel):
    file_id: str
    text: str
    truncated: bool


class CreateDocDraftIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content_md: str = Field(min_length=1, max_length=200_000)
    folder_id: str | None = None


class DocRefOut(BaseModel):
    file_id: str
    url: str | None = None


class ShareIn(BaseModel):
    file_id: str = Field(min_length=1)
    principal: str = Field(min_length=3, max_length=320)
    role: Literal["reader", "commenter", "writer"]


class ShareOut(BaseModel):
    permission_id: str


def _spec(
    *,
    name: str,
    description: str,
    domains: frozenset[str],
    integration: Literal["gmail", "gcal", "gdrive"],
    scopes: tuple[str, ...],
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    side_effect: Literal["read", "write_reversible", "write_external", "destructive"],
    tier: int,
    idempotency: Literal["none_read_only", "key_param", "natural_key"],
    preview: str | None = None,
    critical: tuple[str, ...] = (),
) -> ToolSpec:
    approval = (
        ApprovalPolicy.ALWAYS
        if tier >= 4
        else ApprovalPolicy.APPROVAL
        if tier == 3
        else ApprovalPolicy.AUTO
    )
    return ToolSpec(
        name=name,
        description=description,
        domains=domains,
        integration=integration,
        required_scopes=scopes,
        input_model=input_model,
        output_model=output_model,
        side_effect=side_effect,
        risk_tier=tier,
        approval=approval,
        idempotency=idempotency,
        timeout_s=15 if side_effect == "read" else 20,
        output_trust="untrusted" if side_effect == "read" else "trusted_internal",
        max_output_bytes=64_000,
        redact_args=("$.body_text", "$.content_md"),
        kill_switch_key=f"kill.tool.{name}",
        preview_renderer=preview,
        critical_args=critical,
    )


GOOGLE_SPECS = (
    _spec(
        name="gmail.search_threads",
        description="Search the user's Gmail threads with Gmail query syntax.",
        domains=frozenset({"gmail"}),
        integration="gmail",
        scopes=(_GMAIL_READ,),
        input_model=SearchThreadsIn,
        output_model=SearchThreadsOut,
        side_effect="read",
        tier=0,
        idempotency="none_read_only",
    ),
    _spec(
        name="gmail.read_thread",
        description="Read one Gmail thread and return message bodies.",
        domains=frozenset({"gmail"}),
        integration="gmail",
        scopes=(_GMAIL_READ,),
        input_model=ReadThreadIn,
        output_model=ReadThreadOut,
        side_effect="read",
        tier=0,
        idempotency="none_read_only",
    ),
    _spec(
        name="gmail.create_draft",
        description="Create a Gmail draft without sending it.",
        domains=frozenset({"gmail"}),
        integration="gmail",
        scopes=(_GMAIL_COMPOSE,),
        input_model=CreateDraftIn,
        output_model=DraftOut,
        side_effect="write_reversible",
        tier=2,
        idempotency="key_param",
        preview="render_gmail_draft_preview",
        critical=("to", "cc"),
    ),
    _spec(
        name="gcal.list_events",
        description="List calendar events in a time window.",
        domains=frozenset({"calendar"}),
        integration="gcal",
        scopes=(_CALENDAR,),
        input_model=ListEventsIn,
        output_model=ListEventsOut,
        side_effect="read",
        tier=0,
        idempotency="none_read_only",
    ),
    _spec(
        name="gcal.create_hold",
        description="Create a private calendar hold without inviting attendees.",
        domains=frozenset({"calendar"}),
        integration="gcal",
        scopes=(_CALENDAR,),
        input_model=CreateHoldIn,
        output_model=EventRefOut,
        side_effect="write_reversible",
        tier=2,
        idempotency="key_param",
        preview="render_calendar_hold_preview",
    ),
    _spec(
        name="gcal.create_event",
        description="Create a calendar event and optionally notify attendees.",
        domains=frozenset({"calendar"}),
        integration="gcal",
        scopes=(_CALENDAR,),
        input_model=CreateEventIn,
        output_model=EventRefOut,
        side_effect="write_external",
        tier=3,
        idempotency="key_param",
        preview="render_calendar_event_preview",
        critical=("attendees",),
    ),
    _spec(
        name="gcal.respond_to_invite",
        description="Respond to a calendar invitation on the user's behalf.",
        domains=frozenset({"calendar"}),
        integration="gcal",
        scopes=(_CALENDAR,),
        input_model=RespondToInviteIn,
        output_model=EventRefOut,
        side_effect="write_external",
        tier=3,
        idempotency="natural_key",
        preview="render_invite_response_preview",
        critical=("event_id",),
    ),
    _spec(
        name="gcal.delete_event",
        description="Permanently delete a calendar event.",
        domains=frozenset({"calendar"}),
        integration="gcal",
        scopes=(_CALENDAR,),
        input_model=DeleteEventIn,
        output_model=EventRefOut,
        side_effect="destructive",
        tier=4,
        idempotency="natural_key",
        preview="render_event_delete_preview",
        critical=("event_id",),
    ),
    _spec(
        name="drive.search_files",
        description="Search files the user has opened with ZARA.",
        domains=frozenset({"drive"}),
        integration="gdrive",
        scopes=(_DRIVE_FILE,),
        input_model=SearchFilesIn,
        output_model=SearchFilesOut,
        side_effect="read",
        tier=0,
        idempotency="none_read_only",
    ),
    _spec(
        name="drive.read_file",
        description="Read text from a Drive document or file.",
        domains=frozenset({"drive"}),
        integration="gdrive",
        scopes=(_DRIVE_FILE,),
        input_model=ReadFileIn,
        output_model=FileTextOut,
        side_effect="read",
        tier=0,
        idempotency="none_read_only",
    ),
    _spec(
        name="drive.create_doc_draft",
        description="Create a private Google Doc draft.",
        domains=frozenset({"drive"}),
        integration="gdrive",
        scopes=(_DRIVE_FILE,),
        input_model=CreateDocDraftIn,
        output_model=DocRefOut,
        side_effect="write_reversible",
        tier=2,
        idempotency="key_param",
        preview="render_drive_doc_preview",
        critical=("folder_id",),
    ),
    _spec(
        name="drive.share",
        description="Share a Drive file with another principal.",
        domains=frozenset({"drive"}),
        integration="gdrive",
        scopes=(_DRIVE_FILE,),
        input_model=ShareIn,
        output_model=ShareOut,
        side_effect="destructive",
        tier=4,
        idempotency="natural_key",
        preview="render_share_preview",
        critical=("principal",),
    ),
)
