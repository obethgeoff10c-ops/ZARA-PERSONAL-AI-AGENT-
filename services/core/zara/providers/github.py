"""Real GitHub REST API client (read-only tools)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from zara.errors import upstream_error
from zara.tools.builtin.github import (
    CheckOut,
    CommentOut,
    GetCiStatusIn,
    GetCiStatusOut,
    GetPullRequestIn,
    IssueOut,
    ListIssuesIn,
    ListIssuesOut,
    ListPullRequestsIn,
    ListPullRequestsOut,
    ListReposIn,
    ListReposOut,
    PullRequestDetailOut,
    PullRequestOut,
    RepoOut,
)

_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_VERSION = "2022-11-28"


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _VERSION,
    }


def _raise(response: httpx.Response, operation: str) -> None:
    if response.status_code < 400:
        return
    raise upstream_error(f"GitHub {operation} failed (status {response.status_code}).")


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pr(item: dict[str, Any], repo: str, *, ci_status: str | None = None) -> PullRequestOut:
    head = item.get("head") or {}
    labels = [str(label.get("name")) for label in item.get("labels") or []]
    reviewers = [
        str(person.get("login"))
        for person in item.get("requested_reviewers") or []
        if person.get("login")
    ]
    return PullRequestOut(
        repo=repo,
        number=int(item.get("number", 0)),
        title=str(item.get("title", "")),
        author=(item.get("user") or {}).get("login"),
        state=str(item.get("state", "")),
        draft=bool(item.get("draft")),
        created_at=_dt(item.get("created_at")),
        updated_at=_dt(item.get("updated_at")),
        labels=labels,
        reviewers=reviewers,
        ci_status=ci_status or head.get("ci_status"),
    )


async def _ci_status(
    client: httpx.AsyncClient, access_token: str, repo: str, sha: str
) -> str | None:
    if not sha:
        return None
    response = await client.get(
        f"{_API}/repos/{repo}/commits/{sha}/check-runs",
        headers=_headers(access_token),
    )
    if response.status_code >= 400:
        return None
    runs = (response.json() or {}).get("check_runs") or []
    if not runs:
        return "none"
    if any(run.get("status") != "completed" for run in runs):
        return "pending"
    if any(run.get("conclusion") in {"failure", "timed_out", "cancelled"} for run in runs):
        return "failure"
    return "success"


async def list_repos(
    client: httpx.AsyncClient, access_token: str, params: ListReposIn
) -> ListReposOut:
    response = await client.get(
        f"{_API}/user/repos",
        headers=_headers(access_token),
        params={"per_page": params.max_results, "sort": "updated"},
    )
    _raise(response, "repo list")
    items: list[dict[str, Any]] = response.json()
    repos = [
        RepoOut(
            full_name=str(item.get("full_name", "")),
            private=bool(item.get("private")),
            default_branch=str(item.get("default_branch", "")),
            updated_at=_dt(item.get("updated_at")),
            open_issues=int(item.get("open_issues_count", 0)),
        )
        for item in items
        if params.owner is None or str(item.get("full_name", "")).startswith(f"{params.owner}/")
    ]
    return ListReposOut(repos=repos)


async def list_pull_requests(
    client: httpx.AsyncClient, access_token: str, params: ListPullRequestsIn
) -> ListPullRequestsOut:
    response = await client.get(
        f"{_API}/repos/{params.repo}/pulls",
        headers=_headers(access_token),
        params={"state": params.state, "per_page": params.max_results, "sort": "updated"},
    )
    _raise(response, "pull request list")
    items: list[dict[str, Any]] = response.json()
    selected = [
        item
        for item in items
        if (params.author is None or (item.get("user") or {}).get("login") == params.author)
        and (not params.review_requested or bool(item.get("requested_reviewers")))
        and (
            params.updated_since is None
            or (_dt(item.get("updated_at")) or datetime.min) >= params.updated_since
        )
    ]
    pull_requests = []
    for item in selected:
        ci_status = None
        if len(selected) <= 10:
            ci_status = await _ci_status(
                client, access_token, params.repo, str((item.get("head") or {}).get("sha", ""))
            )
        pull_requests.append(_pr(item, params.repo, ci_status=ci_status))
    return ListPullRequestsOut(pull_requests=pull_requests)


async def get_pull_request(
    client: httpx.AsyncClient, access_token: str, params: GetPullRequestIn
) -> PullRequestDetailOut:
    response = await client.get(
        f"{_API}/repos/{params.repo}/pulls/{params.number}",
        headers=_headers(access_token),
    )
    _raise(response, "pull request read")
    item: dict[str, Any] = response.json()
    base = _pr(item, params.repo)
    comments: list[CommentOut] = []
    if params.include_comments and params.max_comments:
        reviews = await client.get(
            f"{_API}/repos/{params.repo}/pulls/{params.number}/comments",
            headers=_headers(access_token),
            params={"per_page": params.max_comments},
        )
        if reviews.status_code < 400:
            comments = [
                CommentOut(
                    author=(comment.get("user") or {}).get("login"),
                    body=str(comment.get("body", "")),
                    created_at=_dt(comment.get("created_at")),
                )
                for comment in (reviews.json() or [])
            ]
    return PullRequestDetailOut(
        **base.model_dump(),
        body=item.get("body"),
        additions=int(item.get("additions", 0)) if params.include_diff_stat else None,
        deletions=int(item.get("deletions", 0)) if params.include_diff_stat else None,
        changed_files=int(item.get("changed_files", 0)) if params.include_diff_stat else None,
        comments=comments,
    )


async def list_issues(
    client: httpx.AsyncClient, access_token: str, params: ListIssuesIn
) -> ListIssuesOut:
    query: dict[str, Any] = {
        "state": params.state,
        "per_page": params.max_results,
        "sort": "updated",
    }
    if params.labels:
        query["labels"] = ",".join(params.labels)
    response = await client.get(
        f"{_API}/repos/{params.repo}/issues", headers=_headers(access_token), params=query
    )
    _raise(response, "issue list")
    items = [item for item in response.json() if "pull_request" not in item]
    issues = [
        IssueOut(
            repo=params.repo,
            number=int(item.get("number", 0)),
            title=str(item.get("title", "")),
            author=(item.get("user") or {}).get("login"),
            state=str(item.get("state", "")),
            labels=[str(label.get("name")) for label in item.get("labels") or []],
            updated_at=_dt(item.get("updated_at")),
        )
        for item in items
    ]
    return ListIssuesOut(issues=issues)


async def get_ci_status(
    client: httpx.AsyncClient, access_token: str, params: GetCiStatusIn
) -> GetCiStatusOut:
    response = await client.get(
        f"{_API}/repos/{params.repo}/commits/{params.ref}/check-runs",
        headers=_headers(access_token),
    )
    _raise(response, "CI status")
    runs = (response.json() or {}).get("check_runs") or []
    checks = [
        CheckOut(
            name=str(run.get("name", "")),
            status=str(run.get("status", "")),
            conclusion=run.get("conclusion"),
        )
        for run in runs
    ]
    return GetCiStatusOut(ref=params.ref, checks=checks)
