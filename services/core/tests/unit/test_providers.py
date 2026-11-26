"""Unit tests for provider clients using httpx MockTransport (no network)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from zara.errors import AppError
from zara.providers import github, google
from zara.tools.builtin import github as gh_specs
from zara.tools.builtin import google as g_specs

TOKEN = "ya29.fake-access-token"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


async def test_gmail_search_reads_thread_metadata() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path.endswith("/threads"):
            assert request.url.params["q"] == "from:ana newer_than:7d"
            return httpx.Response(200, json={"threads": [{"id": "t1"}]})
        return httpx.Response(
            200,
            json={
                "id": "t1",
                "messages": [
                    {
                        "id": "m1",
                        "internalDate": "1735689600000",
                        "snippet": "Lunch?",
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "Lunch"},
                                {"name": "From", "value": "Ana <ana@example.com>"},
                                {"name": "To", "value": "me@example.com"},
                            ]
                        },
                    }
                ],
            },
        )

    async with _client(handler) as client:
        result = await google.search_threads(
            client,
            TOKEN,
            g_specs.SearchThreadsIn(query="from:ana", newer_than="7d"),
        )

    assert len(result.threads) == 1
    assert result.threads[0].subject == "Lunch"
    assert result.threads[0].participants == ["Ana <ana@example.com>", "me@example.com"]
    assert result.threads[0].last_message_at == datetime(2025, 1, 1, tzinfo=UTC)
    assert len(seen) == 2


async def test_gmail_read_thread_decodes_plain_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "t1",
                "messages": [
                    {
                        "id": "m1",
                        "payload": {
                            "mimeType": "multipart/alternative",
                            "headers": [
                                {"name": "Subject", "value": "Plan"},
                                {"name": "From", "value": "ana@example.com"},
                                {"name": "To", "value": "me@example.com"},
                            ],
                            "parts": [
                                {"mimeType": "text/html", "body": {"data": _b64("<p>no</p>")}},
                                {"mimeType": "text/plain", "body": {"data": _b64("hello")}},
                            ],
                        },
                    }
                ],
            },
        )

    async with _client(handler) as client:
        result = await google.read_thread(client, TOKEN, g_specs.ReadThreadIn(thread_id="t1"))

    assert result.messages[0].body_text == "hello"
    assert result.messages[0].from_ == "ana@example.com"


async def test_gmail_create_draft_builds_rfc822_message() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "d1"})

    async with _client(handler) as client:
        result = await google.create_draft(
            client,
            TOKEN,
            g_specs.CreateDraftIn(
                to=["ana@example.com"],
                subject="Hello",
                body_text="Draft body",
            ),
        )

    raw = base64.urlsafe_b64decode(captured["message"]["raw"] + "==").decode()
    assert "To: ana@example.com" in raw
    assert "Subject: Hello" in raw
    assert result.draft_id == "d1"


async def test_calendar_list_and_create_hold() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "e1",
                            "summary": "Standup",
                            "start": {"dateTime": "2026-01-01T10:00:00Z"},
                            "end": {"dateTime": "2026-01-01T10:15:00Z"},
                            "attendees": [{"email": "ana@example.com"}],
                        }
                    ]
                },
            )
        body = json.loads(request.content)
        assert body["visibility"] == "private"
        assert "attendees" not in body
        return httpx.Response(200, json={"id": "e2"})

    async with _client(handler) as client:
        listed = await google.list_events(
            client,
            TOKEN,
            g_specs.ListEventsIn(
                time_min=datetime(2026, 1, 1, tzinfo=UTC),
                time_max=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        )
        hold = await google.create_hold(
            client,
            TOKEN,
            g_specs.CreateHoldIn(
                title="Focus",
                start=datetime(2026, 1, 1, 12, tzinfo=UTC),
                end=datetime(2026, 1, 1, 13, tzinfo=UTC),
            ),
        )

    assert listed.events[0].title == "Standup"
    assert hold.event_id == "e2"


async def test_drive_search_read_create_and_share() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "f1",
                            "name": "Plan",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            )
        if path.endswith("/files/f1") and request.method == "GET":
            return httpx.Response(200, json={"mimeType": "application/vnd.google-apps.document"})
        if path.endswith("/files/f1/export"):
            return httpx.Response(200, text="exported text")
        if path.endswith("/upload/drive/v3/files"):
            assert b"multipart/related" in request.headers["content-type"].encode()
            assert b"# Draft" in request.content
            return httpx.Response(200, json={"id": "f2", "webViewLink": "https://docs/x"})
        if path.endswith("/files/f1/permissions"):
            return httpx.Response(200, json={"id": "p1"})
        return httpx.Response(404)

    async with _client(handler) as client:
        found = await google.search_files(client, TOKEN, g_specs.SearchFilesIn(query="plan"))
        text = await google.read_file(client, TOKEN, g_specs.ReadFileIn(file_id="f1"))
        doc = await google.create_doc_draft(
            client, TOKEN, g_specs.CreateDocDraftIn(title="Draft", content_md="# Draft")
        )
        share = await google.share_file(
            client,
            TOKEN,
            g_specs.ShareIn(file_id="f1", principal="ana@example.com", role="reader"),
        )

    assert found.files[0].name == "Plan"
    assert text.text == "exported text"
    assert doc.file_id == "f2"
    assert share.permission_id == "p1"


async def test_github_list_repos_and_pull_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-github-api-version"] == "2022-11-28"
        path = request.url.path
        if path == "/user/repos":
            return httpx.Response(
                200,
                json=[
                    {
                        "full_name": "acme/app",
                        "private": True,
                        "default_branch": "main",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "open_issues_count": 3,
                    }
                ],
            )
        if path == "/repos/acme/app/pulls":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 4,
                        "title": "Add cache",
                        "user": {"login": "ana"},
                        "state": "open",
                        "draft": False,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-02T00:00:00Z",
                        "labels": [{"name": "backend"}],
                        "requested_reviewers": [{"login": "bo"}],
                        "head": {"sha": "abc123"},
                    }
                ],
            )
        if path == "/repos/acme/app/commits/abc123/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"name": "tests", "status": "completed", "conclusion": "success"}
                    ]
                },
            )
        return httpx.Response(404)

    async with _client(handler) as client:
        repos = await github.list_repos(client, TOKEN, gh_specs.ListReposIn())
        prs = await github.list_pull_requests(
            client, TOKEN, gh_specs.ListPullRequestsIn(repo="acme/app")
        )

    assert repos.repos[0].full_name == "acme/app"
    assert prs.pull_requests[0].ci_status == "success"
    assert prs.pull_requests[0].labels == ["backend"]


async def test_github_get_pull_request_and_ci_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls/4"):
            return httpx.Response(
                200,
                json={
                    "number": 4,
                    "title": "Add cache",
                    "user": {"login": "ana"},
                    "state": "open",
                    "head": {"sha": "abc"},
                    "additions": 10,
                    "deletions": 2,
                    "changed_files": 3,
                },
            )
        if path.endswith("/pulls/4/comments"):
            return httpx.Response(
                200,
                json=[
                    {"user": {"login": "bo"}, "body": "nit", "created_at": "2026-01-01T00:00:00Z"}
                ],
            )
        if path.endswith("/commits/main/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [{"name": "lint", "status": "completed", "conclusion": "failure"}]
                },
            )
        return httpx.Response(404)

    async with _client(handler) as client:
        detail = await github.get_pull_request(
            client, TOKEN, gh_specs.GetPullRequestIn(repo="acme/app", number=4)
        )
        ci = await github.get_ci_status(
            client, TOKEN, gh_specs.GetCiStatusIn(repo="acme/app", ref="main")
        )

    assert detail.additions == 10
    assert detail.comments[0].body == "nit"
    assert ci.checks[0].conclusion == "failure"


async def test_provider_errors_are_wrapped_without_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "secret-token-value"})

    async with _client(handler) as client:
        with pytest.raises(AppError) as excinfo:
            await google.list_events(
                client,
                TOKEN,
                g_specs.ListEventsIn(
                    time_min=datetime(2026, 1, 1, tzinfo=UTC),
                    time_max=datetime(2026, 1, 2, tzinfo=UTC),
                ),
            )

    assert excinfo.value.status_code == 502
    assert "secret-token-value" not in str(excinfo.value.detail)
