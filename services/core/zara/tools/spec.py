"""Tool specification and boot validation (05-TOOLS.md §1.1)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_IMPERATIVE_RE = re.compile(r"\byou (?:must|should|need to|will)\b", re.IGNORECASE)

SideEffect = Literal["read", "write_internal", "write_reversible", "write_external", "destructive"]
Idempotency = Literal["none_read_only", "key_param", "natural_key"]
OutputTrust = Literal["untrusted", "trusted_internal"]
IntegrationName = Literal["gmail", "gcal", "gdrive", "github"]


class ApprovalPolicy(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval_required"
    ALWAYS = "always_approval"


class ToolSpec(BaseModel):
    """Static, reviewed description of one callable tool."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    name: str
    version: str = "1.0.0"
    description: str = Field(max_length=500)
    domains: frozenset[str]
    provider: Literal["native", "mcp"] = "native"
    integration: IntegrationName | None = None
    required_scopes: tuple[str, ...] = ()
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effect: SideEffect
    risk_tier: int = Field(ge=0, le=4)
    approval: ApprovalPolicy
    idempotency: Idempotency
    timeout_s: int = Field(ge=1, le=120)
    output_trust: OutputTrust
    egress: Literal["none", "user_data_to_third_party", "user_data_to_user_account"] = "none"
    max_output_bytes: int = Field(ge=256, le=2_000_000)
    compensation: str | None = None
    redact_args: tuple[str, ...] = ()
    feature_flag: str | None = None
    kill_switch_key: str
    preview_renderer: str | None = None
    critical_args: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> ToolSpec:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}; expected namespace.verb_noun")
        if _URL_RE.search(self.description):
            raise ValueError("tool descriptions must not contain URLs")
        if _IMPERATIVE_RE.search(self.description):
            raise ValueError("tool descriptions must not address another agent imperatively")
        if not self.domains:
            raise ValueError("at least one domain is required")
        if self.integration is not None and not self.required_scopes:
            raise ValueError("integration tools must declare required_scopes")
        if self.integration is None and self.required_scopes:
            raise ValueError("native tools must not declare provider scopes")
        if self.risk_tier >= 2 and not self.preview_renderer:
            raise ValueError("tier >= 2 tools require a preview_renderer")
        if (
            self.side_effect
            in {"write_internal", "write_reversible", "write_external", "destructive"}
            and self.idempotency == "none_read_only"
        ):
            raise ValueError("write tools require key_param or natural_key idempotency")
        if self.side_effect == "read" and self.idempotency != "none_read_only":
            raise ValueError("read tools must use none_read_only idempotency")
        if (
            self.integration is not None
            and self.side_effect == "read"
            and self.output_trust != "untrusted"
        ):
            raise ValueError("integration reads return third-party content and must be untrusted")
        if self.risk_tier == 4 and self.approval != ApprovalPolicy.ALWAYS:
            raise ValueError("tier 4 tools must always require approval")
        if self.risk_tier == 3 and self.approval != ApprovalPolicy.APPROVAL:
            raise ValueError("tier 3 tools must require approval by default")
        return self
