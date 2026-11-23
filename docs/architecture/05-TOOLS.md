# 05 — Tool System

## 1. Registry

`tools/registry.py` holds `ToolSpec` objects registered at import time by `tools/builtin/*`. The registry is **static code** (plus MCP tools loaded from DB after pinning, see §9). The LLM never sees the registry directly; `registry.expose(session, domains)` returns the filtered list of tool _definitions_ for the prompt.

### 1.1 ToolSpec schema

```python
class ToolSpec(BaseModel):
    name: str                         # "gmail.search_threads" (namespace.verb_noun, snake)
    version: str                      # semver; bump on schema change
    description: str                  # model-facing; static, reviewed; ≤ 500 chars
    domains: set[Domain | Literal["CORE"]]
    provider: Literal["native","mcp"]
    integration: Literal["gmail","gcal","gdrive","github",None]
    required_scopes: list[str]
    input_model: type[BaseModel]      # Pydantic → JSON schema for Claude tool definition
    output_model: type[BaseModel]
    side_effect: Literal["read","write_internal","write_reversible","write_external","destructive"]
    risk_tier: Literal[0,1,2,3,4]     # see §3
    approval: ApprovalPolicy          # default by tier; can only be made *stricter* by config
    idempotency: Literal["none_read_only","key_param","natural_key"]
    timeout_s: int
    retry: RetryPolicy                # max_attempts, backoff, retry_on error classes
    rate_limit: RateLimitPolicy       # per_user_per_min, per_provider budget key, burst
    output_trust: Literal["untrusted","trusted_internal"]   # external-sourced → untrusted
    egress: Literal["none","user_data_to_third_party","user_data_to_user_account"]
    max_output_bytes: int
    compensation: str | None          # name of undo tool/handler
    redact_args: list[str]            # JSON paths redacted in logs (e.g. "$.body")
    feature_flag: str | None
    kill_switch_key: str              # "kill.tool.<name>"
    preview_renderer: str | None      # function rendering human-readable approval preview (required if tier ≥2)
```

Registry boot validation (fails startup): unique names; tier ≥2 ⇒ `preview_renderer` set; `write_*`/`destructive` ⇒ idempotency ≠ `none_read_only`; `output_trust='untrusted'` for any integration tool that returns third-party-authored content; tool descriptions contain no URLs or imperative second-person text addressed to other agents.

## 2. Validation

1. **Input:** parse model-proposed args with `input_model.model_validate(strict=True)`; reject on error (error returned to model as `ToolError(kind="validation")`, counts against step budget; max 2 consecutive validation retries then replan).
2. **Semantic validation hooks** per tool (e.g., event end>start, recipient syntactically valid, repo ∈ allowlist, date within ±5 years).
3. **Provenance check:** `ToolRequest.derived_from` — for tier ≥2 tools, any _critical argument_ (declared in spec as `critical_args=["to","cc","bcc","attendees","repo","visibility"]`) whose value appears only in untrusted context and not in user/zara-trusted context is flagged `arg_untrusted=True` → forces `require_approval` with explicit highlighting in preview.
4. **Output:** `output_model.model_validate`; size cap (`max_output_bytes`, truncate with marker + `truncated=true`); strip HTML/scripts/zero-width/control chars; if `output_trust='untrusted'` wrap as `<untrusted_data …>`.
5. **Result sanity:** schema mismatch → `ToolError(kind="integration_schema")`, no retry, alert.

## 3. Permission tiers

| Tier | Side effect                       | Default policy                                                                               | Examples                                                                                              |
| ---- | --------------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| 0    | Read user's own data              | auto-allow                                                                                   | `memory.search`, `gcal.list_events`, `gmail.search_threads`                                           |
| 1    | Internal writes                   | auto-allow + logged                                                                          | `memory.save`, `tasks.create`, `notify.in_app`                                                        |
| 2    | Reversible external writes        | auto-allow only if autonomy ≥ `draft` for that domain **and** run not tainted; else approval | `gmail.create_draft`, `gcal.create_hold`, `drive.create_doc_draft`                                    |
| 3    | External communication/commitment | **approval required** unless matching `standing_rule` and run clean                          | `gmail.send_email`, `gcal.respond_to_invite`, `github.comment`                                        |
| 4    | Destructive / sensitive           | **always approval**, step-up auth if enabled; standing rules not allowed                     | `gmail.trash_thread`, `drive.share`, `github.merge_pr`, `memory.delete_all`, `integration.disconnect` |

**Policy engine (`tools/policy.py`) algorithm** (pure function; every decision recorded):

