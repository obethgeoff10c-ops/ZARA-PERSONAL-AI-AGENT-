# 07 — API Specification

Base URL: `/api/v1`. JSON over HTTPS. OpenAPI 3.1 generated from FastAPI; Pydantic schemas are the contract. Frontend client generated into `packages/api-client`.

## 0. Cross-cutting

**Authentication:** `Authorization: Bearer <IdP access JWT>` (verified against JWKS; `aud`, `iss`, `exp`, `nbf` checked) _or_ BFF session cookie translated by Next.js server into a short-lived (5 min) internal JWT. Every request resolves `Principal{tenant_id,user_id,role,session_id}` and opens an RLS session. Auth levels used below: **P** = public, **U** = authenticated user, **U+** = authenticated + step-up (recent MFA ≤10 min), **S** = service/internal (HMAC or mTLS), **W** = webhook (signature verified).

**Common headers:** `X-Request-Id` (echoed), `Idempotency-Key` (required on non-idempotent POSTs marked ⟲), `If-Match` (optimistic concurrency for PATCH using `version`/ETag where noted), `Last-Event-ID` (SSE).

**Pagination:** cursor-based: `?limit=50&cursor=<opaque>`; response `{ "items": [...], "next_cursor": "…"|null }`.

**Timestamps** RFC 3339 UTC. **IDs** UUID strings.

**Error format (RFC 9457 problem+json):**

```json
{
  "type": "https://zara.app/errors/validation",
  "title": "Validation failed",
  "status": 422,
  "detail": "…",
  "code": "validation_error",
  "request_id": "…",
  "errors": [{ "field": "due_at", "message": "…" }]
}
```

Error codes: `unauthenticated(401)`, `forbidden(403)`, `step_up_required(403)`, `not_found(404)`, `conflict(409)`, `idempotency_conflict(409)`, `precondition_failed(412)`, `validation_error(422)`, `rate_limited(429, Retry-After)`, `budget_exceeded(402/429)`, `integration_needs_reauth(409)`, `kill_switch_active(503)`, `internal_error(500)`, `upstream_error(502)`.

**Rate limits (per user unless noted):** global 600 req/min; chat/message 20/min; memory writes 60/min; integration connect 10/h; auth endpoints 30/min per IP. Headers `RateLimit-Limit/Remaining/Reset`.

## 1. Authentication & session

| Method Path                           | Auth         | Request                                                                             | Response                                                                                                         |
| ------------------------------------- | ------------ | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `GET /auth/config`                    | P            | —                                                                                   | `{provider, issuer, client_id, jwks_uri}`                                                                        |
| `POST /auth/session`                  | U(IdP token) | `{device_label?}`                                                                   | `201 {session_id, expires_at, user: UserOut}` (creates `auth_sessions` row, returns opaque token for BFF cookie) |
| `DELETE /auth/session`                | U            | —                                                                                   | `204` (revoke current)                                                                                           |
| `GET /auth/sessions`                  | U            | —                                                                                   | `{items:[{id, device_label, ip, last_used_at, created_at, current}]}`                                            |
| `DELETE /auth/sessions/{id}`          | U            | —                                                                                   | `204`                                                                                                            |
| `GET /me`                             | U            | —                                                                                   | `UserOut {id,email,display_name,tenant_id,role,profile:ProfileOut,onboarding_state}`                             |
| `PATCH /me/profile`                   | U            | `{display_name?,timezone?,locale?,working_hours?,quiet_hours?,job_title?,company?}` | `ProfileOut`                                                                                                     |
| `GET/PUT /me/preferences/{namespace}` | U            | `{values:{k:v}}`                                                                    | `{namespace, values}`                                                                                            |
| `POST /me/export` ⟲                   | U+           | `{}`                                                                                | `202 {workflow_id, status_url}`                                                                                  |
| `DELETE /me` ⟲                        | U+           | `{confirm:"DELETE", reason?}`                                                       | `202 {workflow_id}` (DataDeletionWorkflow)                                                                       |

## 2. Chat & conversations

