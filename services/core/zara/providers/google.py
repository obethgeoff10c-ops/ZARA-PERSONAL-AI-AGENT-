"""Real Google Workspace API clients (Gmail, Calendar, Drive).

These clients never persist tokens; the caller passes a short-lived access
token and the HTTP client used for the call. All responses are parsed into the
typed Pydantic models declared by the tool specs.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

import httpx

from zara.errors import upstream_error
from zara.tools.builtin.google import (
    CreateDocDraftIn,
    CreateDraftIn,
    CreateEventIn,
    CreateHoldIn,
    DeleteEventIn,
    DocRefOut,
    DraftOut,
    EventOut,
    EventRefOut,
    FileOut,
    FileTextOut,
    ListEventsIn,
    ListEventsOut,
    MessageOut,
    ReadFileIn,
    ReadThreadIn,
    ReadThreadOut,
    RespondToInviteIn,
    SearchFilesIn,
    SearchFilesOut,
    SearchThreadsIn,
    SearchThreadsOut,
    ShareIn,
    ShareOut,
    ThreadOut,
)

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_CALENDAR = "https://www.googleapis.com/calendar/v3"
_DRIVE = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
_DOC_MIME = "application/vnd.google-apps.document"
_FOLDER_MIME = "application/vnd.google-apps.folder"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _raise(response: httpx.Response, provider: str) -> None:
    if response.status_code < 400:
        return
    # Never echo response bodies; Gmail bodies can contain private content.
    raise upstream_error(f"{provider} request failed (status {response.status_code}).")


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _headers_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers") or []
    return {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in headers}


def _plain_texts(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    mime = payload.get("mimeType")
    data = (payload.get("body") or {}).get("data")
    if mime == "text/plain" and data:
        texts.append(_b64url_decode(str(data)))
    for part in payload.get("parts") or []:
        texts.extend(_plain_texts(part))
    return texts


def _message_text(payload: dict[str, Any]) -> str | None:
    plain = _plain_texts(payload)
    if plain:
        return "\n".join(plain)
    data = (payload.get("body") or {}).get("data")
    if data:
        return _b64url_decode(str(data))
    for part in payload.get("parts") or []:
        found = _message_text(part)
        if found:
            return found
    return None


def _thread_summary(thread: dict[str, Any]) -> ThreadOut:
    messages: list[dict[str, Any]] = thread.get("messages") or []
    last = messages[-1] if messages else {}
    headers = _headers_map(last.get("payload") or {})
    participants: list[str] = []
    for key in ("from", "to"):
        value = headers.get(key)
        if value:
            participants.extend(part.strip() for part in value.split(",") if part.strip())
    last_at = None
    internal = last.get("internalDate")
    if internal:
        last_at = datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    return ThreadOut(
        id=str(thread.get("id", "")),
        subject=headers.get("subject"),
        participants=participants,
        snippet=thread.get("snippet"),
        last_message_at=last_at,
    )


async def search_threads(
    client: httpx.AsyncClient, access_token: str, params: SearchThreadsIn
) -> SearchThreadsOut:
    query = params.query
    if params.newer_than:
        query = f"{query} newer_than:{params.newer_than}"
    response = await client.get(
        f"{_GMAIL}/threads",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": query, "maxResults": params.max_results},
    )
    _raise(response, "Gmail search")
    body: dict[str, Any] = response.json()
    threads: list[ThreadOut] = []
    for item in body.get("threads") or []:
        detail = await client.get(
            f"{_GMAIL}/threads/{item['id']}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "To"],
            },
        )
        _raise(detail, "Gmail thread")
        threads.append(_thread_summary(detail.json()))
    return SearchThreadsOut(threads=threads)


async def read_thread(
    client: httpx.AsyncClient, access_token: str, params: ReadThreadIn
) -> ReadThreadOut:
    response = await client.get(
        f"{_GMAIL}/threads/{quote(params.thread_id, safe='')}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "full"},
    )
    _raise(response, "Gmail thread")
    body: dict[str, Any] = response.json()
    messages: list[MessageOut] = []
    for message in (body.get("messages") or [])[: params.max_messages]:
        payload = message.get("payload") or {}
        headers = _headers_map(payload)
        to_header = headers.get("to") or ""
        messages.append(
            MessageOut(
                id=str(message.get("id", "")),
                subject=headers.get("subject"),
                **{"from": headers.get("from")},
                to=[part.strip() for part in to_header.split(",") if part.strip()],
                body_text=_message_text(payload),
            )
        )
    return ReadThreadOut(thread_id=str(body.get("id", params.thread_id)), messages=messages)


async def create_draft(
    client: httpx.AsyncClient, access_token: str, params: CreateDraftIn
) -> DraftOut:
    message = EmailMessage()
    message["To"] = ", ".join(params.to)
    if params.cc:
        message["Cc"] = ", ".join(params.cc)
    message["Subject"] = params.subject
    if params.reply_to_thread_id:
        message["X-ZARA-Thread"] = params.reply_to_thread_id
    message.set_content(params.body_text)

    payload: dict[str, Any] = {"message": {"raw": _b64url_encode(message.as_bytes())}}
    if params.reply_to_thread_id:
        payload["message"]["threadId"] = params.reply_to_thread_id
    response = await client.post(
        f"{_GMAIL}/drafts",
        headers={"Authorization": f"Bearer {access_token}"},
        json=payload,
    )
    _raise(response, "Gmail draft")
    body = response.json()
    draft_id = str(body.get("id", ""))
    return DraftOut(
        draft_id=draft_id, gmail_url=f"https://mail.google.com/mail/u/0/#drafts/{draft_id}"
    )


def _event_out(event: dict[str, Any]) -> EventOut:
    start = event.get("start") or {}
    end = event.get("end") or {}
    attendees = [
        str(item.get("email")) for item in event.get("attendees") or [] if item.get("email")
    ]
    return EventOut(
        id=str(event.get("id", "")),
        title=event.get("summary"),
        start=_parse_dt(start.get("dateTime") or start.get("date")),
        end=_parse_dt(end.get("dateTime") or end.get("date")),
        attendees=attendees,
        location=event.get("location"),
    )


async def list_events(
    client: httpx.AsyncClient, access_token: str, params: ListEventsIn
) -> ListEventsOut:
    calendars = params.calendar_ids or ["primary"]
    events: list[EventOut] = []
    for calendar_id in calendars:
        response = await client.get(
            f"{_CALENDAR}/calendars/{quote(calendar_id, safe='')}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "timeMin": _iso(params.time_min),
                "timeMax": _iso(params.time_max),
                "singleEvents": "true",
                "orderBy": "startTime",
                **({"q": params.query} if params.query else {}),
            },
        )
        _raise(response, "Calendar list")
        body: dict[str, Any] = response.json()
        events.extend(_event_out(item) for item in body.get("items") or [])
    events.sort(key=lambda item: item.start or datetime.max.replace(tzinfo=UTC))
    return ListEventsOut(events=events)


async def create_hold(
    client: httpx.AsyncClient, access_token: str, params: CreateHoldIn
) -> EventRefOut:
    response = await client.post(
        f"{_CALENDAR}/calendars/primary/events",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"sendUpdates": "none"},
        json={
            "summary": params.title,
            "description": params.description,
            "start": {"dateTime": _iso(params.start)},
            "end": {"dateTime": _iso(params.end)},
            "visibility": "private",
            "transparency": "opaque",
        },
    )
    _raise(response, "Calendar hold")
    return EventRefOut(event_id=str(response.json().get("id", "")))


async def create_event(
    client: httpx.AsyncClient, access_token: str, params: CreateEventIn
) -> EventRefOut:
    response = await client.post(
        f"{_CALENDAR}/calendars/primary/events",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"sendUpdates": params.send_updates},
        json={
            "summary": params.title,
            "description": params.description,
            "start": {"dateTime": _iso(params.start)},
            "end": {"dateTime": _iso(params.end)},
            "attendees": [{"email": email} for email in params.attendees],
        },
    )
    _raise(response, "Calendar event")
    return EventRefOut(event_id=str(response.json().get("id", "")))


async def respond_to_invite(
    client: httpx.AsyncClient, access_token: str, params: RespondToInviteIn
) -> EventRefOut:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{_CALENDAR}/calendars/primary/events/{quote(params.event_id, safe='')}"
    current = await client.get(url, headers=headers)
    _raise(current, "Calendar event read")
    body: dict[str, Any] = current.json()
    attendees = body.get("attendees") or []
    updated = False
    for attendee in attendees:
        if attendee.get("self"):
            attendee["responseStatus"] = params.response
            updated = True
    if not updated:
        attendees.append({"self": True, "responseStatus": params.response})
    response = await client.patch(
        url,
        headers=headers,
        params={"sendUpdates": "none"},
        json={"attendees": attendees},
    )
    _raise(response, "Calendar invite response")
    return EventRefOut(event_id=params.event_id)


async def delete_event(
    client: httpx.AsyncClient, access_token: str, params: DeleteEventIn
) -> EventRefOut:
    response = await client.delete(
        f"{_CALENDAR}/calendars/primary/events/{quote(params.event_id, safe='')}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    _raise(response, "Calendar delete")
    return EventRefOut(event_id=params.event_id)


_FILE_FIELDS = "files(id,name,mimeType,modifiedTime,parents)"


async def search_files(
    client: httpx.AsyncClient, access_token: str, params: SearchFilesIn
) -> SearchFilesOut:
    clauses = ["trashed = false"]
    escaped = params.query.replace("\\", "\\\\").replace("'", "\\'")
    clauses.append(f"fullText contains '{escaped}'")
    if params.folder_ids:
        parents = " or ".join(f"'{folder}' in parents" for folder in params.folder_ids)
        clauses.append(f"({parents})")
    if params.mime_types:
        types = " or ".join(f"mimeType = '{mime}'" for mime in params.mime_types)
        clauses.append(f"({types})")
    response = await client.get(
        f"{_DRIVE}/files",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"q": " and ".join(clauses), "fields": _FILE_FIELDS, "pageSize": 50},
    )
    _raise(response, "Drive search")
    body: dict[str, Any] = response.json()
    files = [
        FileOut(
            id=str(item.get("id", "")),
            name=str(item.get("name", "")),
            mime_type=item.get("mimeType"),
            modified_at=_parse_dt(item.get("modifiedTime")),
        )
        for item in body.get("files") or []
    ]
    return SearchFilesOut(files=files)


async def read_file(
    client: httpx.AsyncClient, access_token: str, params: ReadFileIn
) -> FileTextOut:
    headers = {"Authorization": f"Bearer {access_token}"}
    file_id = quote(params.file_id, safe="")
    meta = await client.get(
        f"{_DRIVE}/files/{file_id}", headers=headers, params={"fields": "mimeType"}
    )
    _raise(meta, "Drive file")
    mime = str((meta.json() or {}).get("mimeType", ""))
    if mime.startswith("application/vnd.google-apps"):
        response = await client.get(
            f"{_DRIVE}/files/{file_id}/export",
            headers=headers,
            params={"mimeType": "text/plain"},
        )
    else:
        response = await client.get(
            f"{_DRIVE}/files/{file_id}", headers=headers, params={"alt": "media"}
        )
    _raise(response, "Drive read")
    text = response.text
    truncated = len(text) > params.max_chars
    return FileTextOut(file_id=params.file_id, text=text[: params.max_chars], truncated=truncated)


async def create_doc_draft(
    client: httpx.AsyncClient, access_token: str, params: CreateDocDraftIn
) -> DocRefOut:
    metadata: dict[str, Any] = {"name": params.title, "mimeType": _DOC_MIME}
    if params.folder_id:
        metadata["parents"] = [params.folder_id]
    boundary = "zara_doc_boundary"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/markdown; charset=UTF-8\r\n\r\n"
        f"{params.content_md}\r\n"
        f"--{boundary}--"
    )
    response = await client.post(
        f"{_DRIVE_UPLOAD}/files",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        params={"uploadType": "multipart", "fields": "id,webViewLink"},
        content=body.encode("utf-8"),
    )
    _raise(response, "Drive create")
    payload = response.json()
    return DocRefOut(file_id=str(payload.get("id", "")), url=payload.get("webViewLink"))


async def share_file(client: httpx.AsyncClient, access_token: str, params: ShareIn) -> ShareOut:
    response = await client.post(
        f"{_DRIVE}/files/{quote(params.file_id, safe='')}/permissions",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"sendNotificationEmail": "true", "fields": "id"},
        json={
            "type": "user",
            "role": params.role,
            "emailAddress": params.principal,
        },
    )
    _raise(response, "Drive share")
    return ShareOut(permission_id=str(response.json().get("id", "")))
