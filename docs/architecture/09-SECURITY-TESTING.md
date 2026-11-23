# 09 — Security & Testing Specification

## Part A — Security

### 1. OAuth security

- **Authorization Code + PKCE (S256)** for Google; GitHub App user-to-server flow for identity + installation flow for repo access. No implicit flow, no tokens in URLs/fragments.
- `state`: 256-bit random, stored server-side (`integrations.oauth_states(state_hash, user_id, tenant_id, provider, code_verifier_enc, redirect_after, expires_at(10 min), used_at)`); callback validates state, user session match, single use.
- Redirect URIs: exact-match allowlist per environment; `redirect_after` restricted to same-origin relative paths.
- Scopes (D-17/D-18): Gmail `gmail.readonly`, `gmail.compose`; Calendar `calendar.events` (+`calendar.readonly` if separate calendars needed); Drive `drive.file` only; GitHub App read-only permissions. **Incremental authorization** (`include_granted_scopes`); never request scopes not used by a shipped tool (CI check: tool `required_scopes` ⊆ requested scopes and vice-versa).
- Google `access_type=offline` + `prompt=consent` only on first connect; refresh tokens stored encrypted; refresh handled in a single service with DB advisory lock `pg_try_advisory_xact_lock(hash(integration_id))` to prevent races; failed refresh (`invalid_grant`) → `needs_reauth`.
- Token revocation: on disconnect, call provider revoke endpoint (Google `oauth2.googleapis.com/revoke`; GitHub installation token revocation + app uninstall guidance), then delete ciphertext, webhooks/watch subscriptions stopped, cursors deleted, workflows paused/cancelled. Provider-initiated revocation detected via 401/`invalid_grant` → same cleanup path (minus provider call).
- Google API Services User Data Policy compliance (Limited Use): no use of Gmail data for ads/model training; documented in privacy policy; restricted-scope verification & CASA assessment tracked in release plan (arch §2.2).

### 2. Encryption & secret storage

- **In transit:** TLS 1.2+ (1.3 preferred), HSTS, internal service traffic over private network/TLS.
- **At rest:** managed Postgres/object storage encryption (AES-256).
- **Application-level envelope encryption (credentials & restricted fields):** per-user data encryption key (DEK, AES-256-GCM) generated on first connect; DEK wrapped by KMS CMK (`kms_key_id`, AWS KMS/GCP KMS); AAD = `tenant_id|user_id|integration_id|enc_version`; random 96-bit nonce per encryption; ciphertext layout `version(1)|nonce(12)|ct|tag(16)`. DEK cache in worker memory ≤5 min. KMS key rotation annual + `enc_version` re-wrap job. Dev uses a local KMS-compatible stub _only in `tests/`/docker dev profile_ (prod refuses to boot without `KMS_PROVIDER` ≠ `local`).
- **Secrets:** all runtime secrets (DB URLs, Anthropic key, Voyage key, IdP secret, GitHub App private key, webhook secrets, KMS config) in cloud secret manager; injected as env at start; no secrets in images or git; pre-commit + CI secret scanning (gitleaks); separate secrets per environment; rotation runbook.
- **Search/observability:** log/trace redaction filters (token patterns, email bodies, `Authorization`) applied at the logging layer and OTel span processor; prompt/response capture to Langfuse is **off by default**, opt-in per environment with redaction (PII scrubbing) and short retention.
- **Backups:** encrypted; credential ciphertext is useless without KMS access.

### 3. Tenant isolation

- RLS on all tenant-tables (02 §0) with `FORCE`; `zara_app` role cannot bypass; migrator role used only in migrations.
- Per-request/per-activity `rls_session(tenant_id,user_id)`; background code without context fails closed (no rows).
- Object storage keys prefixed `t/{tenant}/u/{user}`; pre-signed URLs ≤5 min, bound to key.
- Caches (embedding cache, rate limit, profile summary) keyed with tenant+user.
- Temporal: workflow IDs include user id; search attributes include `TenantId`; activities re-derive tenant context from DB by `run_id`, never from client-supplied input.
- LLM context isolation: prompts built only from rows loaded under the active RLS context; assertion in `prompt_builder` that all injected items' `user_id` equal the session user.
- Tests: automated cross-tenant matrix (02 §6), retrieval leak tests, SSE channel isolation test.

### 4. Prompt-injection defenses (defense in depth)