**Conversations**
| `GET /conversations` | U | `?archived=false&limit&cursor` | `{items:[ConversationOut{id,title,last_message_at,domains[],unread_runs}], next_cursor}` |
| `POST /conversations` | U | `{title?}` | `201 ConversationOut` |
| `GET /conversations/{id}` | U | — | `ConversationOut` |
| `PATCH /conversations/{id}` | U | `{title?, archived?}` | `ConversationOut` |
| `DELETE /conversations/{id}` | U | — | `204` (hard delete messages; memory derived stays unless "also forget" flag `?forget_memories=true`) |
| `GET /conversations/{id}/messages` | U | `?limit&cursor(before)` | `{items:[MessageOut], next_cursor}` |

`MessageOut { id, role, content, content_blocks[], citations[{type:"memory|document|entity", id, label}], run_id?, created_at, feedback? }`

**Chat (send a message and stream the response)**
`POST /chat` ⟲ — U — Request:

```json
{
  "conversation_id": "uuid|null",
  "message": "Prep me for my 11am with Priya",
  "attachments": [],
  "client_message_id": "uuid",
  "mode": "auto|quick|deep"
}
```

Response: `200 text/event-stream` (SSE). `Idempotency-Key = client_message_id`; reconnect with `GET /runs/{run_id}/events` + `Last-Event-ID`.

**SSE event types** (each `id: <seq>`, `event: <type>`, `data: <json>`):

| type                            | data                                                               |
| ------------------------------- | ------------------------------------------------------------------ |
| `run.started`                   | `{run_id, conversation_id, user_message_id}`                       |
| `route.decided`                 | `{domains:[{domain,score,primary}], intent, confidence}`           |
| `memory.retrieved`              | `{count, items:[{id,label,kind}]}`                                 |
| `plan.created` / `plan.updated` | `{version, steps:[{id,description,status,tool?}]}`                 |
| `token`                         | `{delta, block_index}` (assistant text)                            |
| `citation`                      | `{block_index, ref:{type,id,label}}`                               |
| `tool.started`                  | `{execution_id, tool, tier, args_preview}`                         |
| `tool.finished`                 | `{execution_id, status, summary, latency_ms}`                      |
| `tool.denied`                   | `{execution_id, tool, reason}`                                     |
| `approval.requested`            | `{approval_id, tool, tier, preview, expires_at}`                   |
| `approval.resolved`             | `{approval_id, status}`                                            |
| `run.promoted`                  | `{workflow_id}` (moved to background)                              |
| `run.status`                    | `{status}`                                                         |
| `message.completed`             | `{message_id, content, citations[], usage:{tokens_in,tokens_out}}` |
| `error`                         | `{code, message, retryable}`                                       |
| `heartbeat`                     | `{}` every 15 s                                                    |
| `done`                          | `{run_id, status}`                                                 |

Errors before stream start use normal HTTP problem+json.

`POST /messages/{id}/feedback` — U — `{feedback:"up"|"down", note?}` → `204`.

## 3. Agent runs, tasks (agent execution)

| `POST /runs` ⟲ | U | `{goal: string(≤2000), domains?: Domain[], mode?: "background", conversation_id?, budget?: {max_steps?,max_cost_usd?}}` | `202 RunOut` (starts `AgentRunWorkflow`) |
| `GET /runs` | U | `?status&trigger_type&since&limit&cursor` | `{items:[RunSummary], next_cursor}` |
| `GET /runs/{id}` | U | — | `RunOut {id,goal,status,trigger_type,domains,routing,plan,usage,error,final_output,started_at,finished_at,workflow_id,trace_id}` |
| `GET /runs/{id}/steps` | U | | `{items:[StepOut{seq,node,status,summary,llm_calls,started_at,finished_at}]}` |
| `GET /runs/{id}/tools` | U | | `{items:[ToolExecutionOut{id,tool,tier,status,args_redacted,result_summary,policy_decision,approval_id,latency_ms,started_at}]}` |
| `GET /runs/{id}/events` | U | SSE replay from `Last-Event-ID` | stream |
| `POST /runs/{id}/cancel` | U | `{reason?}` | `202 {status:"cancelling"}` |
| `POST /runs/{id}/pause` / `/resume` | U | | `202` |
| `POST /runs/{id}/messages` | U | `{message}` (refine/steer in-flight run) | `202` |