```
inputs: ToolSpec, args, session(user prefs, autonomy, kill switches, quiet hours), run(taint, usage),
        org_policy, standing_rules, rate-limit state, anomaly state
1. kill switch (global/user/integration/tool) → DENY
2. integration status ≠ active or missing scope → DENY (reason: needs_reauth / scope_missing)
3. tool not in this run's exposed set (allowlist) → DENY
4. budget exceeded (cost/steps/rate) → DENY(budget)
5. tier 4 → REQUIRE_APPROVAL(step_up if enabled)
6. tier 3: if run.tainted or any critical arg untrusted → REQUIRE_APPROVAL (standing rules ignored)
          elif matching standing_rule (conditions satisfied, daily_limit not exceeded) → ALLOW(rule_id)
          else REQUIRE_APPROVAL
7. tier 2: if run.tainted → REQUIRE_APPROVAL; elif autonomy[domain] ≥ draft → ALLOW else REQUIRE_APPROVAL
8. anomaly (bulk: >5 external writes/10 min, new recipient domain, off-hours burst) → escalate one tier
9. tier ≤1 → ALLOW
```

Autonomy levels order: `observe < suggest < draft < act_with_approval < act_autonomous`. Tier-3 `ALLOW` via standing rule requires `act_autonomous` or an explicit rule; tier 4 never.

## 4. User consent

- **Integration consent:** OAuth screen with scope list; per-integration settings (labels/folders/repos). Tools needing broader scope than granted → `scope_missing`; UI offers incremental authorization.
- **Action consent:** `agent.approvals` object (02 §2.6); preview is rendered by the tool's `preview_renderer` from the **exact args** that will execute; approval stores `args_hash`; executor recomputes hash and refuses on mismatch. User may _edit_ (e.g., email body) → new args → new hash → `edited_approved`, re-validated and re-policy-checked.
- **Standing rules:** explicit creation UI/chat confirmation; scoped (tool, recipient domain/contact set, max/day, expiry ≤90 d); revocable; tier ≤3; listed in settings; each use logged.
- **Expiry:** approvals expire (default 24 h; tier 4: 1 h; configurable); expiry → run continues with `rejected_by_expiry` observation, never auto-approves.

## 5. Execution pipeline (`tools/pipeline.py: execute(request, ctx)`)

```
 1 resolve ToolSpec; load integration
 2 validate input (strict) + semantic hooks
 3 compute args_hash, idempotency_key (if absent: sha256(run_id|plan_step|tool|canonical_args))
 4 INSERT tool_executions(status='pending') ON CONFLICT (tenant,tool,key) → if exists:
       succeeded → return stored result (idempotent replay);  running → wait/poll; failed → allow retry attempt+1
 5 policy decision → persist (policy_decision, reasons)
 6 deny → status='denied', return ToolError(kind="policy")
 7 require_approval → create approval, status stays 'pending', return pending_approval (graph enters APPROVAL node; durable wait)
 8 acquire rate-limit token (provider budget); if none → backoff/queue (never drop)
 9 execute with timeout (asyncio.timeout + provider client timeouts) under OTel span
10 on error → classify (§6) → retry per policy or fail
11 validate/sanitize output; wrap trust; store result_summary (+ object-store for large)
12 audit_log entry (action='tool.executed', ids, hashes only), usage metrics
13 return ToolResult
```

Compensation: for `write_reversible` tools with `compensation` set, `agent.run` failure after side-effect triggers optional compensation step (e.g., delete the draft) only if user-visible state would otherwise be inconsistent; compensation is itself a tier-≤2 tool call logged normally.

## 6. Timeouts, retries, rate limits

| Class                                                                                                                                                                                                                                                                               | Timeout | Retry                                                                                                                                                          |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| read, Google/GitHub API                                                                                                                                                                                                                                                             | 15 s    | 3 attempts, exp backoff 1s→2s→4s ±30% jitter, on 429/5xx/network; honor `Retry-After`                                                                          |
| write_reversible                                                                                                                                                                                                                                                                    | 20 s    | 2 attempts only if idempotency guaranteed by key/natural key                                                                                                   |
| write_external (send)                                                                                                                                                                                                                                                               | 30 s    | **0 automatic retries after request may have been sent** (unknown outcome → reconcile by querying provider with idempotency token/Message-ID before any retry) |
| web.fetch/search                                                                                                                                                                                                                                                                    | 15 s    | 2 attempts                                                                                                                                                     |
| memory/tasks (DB)                                                                                                                                                                                                                                                                   | 5 s     | 2 attempts on serialization failure                                                                                                                            |
| Non-retryable: 400/401(after one token refresh)/403/404, validation errors, policy denials. `401` → refresh token once → if still failing mark integration `needs_reauth`, pause dependent workflows.                                                                               |
| Rate limiting: token buckets in Postgres (`SELECT … FOR UPDATE SKIP LOCKED` on `rate_limits` row keyed `provider:account`) in MVP; per-user per-tool caps (e.g., `gmail.*` reads 60/min, `github.*` 5k/h shared per installation). Global LLM-calls-per-user caps in `llm.budgets`. |