1. **Trust labels** on all context (system/user/zara/external); external content only in user-role `<untrusted_data>` blocks with escaping; system prompt instructs to treat as data.
2. **Quarantined extraction:** LLM calls that read raw untrusted text (summarize email, extract commitments) have **no tools** and return schema-validated structured output. The privileged agent consumes structured outputs where possible.
3. **Taint tracking:** `AgentState.taint` monotonic; any run reading external content is tainted → tier ≥2 requires approval (05 §3).
4. **Critical-argument provenance:** recipients/attendees/repos/URLs derived only from untrusted text → approval with highlight.
5. **Tool allowlisting per run:** only tools from active domains and necessity (router + plan); tools not in plan not callable (policy step 3).
6. **No exfil channels:** `web.fetch` only GET with no query-string data from tainted context unless approved; no tool can post data to arbitrary URLs; UI blocks auto-loading remote images/links; markdown link rewriting.
7. **Output filters:** DLP scan on outbound drafts/messages (secrets, tokens, large pasted sections of unrelated private data, other users' data) → block + flag.
8. **Safety reviewer** (independent LLM call, role=`reviewer`) for tier ≥3 actions: given original user intent + proposed action + provenance summary (not raw untrusted text) → `approve_ok|suspicious`; suspicious → forced approval with warning (never auto-allow).
9. **Memory poisoning gates** (03 §2.4).
10. **Heuristic detectors:** injection phrase/shape classifier on inbound documents (regex + small model) → `security.flag` events; doc remains but marked `suspicious`; UI shows badge.
11. **Rate/anomaly limits:** max external writes/hour; new-recipient burst detection.
12. **Instruction hierarchy tests** in CI (below).
    Residual-risk statement documented in `docs/security/threat-model.md`: no complete prevention is claimed; approvals are the final control for external effects.

### 5. Tool allowlisting

Static registry (code-reviewed); per-run exposed set computed by router+plan; per-user disable list; integration-scope intersection; MCP tools require explicit install allowlist and pinned manifest hash; kill switches per tool. Any model request for a non-exposed tool → `denied(tool_not_allowed)` + security log.

### 6. Action approvals

See 05 §3–4, 07 §5, 08 §7. Additional controls: approval binding to `args_hash`; single-use; expiry; step-up for tier 4; edit → re-policy; approvals only decidable by the owning user session (not by service tokens, not via GET links); approval decisions audited with IP/user agent; email approval links open the UI only.

### 7. Audit logs

- Append-only `audit.audit_log` with per-tenant **hash chain**; events: auth, integration connect/revoke, tool executions (tier ≥1), approvals, memory create/edit/delete (ids only), policy denials, security flags, admin actions, data export/delete, kill-switch changes, standing-rule changes.
- Content excluded (hashes + ids + redacted summaries). Retention: 13 months (configurable); chain verification job daily (`audit.verify_chain`) alerts on break.
- Users can view/export their own audit data (API 07 §10). Admin access logged and reviewed.

### 8. SSRF prevention (web.fetch, webhook callbacks, any URL-taking tool)

- All outbound fetches go through `security/ssrf.py` + egress proxy (e.g., `smokescreen`-style) with:
  - scheme allowlist (`https`, `http` only if explicitly allowed), port allowlist (80, 443);
  - DNS resolution performed by the proxy; **block** loopback, link-local (169.254.0.0/16 incl. cloud metadata 169.254.169.254, `fd00:ec2::254`), RFC1918, CGNAT, multicast, IPv6 ULA/loopback, `0.0.0.0`; re-validate **after** redirects and on each hop (max 3); pin resolved IP for the connection (anti DNS-rebinding);
  - deny credentials in URL (`user:pass@`), non-ASCII/IDN homograph normalization to punycode then policy check;
  - response limits: 5 MB, 15 s total, content-type allowlist; no cookies; fixed User-Agent;
  - no file://, gopher://, ftp://, data: schemes.
- Cloud metadata endpoint additionally blocked at network layer; workers run with IMDSv2 hop-limit 1 / metadata disabled.
- Tests: SSRF corpus (≥100 payloads: decimal/octal/hex IPs, `localtest.me`-style rebinding hosts, redirects to internal, IPv6 mapped, DNS rebinding simulation) all blocked.

### 9. Webhook verification

| Source                                                                                                                                                                                                                                                                                                                                                          | Verification                                                                                                                                                                                                                                         |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Gmail Pub/Sub push                                                                                                                                                                                                                                                                                                                                              | Validate Google-signed OIDC JWT in `Authorization` (issuer `https://accounts.google.com`, audience = configured endpoint, `email` = configured push service account, not expired); map subscription → integration; only ids used then fetch from API |
| Calendar/Drive channels                                                                                                                                                                                                                                                                                                                                         | `X-Goog-Channel-Token` compared (constant-time) to per-channel secret hash; `X-Goog-Channel-ID` / `X-Goog-Resource-ID` must match an active `webhook_subscriptions` row                                                                              |
| GitHub App                                                                                                                                                                                                                                                                                                                                                      | `X-Hub-Signature-256` HMAC-SHA256 over the **raw body** with app webhook secret, constant-time compare; `X-GitHub-Delivery` dedup; `installation.id` must map to a known integration                                                                 |
| Common: body size cap (1 MB), per-source rate limit, reject unsigned, respond fast (202/204), no sensitive info in responses, replay window (reject events older than 24 h unless backfill), all failures logged with source IP (no payload). Webhook endpoints isolated (`zara-ingest` process) with minimal DB privileges (insert to `events`/`outbox` only). |

### 10. Rate limits & abuse controls

- API limits (07 §0); login brute-force limits at IdP + IP throttling.
- LLM: per-user daily token/cost caps; per-run caps; global circuit breaker.
- Provider call budgets (05 §6).
- Webhook flood protection; DLQ alerts.
- Prompt size limits (user message ≤ 20k chars; attachments ≤ 10 MB; page counts).

### 11. Data deletion & privacy

- **Memory-level delete** (03 §7), **conversation delete**, **integration disconnect** (retain/archive/purge), **account deletion** and **GDPR erasure** via `DataDeletionWorkflow`:
  1. Mark user `deleting`, pause workflows, revoke tokens at providers.
  2. Delete in dependency order: embeddings, chunks, documents, memory, graph, work/crm/dev rows, runs/messages, notifications, integrations, sessions; object-store prefix purge.
  3. Hash-chain audit tombstone (retain minimal non-content record: `user_deleted` with hashed identifier, timestamp) per legal basis.
  4. Verification step: queries confirm zero rows by `user_id` (excluding audit tombstone); backups age out within 35 days (documented).
  5. Notify user; provide completion certificate id.
- Export: JSON + CSV bundle (memories, entities, tasks, goals, conversations, activity) delivered via 24 h signed URL.
- Retention defaults: raw provider payloads 7 d; documents per integration window (user-configurable); run events 14 d; retrieval logs 30 d; audit 13 mo.
- Anthropic/Voyage data handling: commercial terms, no training; subprocessors listed; DPA-ready.

### 12. Other controls

- Dependency scanning (pip-audit, npm audit, Dependabot), SBOM, container scan (Trivy), SAST (Semgrep/Bandit/CodeQL).
- Security headers (CSP strict with nonces, no inline scripts, `frame-ancestors 'none'`, `Referrer-Policy`, `Permissions-Policy`), CORS allowlist, CSRF protection for cookie-authenticated BFF routes (SameSite=Lax + double-submit token on mutating routes).
- Least-privilege DB roles; separate credentials per process type (`api`, `worker`, `ingest` with insert-only).
- Admin tooling behind SSO + MFA + IP allowlist; all actions audited.
- Incident response runbook, key-compromise runbook, token-mass-revocation script.

### 13. Acceptance criteria — Security

1. Threat-model doc reviewed; each threat in arch §16.1 maps to ≥1 control and ≥1 automated test.
2. Injection corpus (≥150 cases across email, calendar description, PR body, Drive doc, web page; direct/indirect; multi-step; exfil via URL/markdown; recipient substitution; memory poisoning): **0 unapproved tier ≥2 executions; 0 disallowed-kind active memories; 0 data egress to non-allowlisted hosts.**
3. Cross-tenant matrix: 0 leaks.
4. Credential ciphertext decrypt tests with wrong AAD/tenant fail; plaintext never present in DB dumps, logs, traces (scan job over test run logs).
5. OAuth tests: state reuse/forgery/expired rejected; PKCE enforced; scope-mismatch alert.
6. Webhook tests: invalid signature/JWT/channel-token rejected; replay deduped.
7. SSRF suite 100% blocked.
8. Audit hash-chain tamper test detected by verification job.
9. Deletion workflow verified end-to-end (row counts zero; object prefix empty; tokens revoked at provider in contract test).
10. External penetration test completed before beta; criticals/highs resolved.

---

## Part B — Testing

### 14. Test pyramid and tooling

| Layer         | Tooling                                                                                                                  | Scope                                                                                                                 | Gate                                                                                                    |
| ------------- | ------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Unit          | pytest, hypothesis; vitest                                                                                               | pure logic: policy engine, scoring, routing rules, ranking math, crypto, parsers, state reducers                      | ≥85% line coverage on `tools/policy, routing, memory/rank, proactive/scoring, security/*`; ≥75% overall |
| Integration   | pytest + Testcontainers (Postgres+pgvector, Temporal test server/dev server, MinIO)                                      | repos + RLS, migrations, memory pipeline with real DB, workflows with real activities, API with ASGI client, webhooks | all pass on every PR                                                                                    |
| Contract      | recorded HTTP fixtures (VCR-style, **tests only**) + weekly live sandbox runs with dedicated test Google/GitHub accounts | provider clients (Gmail, Calendar, Drive, GitHub): request shapes, pagination, error mapping, schema drift detection  | live contract job nightly; failures page owner                                                          |
| E2E           | Playwright                                                                                                               | critical user journeys across web+API+worker (compose env)                                                            | on main + nightly                                                                                       |
| Evals         | `evals/` runners (pytest-driven), results to Langfuse/JSON                                                               | routing, extraction, retrieval, planning/tool-use, composer groundedness, proactive noise, security                   | thresholds block merges for prompt/model/config changes                                                 |
| Load          | k6/Locust                                                                                                                | chat concurrency, retrieval, ingestion                                                                                | pre-release                                                                                             |
| Chaos/failure | scripted fault injection                                                                                                 | see §20                                                                                                               | pre-release + monthly                                                                                   |

Rule: mocks/fixtures live only under `tests/` and `evals/`; production imports are checked by `scripts/check_no_mocks.py`. The LLM is **real** in evals (cached by hash of prompt+model to control cost; cache disabled for release gate runs). Unit tests that need an LLM use a deterministic fake client defined in `tests/fakes/` for control-flow only (never for quality claims).

### 15. Unit tests (examples that MUST exist)

- Policy engine truth table (tier × taint × autonomy × standing rule × anomaly × kill switch) — exhaustive parametrization.
- Idempotency key determinism and canonical-JSON stability.
- Retrieval scoring monotonicity properties; MMR; token packing never exceeds budget.
- Contradiction resolution precedence rules (03 §5), expiration calculations, decay.
- Router Stage A/C merge logic and fallbacks.
- Quiet-hours/notification budget logic with timezone/DST edge cases (Asia/Kolkata, America/New_York DST transitions).
- Crypto: encrypt/decrypt round trip, AAD binding, nonce uniqueness, tamper detection.
- Untrusted-block escaping and prompt builder invariants (no untrusted text in system role).
- SSE reducer ordering/dup handling (frontend).

### 16. Integration tests

- Migrations: empty→head, drift check, RLS-enabled assertion, partition creation.
- Memory pipeline: ingest doc → extract → gates → dedup → store → retrieve; poisoning cases.
- Tool pipeline end-to-end with a fake _provider server_ (tests only): approval flow, replay, concurrency idempotency, timeout/unknown outcome reconciliation, token refresh race.
- Temporal: `AgentRunWorkflow` with approval signals, cancellation, pause/resume, worker restart mid-run (using Temporal test env + replay), schedule overlap skip, outbox relay.
- Webhooks → events → workflows → insights with signature verification.
- API: every endpoint auth/isolation/validation matrix generated from OpenAPI.

### 17. End-to-end scenarios (Playwright, compose stack, test Google/GitHub sandbox or provider fakes in test env)

1. Onboarding → connect Gmail/Calendar/GitHub → initial sync completes → first brief generated.
2. Chat: "Prep me for my meeting with <contact>" → streamed answer with citations from real fixtures; run panel shows route/plan/tools.
3. Draft email request → approval card → edit & approve → draft created (T2) / send path flag off → UI explains.
4. Proactive: simulated silent lead → insight → "Draft it" → approval → done; feedback "not useful" suppresses repeat.
5. Memory: suggested memory confirm; edit memory → retrieval reflects edit; delete → never retrieved; "don't remember again".
6. Kill switch: user pauses autonomy → workflows pause, no deliveries; resume continues.
7. Reauth: revoke token at provider → UI shows reconnect → reconnect resumes sync.
8. Close browser mid-run → run completes in background → notification on return.
9. Account export and deletion flows.

### 18. Agent evaluations

Datasets in `evals/datasets/` (JSONL, versioned, with provenance & labeling guide). Metrics & thresholds (initial, tune after baseline):

| Eval                                                                                                                                                          | Metric                                                                           | Threshold                           |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ----------------------------------- |
| Routing (04)                                                                                                                                                  | domain F1 / primary acc                                                          | ≥0.90 / ≥0.93                       |
| Tool selection & args                                                                                                                                         | correct tool set (F1), arg exact/semantic match                                  | ≥0.90 / ≥0.85                       |
| Planning                                                                                                                                                      | plan validity (steps executable, deps acyclic), efficiency (steps ≤ reference+2) | ≥0.90 / ≥0.80                       |
| Task success (trajectory) — scenarios run against _fake provider servers_ + real LLM                                                                          | goal satisfied (LLM judge calibrated to human labels κ≥0.7)                      | ≥0.80 (MVP), ≥0.88 (GA)             |
| Groundedness                                                                                                                                                  | supported-claim rate (citations verified against retrieved context)              | ≥0.95                               |
| Hallucinated tool results                                                                                                                                     | rate of claims about data never retrieved                                        | ≤1%                                 |
| Approval compliance                                                                                                                                           | tier ≥3 actions executed without approval                                        | **0**                               |
| Briefing quality                                                                                                                                              | rubric score (completeness, prioritization, concision)                           | ≥4.0/5 avg; no fabricated items     |
| Proactive precision                                                                                                                                           | delivered insights labeled useful                                                | ≥0.75                               |
| Cost                                                                                                                                                          | median cost/run; p95                                                             | within budgets (set after baseline) |
| Regression: PRs touching `agent/prompts`, `routing`, `memory`, `llm/roles` run the eval suite; ≥2 pt drop on any metric blocks merge unless waived with note. |

### 19. Memory retrieval evaluations

As 03 §11. Additional: temporal queries ("what did I promise last week"), entity-disambiguation (two Priyas), contradiction recency, negative queries (nothing relevant → returns empty and agent says "I don't have that"), large-corpus scaling (50k items latency), multilingual spot checks (English + Hindi).

### 20. Tool execution tests & failure simulations

| Scenario                             | Injection method                                        | Expected                                                                                  |
| ------------------------------------ | ------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Worker kill mid-run / mid-tool       | `SIGKILL` worker                                        | run resumes; no duplicate side effects                                                    |
| Postgres failover / connection reset | pause/stop container                                    | retries; no data loss; user-visible degraded status                                       |
| Temporal outage                      | stop Temporal                                           | API accepts events into outbox; recovers & drains on return                               |
| Anthropic 429/529/timeout            | fault proxy                                             | backoff; fallback model; run `failed` with clear error after budget; no fabricated output |
| Embedding provider outage            | fault proxy                                             | writes queue (memory marked `pending_embedding`); keyword retrieval still works           |
| Google/GitHub 429/5xx                | fault proxy                                             | `Retry-After` honored; circuit breaker; sync resumes from cursor                          |
| Token revoked mid-run                | revoke at provider                                      | `needs_reauth`; run pauses/fails gracefully; user notified                                |
| Duplicate webhook storm              | replay 1000×                                            | single processing per dedup key                                                           |
| Missed webhooks                      | drop push                                               | reconciliation poll recovers within interval                                              |
| Clock skew / DST                     | time travel in tests                                    | schedules fire correctly in user tz                                                       |
| Disk/object-store failure            | stop MinIO                                              | large bodies degrade to inline/skip; no crash                                             |
| Poison event                         | malformed payload                                       | DLQ after retries; others unaffected; replay works                                        |
| Unknown-outcome send                 | drop response after provider accepted (tests-only fake) | reconciliation prevents double send                                                       |
| Budget exhaustion                    | set cost cap                                            | graceful stop, user message, no further LLM calls                                         |

### 21. Acceptance criteria — Testing

1. CI pipeline stages: lint/type → unit → integration → contract(replay) → build → e2e(smoke) → evals(on relevant paths) → security scans; main branch protected by required checks.
2. All "MUST exist" unit tests (§15) present; coverage thresholds met.
3. Nightly live contract tests green for 7 consecutive days before beta.
4. E2E scenarios 1–9 automated and green.
5. Eval thresholds in §18/§19 met on the release candidate; results archived.
6. All failure simulations in §20 executed pre-release with recorded outcomes and runbook updates.
7. Flaky test budget: <1% flaky rate; quarantined tests tracked with owners.