**Tasks** (`work.tasks`)
| `GET /tasks` | U | `?status&due_before&due_after&project_id&goal_id&q&limit&cursor` | `{items:[TaskOut], next_cursor}` |
| `POST /tasks` | U | `{title, description?, due_at?, remind_at?, priority?(1-5), project_id?, goal_id?, contact_id?, recurrence?}` | `201 TaskOut` |
| `GET/PATCH/DELETE /tasks/{id}` | U | PATCH partial + `If-Match` | `TaskOut` / `204` |
| `POST /tasks/{id}/complete` | U | `{}` | `TaskOut` |
`TaskOut {id,title,description,status,priority,due_at,remind_at,source,goal_id,project_id,contact_id,commitment_memory_id,created_at,updated_at,version}`

## 4. Memory

| `GET /memory` | U | `?q&kind&status(active|suggested|archived)&domain&entity_id&pinned&source_type&limit&cursor` | `{items:[MemoryOut], next_cursor}` (q → hybrid search) |
| `GET /memory/{id}` | U | | `MemoryDetail {…MemoryOut, sources[], revisions[], entities[], used_in_runs[last 10]}` |
| `POST /memory` | U | `{kind, content, payload?, entities?[], importance?, expires_at?, pin?}` | `201 MemoryOut` (trust=user, confidence=1) |
| `PATCH /memory/{id}` | U | `{content?, payload?, importance?, pinned?, expires_at?}` `If-Match` | `MemoryOut` |
| `DELETE /memory/{id}` | U | `?block=true` | `204` |
| `POST /memory/{id}/confirm` | U | | `MemoryOut` (suggested→active) |
| `POST /memory/{id}/reject` | U | | `204` |
| `GET /memory/suggestions` | U | | `{items:[MemoryOut]}` |
| `POST /memory/search` | U | `{query, kinds?, domains?, entity_ids?, k≤50, explain?:bool}` | `{items:[{memory, score, components?}]}` |
| `POST /memory/bulk-delete` | U+ | `{by:"entity|source|kind|all", id?, confirm:true}` | `202 {workflow_id}` |
| `GET /entities` / `GET /entities/{id}` | U | `?type&q` | `EntityOut {id,type,name,aliases,attributes,links:{contact?,project?,repository?},relationships[],recent_memories[]}` |
| `POST /entities/{id}/merge` | U | `{into_entity_id}` | `EntityOut` |
| `GET /entities/merge-candidates` | U | | list |
`MemoryOut {id,kind,title,content,payload,importance,confidence,status,pinned,user_edited,trust_level,valid_from,valid_to,due_at,expires_at,sources_count,entity_refs[],created_at,updated_at,version}`

## 5. Approvals

| `GET /approvals` | U | `?status=pending|decided&limit&cursor` | `{items:[ApprovalOut]}` |
| `GET /approvals/{id}` | U | | `ApprovalOut {id,run_id,tool,risk_tier,preview,reasons[],flags:{tainted,untrusted_args[],anomaly},args_hash,status,requested_at,expires_at,editable_fields[]}` |
| `POST /approvals/{id}/approve` ⟲ | U (U+ for tier 4) | `{args_hash: string (must equal current), note?}` | `200 ApprovalOut` (409 `precondition_failed` if hash differs) |
| `POST /approvals/{id}/edit-and-approve` ⟲ | U | `{edits:{field:value}, note?}` | `200 ApprovalOut` (server recomputes hash, re-validates, re-runs policy) |
| `POST /approvals/{id}/reject` | U | `{reason?}` | `200 ApprovalOut` |
| `GET /standing-rules` / `POST /standing-rules` / `PATCH|DELETE /standing-rules/{id}` | U | `{name, tool_name, conditions, daily_limit?, expires_at(≤90d)}` | `StandingRuleOut` (tier-4 tools rejected 422) |
Email-link approvals: one-time signed token URL opens the dashboard approval page; **never** performs the action by GET.

## 6. Notifications & insights