## 7. Idempotency

- Read tools: naturally idempotent.
- Writes: **key** = `sha256(tenant|user|run_id|plan_step_id|tool|canonical_json(args))`; stored in `tool_executions` UQ. Provider-level: Gmail drafts → store `external_ref` and pre-check by custom header `X-ZARA-Idem: <key>` search; Calendar events → use client-provided `id` derived from key (base32hex) so duplicate inserts 409 cleanly; GitHub comments → search recent comments for hidden marker `<!-- zara:<key> -->` before posting.
- Approval re-delivery or workflow replay never causes a second execution: executor checks `tool_executions.status` first.

## 8. Tool catalogue (MVP v1 names, tier, trust)

Input/output Pydantic models live in `tools/builtin/<ns>.py`; the JSON below is illustrative of the key fields.

### 8.1 Memory & knowledge (CORE)

| Tool              | Tier | Input (key fields)                                                                                                                   | Output                                           | Trust                                                             |
| ----------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------ | ----------------------------------------------------------------- |
| `memory.search`   | 0    | `query, kinds?, entity_ids?, time_window?, k≤20`                                                                                     | `memories[]` (id, kind, content, score, sources) | trusted_internal (content from external wrapped in item metadata) |
| `memory.save`     | 1    | `kind, content, payload?, entities?, evidence[]` (goes through extraction gates; `trust` forced by session: user-turn only → `user`) | `memory_id, status`                              | internal                                                          |
| `memory.update`   | 1    | `memory_id, content?, payload?`                                                                                                      | revision                                         | internal (user-edited protection enforced)                        |
| `memory.forget`   | 4    | `memory_id                                                                                                                           | entity_id                                        | source_id, scope`                                                 | `deleted_count` | internal |
| `entities.lookup` | 0    | `query, types?`                                                                                                                      | `entities[]` cards                               | internal                                                          |
| `entities.link`   | 1    | `entity_a, entity_b, relation`                                                                                                       | edge id                                          | internal                                                          |

### 8.2 Tasks & reminders (CORE)

| `tasks.list` | 0 | filters (status, due range, project, goal) | tasks[] |
| `tasks.create` | 1 | `title, due_at?, remind_at?, priority?, project_id?, goal_id?, contact_id?` | task |
| `tasks.update` | 1 | `task_id, fields…` | task |
| `tasks.complete` | 1 | `task_id` | task |
| `goals.list/update_progress` | 0/1 | | |

### 8.3 Notifications (CORE)

| `notify.in_app` | 1 | `title, body, priority, action_url?` | notification id (subject to notification policy/quiet hours — tool _requests_, policy decides) |
| `notify.email_self` | 1 | same + `digest?` | sent to the user's own verified address only (hard-coded recipient) |
| `notify.schedule` | 1 | `when, title, body` | creates scheduled notification via Temporal timer |

### 8.3b Gmail

| `gmail.search_threads` | 0 | `query` (Gmail syntax), `max≤25`, `newer_than?` | `threads[]` (id, subject, participants, snippet, last_msg_at) | **untrusted** |
| `gmail.read_thread` | 0 | `thread_id, max_messages≤20, include_quoted=false` | messages[] (headers, text body ≤8k chars each, attachment metadata) | **untrusted**; attachments never auto-opened |
| `gmail.create_draft` | 2 | `to[], cc[], subject, body_text, reply_to_thread_id?, in_reply_to?` | `draft_id, gmail_url` | critical_args: to, cc |
| `gmail.update_draft` | 2 | `draft_id, fields` | | |
| `gmail.send_draft` / `gmail.send_email` | 3 (feature-flag off in MVP, D-17) | `draft_id` or full message | `message_id` | critical_args: to, cc, bcc, attachments |
| `gmail.apply_label` | 1→2 | `thread_id, label` | | user labels only |
| `gmail.trash_thread` | 4 | `thread_id` | | |

### 8.4 Google Calendar

| `gcal.list_events` | 0 | `time_min,time_max,calendar_ids?,query?` | events[] (id,title,start,end,attendees,location,conference,organizer,responseStatus) | **untrusted** (titles/descriptions third-party) |
| `gcal.find_free_slots` | 0 | `duration_min, window, attendees?, constraints (working hours from profile)` | slots[] | internal |
| `gcal.detect_conflicts` | 0 | `time_min,time_max` | conflicts[] (pairs, overlap_min, kind) | internal |
| `gcal.create_hold` | 2 | `title,start,end,description?` (no attendees → no invites sent) | event id | |
| `gcal.create_event` | 3 | `…, attendees[], send_updates` | event id | critical: attendees |
| `gcal.update_event` | 3 | | | |
| `gcal.respond_to_invite` | 3 | `event_id, response` | | |
| `gcal.delete_event` | 4 | | | |

