"""End-to-end task CRUD, completion, and optimistic concurrency."""

from __future__ import annotations

import pytest

from helpers import Api, problem_body, unique_email, unique_org

pytestmark = pytest.mark.integration


def _token(api: Api) -> str:
    return api.dev_token(unique_email("task"), org_id=unique_org())


def test_task_lifecycle(api: Api) -> None:
    token = _token(api)

    created = api.post("/tasks", token=token, json={"title": "Ship tests", "priority": 4})
    assert created.status_code == 201
    task = created.json()
    task_id = task["id"]
    assert task["status"] == "todo"
    assert task["completed_at"] is None
    version = task["version"]

    fetched = api.get(f"/tasks/{task_id}", token=token)
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Ship tests"

    patched = api.patch(
        f"/tasks/{task_id}",
        token=token,
        json={"status": "in_progress"},
        headers={"If-Match": str(version)},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "in_progress"
    assert patched.json()["completed_at"] is None

    completed = api.post(f"/tasks/{task_id}/complete", token=token, json={})
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    assert completed.json()["completed_at"] is not None

    deleted = api.delete(f"/tasks/{task_id}", token=token)
    assert deleted.status_code == 204
    assert api.get(f"/tasks/{task_id}", token=token).status_code == 404


def test_list_tasks_filters_by_status(api: Api) -> None:
    token = _token(api)
    todo_id = api.post("/tasks", token=token, json={"title": "Todo item"}).json()["id"]
    done = api.post("/tasks", token=token, json={"title": "Done item"}).json()
    api.post(f"/tasks/{done['id']}/complete", token=token, json={})

    response = api.get("/tasks", token=token, params={"status": "todo"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [todo_id]


def test_complete_twice_is_conflict(api: Api) -> None:
    token = _token(api)
    task_id = api.post("/tasks", token=token, json={"title": "Once"}).json()["id"]
    assert api.post(f"/tasks/{task_id}/complete", token=token, json={}).status_code == 200

    second = api.post(f"/tasks/{task_id}/complete", token=token, json={})
    assert second.status_code == 409
    assert problem_body(second)["code"] == "conflict"


def test_stale_if_match_is_precondition_failed(api: Api) -> None:
    token = _token(api)
    task_id = api.post("/tasks", token=token, json={"title": "Versioned"}).json()["id"]

    stale = api.patch(
        f"/tasks/{task_id}",
        token=token,
        json={"title": "New"},
        headers={"If-Match": "999"},
    )

    assert stale.status_code == 412
    assert problem_body(stale)["code"] == "precondition_failed"


def test_if_match_must_be_an_integer(api: Api) -> None:
    token = _token(api)
    task_id = api.post("/tasks", token=token, json={"title": "Bad header"}).json()["id"]

    response = api.patch(
        f"/tasks/{task_id}",
        token=token,
        json={"title": "New"},
        headers={"If-Match": "not-a-number"},
    )

    assert response.status_code == 422
    body = problem_body(response)
    assert body["code"] == "validation_error"
    assert body["errors"][0]["field"] == "If-Match"
