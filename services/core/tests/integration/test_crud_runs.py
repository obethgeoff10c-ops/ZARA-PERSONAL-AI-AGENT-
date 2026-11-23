"""End-to-end agent-run lifecycle and event replay."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("run"), org_id=unique_org())


def _create_run(api: Api, token: str, goal: str = "Do the thing") -> str:
    response = api.post("/runs", token=token, json={"goal": goal})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_run_create_get_list_and_cancel(api: Api) -> None:
    token = _token(api)
    run_id = _create_run(api, token)

    fetched = api.get(f"/runs/{run_id}", token=token)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "queued"
    assert body["goal"] == "Do the thing"
    assert body["trigger_type"] == "manual"

    listed = api.get("/runs", token=token)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [run_id]

    tools = api.get(f"/runs/{run_id}/tools", token=token)
    assert tools.status_code == 200
    assert tools.json()["items"] == []

    cancelled = api.post(f"/runs/{run_id}/cancel", token=token, json={"reason": "changed my mind"})
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"

    after = api.get(f"/runs/{run_id}", token=token).json()
    assert after["status"] == "cancelled"
    assert after["finished_at"] is not None


def test_cancelled_run_cannot_be_cancelled_twice(api: Api) -> None:
    token = _token(api)
    run_id = _create_run(api, token)
    assert api.post(f"/runs/{run_id}/cancel", token=token, json={}).status_code == 202

    again = api.post(f"/runs/{run_id}/cancel", token=token, json={})

    assert again.status_code == 409
    assert problem_body(again)["code"] == "conflict"


def test_run_create_validates_domains(api: Api) -> None:
    token = _token(api)

    response = api.post("/runs", token=token, json={"goal": "x", "domains": ["not-a-domain"]})

    assert response.status_code == 422
    body = problem_body(response)
    assert body["errors"][0]["field"] == "domains"


def test_run_create_with_unknown_conversation_is_rejected(api: Api) -> None:
    token = _token(api)

    response = api.post(
        "/runs", token=token, json={"goal": "x", "conversation_id": str(uuid.uuid4())}
    )

    assert response.status_code == 422
    assert problem_body(response)["errors"][0]["field"] == "conversation_id"


def _read_sse(client: TestClient, path: str, token: str, headers: dict[str, str]) -> list[str]:
    lines: list[str] = []
    with client.stream(
        "GET", path, headers={"Authorization": f"Bearer {token}", **headers}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for index, line in enumerate(response.iter_lines()):
            if index > 50:  # pragma: no cover - defensive cap against a hung stream
                break
            if line:
                lines.append(line)
    return lines


def test_run_event_stream_replays_terminal_run(api: Api, client: TestClient) -> None:
    token = _token(api)
    run_id = _create_run(api, token)
    assert api.post(f"/runs/{run_id}/cancel", token=token, json={}).status_code == 202

    lines = _read_sse(client, f"/api/v1/runs/{run_id}/events", token, {})

    assert lines[0] == "retry: 3000"
    payloads = [
        json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")
    ]
    statuses = [payload.get("status") for payload in payloads]
    assert statuses[:2] == ["queued", "cancelled"]
    assert any(line.startswith("event: done") for line in lines)


def test_run_event_stream_resumes_after_last_event_id(api: Api, client: TestClient) -> None:
    token = _token(api)
    run_id = _create_run(api, token)
    assert api.post(f"/runs/{run_id}/cancel", token=token, json={}).status_code == 202

    lines = _read_sse(client, f"/api/v1/runs/{run_id}/events", token, {"Last-Event-ID": "1"})

    payloads = [
        json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")
    ]
    # Only the event after sequence 1 is replayed, followed by the terminal done event.
    assert [payload.get("status") for payload in payloads] == ["cancelled", "cancelled"]
    assert not any(payload.get("status") == "queued" for payload in payloads)
    assert any(line.startswith("event: done") for line in lines)


def test_run_event_stream_requires_auth(client: TestClient) -> None:
    response = client.get(f"/api/v1/runs/{uuid.uuid4()}/events")

    assert response.status_code == 401
    assert problem_body(response)["code"] == "unauthenticated"


def test_run_event_stream_rejects_non_numeric_last_event_id(api: Api, client: TestClient) -> None:
    token = _token(api)
    run_id = _create_run(api, token)

    response = client.get(
        f"/api/v1/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "abc"},
    )

    assert response.status_code == 422
    assert problem_body(response)["errors"][0]["field"] == "Last-Event-ID"