### 8.5 Google Drive

| `drive.search_files` | 0 | `query, folder_ids?, mime_types?` | files[] metadata | untrusted (names) |
| `drive.read_file` | 0 | `file_id, max_chars≤20k` | text (Docs/Sheets→text export; PDFs via extraction) | **untrusted** |
| `drive.create_doc_draft` | 2 | `title, content_md, folder_id?` | file id, url (private to user) | |
| `drive.share` | 4 | `file_id, principal, role` | | |
Scope: `drive.file` + user-picked folders via Picker; `drive.readonly` is **not** requested in MVP.

### 8.6 GitHub (read-only in MVP, D-18)

| `github.list_repos` | 0 | | monitored repos |
| `github.list_pull_requests` | 0 | `repo, state, author?, updated_since?, review_requested?` | prs[] (number,title,author,state,draft,ci_status,age_days,reviewers,labels) | **untrusted** |
| `github.get_pull_request` | 0 | `repo, number, include_diff_stat=true, include_comments=true(≤30)` | pr detail (diff _stat_ only; raw diff via `github.get_diff` capped 30k chars) | **untrusted** |
| `github.list_issues` | 0 | | | untrusted |
| `github.get_ci_status` | 0 | `repo, ref` | checks[] | untrusted (names) |
| `github.list_releases` / `github.get_milestones` | 0 | | | |
| `github.comment` | 3 (post-MVP) | `repo, number, body` | | |
| `github.merge_pr` | 4 (post-MVP) | | | |

### 8.7 Web research

| `web.search` | 0 | `query, max_results≤8, recency?` | results[] (title,url,snippet,published) | **untrusted** |
| `web.fetch` | 0 | `url` | `text (readability-extracted ≤15k chars), final_url, status` | **untrusted**; **SSRF-safe** (§10 of 09) |
Both run through the egress proxy; domains denylisted; no cookies/credentials; no following redirects to private IP ranges; robots respected; content-type allowlist (`text/html, text/plain, application/pdf, application/json`).

### 8.8 CRM (native tables, MVP)

| `crm.find_contact` | 0 | `query | email` | contact + interactions summary |
| `crm.upsert_contact` | 1 | fields (emails normalized) | contact |
| `crm.list_leads` / `crm.list_opportunities` | 0 | filters (stage, silent_days≥, close_before) | rows |
| `crm.update_lead_stage` / `crm.update_opportunity` | 1 | | row (internal state only; no external CRM in MVP) |
| `crm.log_interaction` | 1 | | |

## 9. MCP tools (post-MVP, designed now)

- Table `integrations.mcp_servers(id, tenant_id, user_id, name, transport, url, auth_ref, status, allowed_tools text[], tool_manifest_hash bytea, approved_by, approved_at)` (added by migration 0014, not in MVP).
- Install flow: fetch manifest → show tools + descriptions to user → user selects allowlist → store hash. Descriptions are **sanitized and length-capped**; any hash change → `status='needs_review'`.
- Each MCP tool is wrapped in a generated `ToolSpec` with `provider="mcp"`, tier ≥2 by default (unknown side effects), `output_trust="untrusted"`, same pipeline/policy.

## 10. Execution logging

- `agent.tool_executions` row per call (args redacted per `redact_args`; `args_hash` of full args), `audit.audit_log` entry, OTel span `tool.<name>` with attributes `tool.tier, tool.policy, tool.attempt, run.id, tenant.id(hash)`, SSE event `tool.started|tool.finished|tool.denied|approval.requested`.
- Logs never contain tokens or message bodies; `result_summary` is a model-safe digest (counts, ids, short subject lines ≤ 80 chars).

## 11. Acceptance criteria — Tools

1. Registry boot validation tests (each rule in §1.1) pass; adding a tier-3 tool without preview renderer fails CI.
2. 100% of tool calls produce `tool_executions` + `audit_log` rows (checked by integration test counting).
3. Duplicate execution test: replaying the same request 20× concurrently produces exactly one provider-side effect (fake provider _in tests only_ counting calls).
4. Approval hash mismatch → refusal (test).
5. Tainted-run test corpus (≥60 injection emails): **0** tier ≥2 executions without approval.
6. Unknown-outcome send (simulated timeout after provider accepted) reconciles via provider lookup and does not double send.
7. Rate-limit test: 429 + `Retry-After` honored; no drops; bounded queue wait.
8. SSRF suite (09 §8) passes for `web.fetch`.