| `GET /notifications` | U | `?unread=true&kind&limit&cursor` | `{items:[NotificationOut], unread_count, next_cursor}` |
| `POST /notifications/{id}/read` | U | | `204` |
| `POST /notifications/read-all` | U | | `204` |
| `GET /insights` | U | `?status&domain&limit&cursor` | `{items:[InsightOut]}` |
| `POST /insights/{id}/feedback` | U | `{action:"useful|not_useful|dismiss|snooze|acted|too_frequent", snooze_until?, note?}` | `204` |
| `POST /insights/{id}/act` ⟲ | U | `{action:"create_draft|create_task|..."}` | `202 RunOut` |
| `GET /notifications/stream` | U | SSE | events: `notification.created`, `approval.requested`, `insight.created`, `integration.status`, `run.status` |
| `GET/PUT /notification-settings` | U | `{channels:{in_app:true,email:true}, quiet_hours, daily_budget, urgent_in_quiet_hours, digest_time, sensitivity:"low|normal|high"}` | same |
| `GET /detectors` / `PATCH /detectors/{name}` | U | `{enabled?, params?, autonomy_level?}` | `DetectorOut` |

## 7. Integrations

| `GET /integrations` | U | | `{items:[IntegrationOut{id,provider,account,status,scopes,last_sync_at,last_error,settings,sync:{state,progress}}]}` |
| `POST /integrations/{provider}/connect` ⟲ | U | `{scopes?: string[], redirect_after?}` | `200 {authorization_url, state}` (state bound to user+PKCE verifier stored server-side, TTL 10 min) |
| `GET /integrations/{provider}/callback` | W/U | `code,state` (browser redirect) | `302` to `/settings/integrations?connected=provider` (validates state, exchanges code server-side, stores encrypted tokens) |
| `PATCH /integrations/{id}/settings` | U | provider-specific: gmail `{labels_include[], backfill_days≤180}`, gdrive `{folder_ids[]}`, github `{repo_ids[], stale_pr_days}` | `IntegrationOut` |
| `POST /integrations/{id}/sync` ⟲ | U | `{mode:"incremental|reconcile"}` | `202 {workflow_id}` |
| `POST /integrations/{id}/pause` / `/resume` | U | | `IntegrationOut` |
| `DELETE /integrations/{id}` | U+ | `{data_handling:"retain|archive|purge"}` | `202 {workflow_id}` (revokes token at provider, deletes credentials, runs data handling) |
| `GET /integrations/github/install-url` | U | | `{url}` (GitHub App installation) |
| `POST /webhooks/gmail` | W | Pub/Sub push; JWT verified | `204` |
| `POST /webhooks/gcal` / `/webhooks/gdrive` | W | `X-Goog-Channel-Token` + `X-Goog-Channel-ID` verified against stored hash | `200` |
| `POST /webhooks/github` | W | `X-Hub-Signature-256` HMAC over raw body; `X-GitHub-Delivery` dedup | `202` |

## 8. Dashboard

| `GET /dashboard/home` | U | | `{briefing:{id,generated_at,sections[{domain,title,items[]}]}, today:{events[],tasks[]}, attention:{pending_approvals,overdue_commitments,suggested_memories,integration_alerts}, insights:[InsightOut top 5]}` |
| `GET /dashboard/briefings` | U | `?limit&cursor` | list |
| `GET /dashboard/briefings/{id}` | U | | `BriefingOut` |
| `POST /dashboard/briefings/generate` ⟲ | U | `{}` | `202 {workflow_id}` |
| `GET /dashboard/stats` | U | `?range=7d|30d` | `{runs:{total,succeeded,failed}, approvals:{...}, insights:{delivered,acted,dismissed}, cost:{usd,tokens}, memory:{active,suggested}}` |
| `GET /dashboard/pipeline` | U | | sales summary: opportunities by stage, silent leads |
| `GET /dashboard/dev` | U | | repos: open PRs, stale PRs, failing CI |

## 9. Goals & projects

| `GET /goals` / `POST /goals` | U | `{title, description?, domain, target_metric?, target_date?, review_cadence?, project_id?, parent_goal_id?}` | `GoalOut {…, progress_pct, status, linked_tasks_count}` |
| `GET/PATCH/DELETE /goals/{id}` | U | PATCH `{status?, current_value?, …}` | |
| `POST /goals/{id}/progress` | U | `{value, note?}` | `GoalOut` |
| `GET /projects` / `POST /projects` | U | `{name, description?, status?, priority?, target_date?, repository_ids?}` | `ProjectOut {…, entity_id, tasks_summary, goals[], repositories[]}` |
| `GET/PATCH/DELETE /projects/{id}` | U | | |
| `GET /contacts`, `/leads`, `/opportunities` (+ CRUD) | U | filters by stage/silent_days | CRM rows |

