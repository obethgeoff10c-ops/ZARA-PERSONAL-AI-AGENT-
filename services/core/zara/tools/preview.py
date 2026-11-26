"""Human-readable approval previews rendered from the exact execution args.

Renderers are registered by name in ``ToolSpec.preview_renderer``. They must be
pure: no provider calls, no database access.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Preview = Callable[[dict[str, Any]], dict[str, Any]]


def _summary(title: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"title": title, "fields": fields}


def render_gmail_draft_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary(
        "Create Gmail draft",
        {
            "To": ", ".join(args.get("to") or []),
            "Cc": ", ".join(args.get("cc") or []),
            "Subject": args.get("subject"),
            "Body": args.get("body_text"),
        },
    )


def render_calendar_hold_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary(
        "Create private calendar hold",
        {
            "Title": args.get("title"),
            "Start": args.get("start"),
            "End": args.get("end"),
            "Description": args.get("description"),
        },
    )


def render_calendar_event_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary(
        "Create calendar event and notify attendees",
        {
            "Title": args.get("title"),
            "Start": args.get("start"),
            "End": args.get("end"),
            "Attendees": ", ".join(args.get("attendees") or []),
            "Send updates": args.get("send_updates", "none"),
        },
    )


def render_invite_response_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary(
        "Respond to calendar invitation",
        {"Event": args.get("event_id"), "Response": args.get("response")},
    )


def render_event_delete_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary("Permanently delete calendar event", {"Event": args.get("event_id")})


def render_drive_doc_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary(
        "Create Google Doc draft",
        {
            "Title": args.get("title"),
            "Folder": args.get("folder_id"),
            "Content": args.get("content_md"),
        },
    )


def render_share_preview(args: dict[str, Any]) -> dict[str, Any]:
    return _summary(
        "Share Drive file",
        {
            "File": args.get("file_id"),
            "Principal": args.get("principal"),
            "Role": args.get("role"),
        },
    )


PREVIEWS: dict[str, Preview] = {
    "render_gmail_draft_preview": render_gmail_draft_preview,
    "render_calendar_hold_preview": render_calendar_hold_preview,
    "render_calendar_event_preview": render_calendar_event_preview,
    "render_invite_response_preview": render_invite_response_preview,
    "render_event_delete_preview": render_event_delete_preview,
    "render_drive_doc_preview": render_drive_doc_preview,
    "render_share_preview": render_share_preview,
}


def render(name: str, args: dict[str, Any]) -> dict[str, Any]:
    renderer = PREVIEWS.get(name)
    if renderer is None:
        return _summary(name, {"args": args})
    return renderer(args)
