"""Pure approval-policy engine (05-TOOLS.md §3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from zara.tools.spec import ApprovalPolicy, ToolSpec

Decision = Literal["allow", "deny", "require_approval"]

_AUTONOMY_ORDER = {
    "observe": 0,
    "suggest": 1,
    "draft": 2,
    "act_with_approval": 3,
    "act_autonomous": 4,
}


@dataclass(frozen=True, slots=True)
class PolicyContext:
    integration_status: str = "active"
    granted_scopes: frozenset[str] = frozenset()
    tool_allowed: bool = True
    budget_exceeded: bool = False
    run_tainted: bool = False
    critical_arg_untrusted: bool = False
    autonomy: str = "suggest"
    standing_rule: str | None = None
    anomaly: bool = False
    kill_switches: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason: str
    rule_id: str | None = None


def _autonomy_at_least(level: str, minimum: str) -> bool:
    return _AUTONOMY_ORDER.get(level, 0) >= _AUTONOMY_ORDER[minimum]


def decide(spec: ToolSpec, ctx: PolicyContext) -> PolicyDecision:
    for key in ("global", spec.kill_switch_key):
        if key in ctx.kill_switches:
            return PolicyDecision("deny", f"kill_switch:{key}")
    if spec.integration is not None:
        if ctx.integration_status != "active":
            return PolicyDecision("deny", "needs_reauth")
        missing = set(spec.required_scopes) - set(ctx.granted_scopes)
        if missing:
            return PolicyDecision("deny", "scope_missing")
    if not ctx.tool_allowed:
        return PolicyDecision("deny", "tool_not_exposed")
    if ctx.budget_exceeded:
        return PolicyDecision("deny", "budget_exceeded")

    if spec.risk_tier == 4:
        return PolicyDecision("require_approval", "tier_4_always")

    if spec.risk_tier == 3:
        if ctx.run_tainted or ctx.critical_arg_untrusted:
            return PolicyDecision("require_approval", "tainted_or_untrusted_args")
        if ctx.standing_rule and _autonomy_at_least(ctx.autonomy, "act_autonomous"):
            return PolicyDecision("allow", "standing_rule", rule_id=ctx.standing_rule)
        return PolicyDecision("require_approval", "tier_3_default")

    if spec.risk_tier == 2:
        if ctx.anomaly:
            return PolicyDecision("require_approval", "anomaly_escalation")
        if ctx.run_tainted:
            return PolicyDecision("require_approval", "run_tainted")
        if _autonomy_at_least(ctx.autonomy, "draft"):
            return PolicyDecision("allow", "autonomy_draft")
        return PolicyDecision("require_approval", "autonomy_below_draft")

    return PolicyDecision("allow", "tier_0_1_auto")


def default_approval(tier: int) -> ApprovalPolicy:
    if tier >= 4:
        return ApprovalPolicy.ALWAYS
    if tier == 3:
        return ApprovalPolicy.APPROVAL
    return ApprovalPolicy.AUTO