## 10. Activity & audit

| `GET /activity` | U | `?type=run|tool|approval|memory|integration|security&since&until&limit&cursor` | `{items:[ActivityItem{id,at,type,summary,run_id?,actor:"user|zara|system",status,links}]}` |
| `GET /activity/audit` | U | same filters | audit_log rows (metadata only) |
| `GET /activity/export` ⟲ | U+ | `{since,until,format:"json|csv"}` | `202 {workflow_id}` → file via signed URL |
| `GET /activity/security-flags` | U | | injection/poisoning flags with source refs |

## 11. Workflows (user-facing visibility/control)

| `GET /workflows` | U | `?type&status&limit&cursor` | `{items:[WorkflowOut{id,type,workflow_id,status,started_at,closed_at,schedule_id?,last_heartbeat_at}]}` |
| `GET /workflows/{id}` | U | | `WorkflowDetail {…, input_summary, result_summary, pending_signals?, children[]}` |
| `POST /workflows/{id}/cancel` / `/pause` / `/resume` | U | | `202` |
| `GET /schedules` | U | | scheduled automations (briefing time, monitors) with next run |
| `PATCH /schedules/{name}` | U | `{enabled?, cron_local?, timezone?}` | |
| `POST /system/kill-switch` | U | `{scope:"user", enabled:bool}` | `{enabled}` (user-level pause of all autonomy) |

## 12. Internal/admin (S auth, not exposed via BFF)

`GET /admin/dlq`, `POST /admin/dlq/{id}/replay`, `GET /admin/runs`, `POST /admin/kill-switch {scope,key,enabled}`, `GET /admin/costs`, `GET /health`, `GET /ready`, `GET /metrics` (internal network only).

## 13. Representative contract examples

`POST /api/v1/approvals/{id}/approve`

```json
// request
{ "args_hash": "b64:Zk3…", "note": "Looks good" }
// 200
{ "id":"…","status":"approved","decided_at":"2026-11-23T05:21:04Z","decided_via":"web","run_id":"…" }
// 409 (args changed)
{ "type":"https://zara.app/errors/precondition_failed","title":"Approval no longer matches action","status":409,"code":"precondition_failed","request_id":"…" }
```

`GET /api/v1/memory?q=pricing&kind=commitment`

```json
{
  "items": [
    {
      "id": "…",
      "kind": "commitment",
      "content": "The user committed to send Priya Nair the pricing proposal by Friday 2026-11-27.",
      "importance": 0.88,
      "confidence": 0.9,
      "status": "active",
      "due_at": "2026-11-27T12:30:00Z",
      "sources_count": 1,
      "pinned": false,
      "user_edited": false,
      "version": 3
    }
  ],
  "next_cursor": null
}
```

## 14. Acceptance criteria — API

1. OpenAPI schema generated in CI; breaking-change check against previous release (`oasdiff`) blocks merge unless version bumped.
2. Every endpoint has: auth test (401 without token), tenant isolation test (other tenant's id → 404), validation test (422), and rate-limit test where specified.
3. SSE: reconnect with `Last-Event-ID` replays missed events exactly once, ordered; heartbeat every ≤15 s; stream closes with `done`.
4. Idempotent ⟲ endpoints return the original response for repeated `Idempotency-Key` (24 h) and 409 for same key with different body.
5. Approval endpoints reject mismatched `args_hash` (409) and tier-4 approvals without step-up (403 `step_up_required`).
6. Webhook endpoints reject invalid signatures/JWTs without side effects (401/403) and dedup replays.
7. p95 latencies (excluding LLM streaming): read endpoints < 150 ms, writes < 300 ms at 50 concurrent users.
8. No response ever includes token ciphertext, `args` unredacted for redacted fields, or other users' data (response-schema allowlist test).
