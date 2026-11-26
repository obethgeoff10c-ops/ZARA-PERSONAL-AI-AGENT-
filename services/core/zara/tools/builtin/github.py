"""GitHub tool definitions (read-only in MVP, D-18)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from zara.tools.spec import ApprovalPolicy, ToolSpec

_GITHUB_SCOPE = "repo:read"


class ListReposIn(BaseModel):
    owner: str | None = None
    max_results: int = Field(default=50, ge=1, le=100)


class RepoOut(BaseModel):
    full_name: str
    private: bool
    default_branch: str
    updated_at: datetime | None
    open_issues: int


class ListReposOut(BaseModel):
    repos: list[RepoOut]


class ListPullRequestsIn(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    state: Literal["open", "closed", "all"] = "open"
    author: str | None = None
    updated_since: datetime | None = None
    review_requested: bool = False
    max_results: int = Field(default=25, ge=1, le=50)


class PullRequestOut(BaseModel):
    repo: str
    number: int
    title: str
    author: str | None
    state: str
    draft: bool
    created_at: datetime | None
    updated_at: datetime | None
    labels: list[str]
    reviewers: list[str]
    ci_status: str | None


class ListPullRequestsOut(BaseModel):
    pull_requests: list[PullRequestOut]


class GetPullRequestIn(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    number: int = Field(ge=1)
    include_diff_stat: bool = True
    include_comments: bool = True
    max_comments: int = Field(default=30, ge=0, le=50)


class CommentOut(BaseModel):
    author: str | None
    body: str
    created_at: datetime | None


class PullRequestDetailOut(PullRequestOut):
    body: str | None = None
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    comments: list[CommentOut] = Field(default_factory=list)


class ListIssuesIn(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    state: Literal["open", "closed", "all"] = "open"
    labels: list[str] = Field(default_factory=list)
    max_results: int = Field(default=25, ge=1, le=50)


class IssueOut(BaseModel):
    repo: str
    number: int
    title: str
    author: str | None
    state: str
    labels: list[str]
    updated_at: datetime | None


class ListIssuesOut(BaseModel):
    issues: list[IssueOut]


class GetCiStatusIn(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    ref: str = Field(min_length=1, max_length=200)


class CheckOut(BaseModel):
    name: str
    status: str
    conclusion: str | None


class GetCiStatusOut(BaseModel):
    ref: str
    checks: list[CheckOut]


def _spec(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        domains=frozenset({"github"}),
        integration="github",
        required_scopes=(_GITHUB_SCOPE,),
        input_model=input_model,
        output_model=output_model,
        side_effect="read",
        risk_tier=0,
        approval=ApprovalPolicy.AUTO,
        idempotency="none_read_only",
        timeout_s=15,
        output_trust="untrusted",
        max_output_bytes=128_000,
        kill_switch_key=f"kill.tool.{name}",
    )


GITHUB_SPECS = (
    _spec(
        name="github.list_repos",
        description="List repositories the connected GitHub installation can access.",
        input_model=ListReposIn,
        output_model=ListReposOut,
    ),
    _spec(
        name="github.list_pull_requests",
        description="List pull requests for one repository with review and CI summary.",
        input_model=ListPullRequestsIn,
        output_model=ListPullRequestsOut,
    ),
    _spec(
        name="github.get_pull_request",
        description="Read one pull request with diff statistics and recent comments.",
        input_model=GetPullRequestIn,
        output_model=PullRequestDetailOut,
    ),
    _spec(
        name="github.list_issues",
        description="List issues for one repository, excluding pull requests.",
        input_model=ListIssuesIn,
        output_model=ListIssuesOut,
    ),
    _spec(
        name="github.get_ci_status",
        description="Read check runs for a branch or commit reference.",
        input_model=GetCiStatusIn,
        output_model=GetCiStatusOut,
    ),
)
