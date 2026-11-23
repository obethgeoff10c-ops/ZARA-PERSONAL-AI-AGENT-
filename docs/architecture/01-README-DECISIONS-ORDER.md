# ZARA — Engineering Specification (Prompt 02)

**Status:** v1.0 implementation spec. Derived from `00-ARCHITECTURE-v1.md` (the source of truth).
**Audience:** OpenCode (implementation agent). Do not make architectural decisions not recorded here. If something is unspecified, stop and flag it under "Open Decisions".

## File index

| File                         | Contents                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------- |
| 00-ARCHITECTURE-v1.md        | Approved architecture (Prompt 01 output)                                         |
| 01-README-DECISIONS-ORDER.md | This file: resolved decisions, open decisions, repo layout, implementation order |
| 02-DATABASE.md               | Schemas, ER diagram, SQLAlchemy conventions, Alembic plan, RLS                   |
| 03-MEMORY-ENGINE.md          | Memory types, extraction schema, lifecycle, retrieval, examples                  |
| 04-AGENT-STATE-ROUTER.md     | Agent state, checkpointing, context router                                       |
| 05-TOOLS.md                  | Tool registry, schema, pipeline, tool catalogue                                  |
| 06-PROACTIVE-WORKFLOWS.md    | Proactive pipeline + Temporal workflow specs                                     |
| 07-API.md                    | REST/SSE contracts                                                               |
| 08-FRONTEND.md               | Routing, components, streaming, approval UX                                      |
| 09-SECURITY-TESTING.md       | Security controls, test strategy, acceptance criteria                            |

---

## 1. Decisions resolved in this spec (binding)

| ID   | Decision                                                                                                                                                                                                                                                         | Rationale                                                                      |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| D-01 | **LangGraph is used** for the in-run reasoning graph (route → retrieve → plan → act → verify → commit), but **with no LangGraph checkpointer database**. Durability = Temporal (background runs) + `agent.agent_runs.state_snapshot` written at node boundaries. | Honors the product stack while keeping a single durable-state owner (ADR-002). |
| D-02 | Interactive chat turns run in the API process (streaming). Any turn that needs a T2+ write, approval, or >30 s of work **hands off to `AgentRunWorkflow`** and the UI follows via SSE.                                                                           | Keeps chat responsive; durability only where needed.                           |
| D-03 | Multi-tenancy: shared schema, `tenant_id` on every tenant-owned row, Postgres RLS enforced via `SET LOCAL app.tenant_id` / `app.user_id` per transaction. MVP: one tenant per user (personal workspace); orgs later.                                             | Cheap to start, SaaS-ready.                                                    |
| D-04 | Primary keys: **UUIDv7** generated in the application (library `uuid-utils`), type `uuid`.                                                                                                                                                                       | Time-ordered, index-friendly.                                                  |
| D-05 | Auth: **managed IdP** (default choice: **Clerk**; swap-able behind `AuthProvider` interface). Backend verifies JWT (JWKS), maps `sub` → `core.users.external_auth_id`. ZARA stores no passwords.                                                                 | ADR-009. See open decision O-02 for final vendor confirmation.                 |
| D-06 | Embeddings: `EmbeddingProvider` interface; default implementation **Voyage AI** (`voyage-3-large`-class, dimension **1024**). Column `vector(1024)`; `embedding_model` stored per row.                                                                           | Claude has no embeddings API. Dimension fixed now to avoid index churn.        |
| D-07 | Vector index: **HNSW**, cosine (`vector_cosine_ops`), `m=16, ef_construction=64`, query `ef_search=80`. Always filter by `tenant_id` + `user_id`.                                                                                                                | pgvector standard.                                                             |
| D-08 | Full-text: `tsvector` generated column (`english` config) + GIN.                                                                                                                                                                                                 | Hybrid retrieval.                                                              |
| D-09 | LLM models referenced by **logical role** (`router`, `extractor`, `executor`, `planner`, `reviewer`, `composer`) mapped in config to current Claude model IDs. No model IDs in code.                                                                             | Models change.                                                                 |
| D-10 | Queues: Temporal task queues `zara-interactive`, `zara-background`, `zara-sync`, `zara-maintenance`. No Redis in MVP.                                                                                                                                            | ADR-003.                                                                       |
| D-11 | Events use a **transactional outbox** (`events.outbox`) relayed by a Temporal-scheduled relay workflow every 5 s (and signalled on write).                                                                                                                       | No Kafka.                                                                      |
| D-12 | Python 3.12, FastAPI, SQLAlchemy 2.0 (async, `asyncpg`), Pydantic v2, Alembic, `uv` for deps, `ruff` + `mypy --strict` on `zara/`, `pytest`.                                                                                                                     | Standardize.                                                                   |
| D-13 | Frontend: Next.js (App Router), TS strict, TanStack Query, Zustand (UI-only state), shadcn/ui, Tailwind, `openapi-typescript` generated client. SSE via `fetch` + `ReadableStream` (not `EventSource`, to allow auth headers).                                   |                                                                                |
| D-14 | Timestamps: `timestamptz` everywhere, UTC stored; user timezone in `user_profiles.timezone` (IANA).                                                                                                                                                              |                                                                                |
| D-15 | Money/amounts: `numeric(14,2)` + `currency char(3)`.                                                                                                                                                                                                             |                                                                                |
| D-16 | Soft-delete is used **only** for: memories (status), entities (merged), conversations (`archived_at`). Everything else hard-delete via deletion workflow.                                                                                                        | Privacy.                                                                       |
| D-17 | Gmail MVP scopes: `gmail.readonly`, `gmail.compose`. **No `gmail.send` scope in MVP**; sending is implemented as "create draft → user sends from Gmail" in MVP, with `send_email` tool built but feature-flagged off.                                            | Smaller verification surface; resolves arch Open Q5 for MVP.                   |
| D-18 | GitHub: **GitHub App** (installation tokens, per-repo permissions), read-only in MVP (`contents: read`, `issues: read`, `pull_requests: read`, `metadata: read`, `checks: read`).                                                                                | ADR-012.                                                                       |
| D-19 | Temporal: **Temporal Cloud** for staging/prod; local `temporalio/auto-setup` in Compose. Python SDK `temporalio`.                                                                                                                                                | Arch Open Q3 resolved.                                                         |
| D-20 | Notification channels MVP: in-app (SSE + inbox) and email (Resend/SES behind `EmailSender` interface). Web push post-MVP.                                                                                                                                        |                                                                                |
| D-21 | Chat streaming protocol: SSE event types defined in 07-API.md; each event has monotonic `seq`; resume via `Last-Event-ID`. Events are persisted in `agent.run_events` for replay.                                                                                |                                                                                |

## 2. Open decisions (must be answered by the founder; defaults noted)

| ID   | Question                                                                                       | Default if unanswered                                                   | Blocks              |
| ---- | ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------- |
| O-01 | Final embeddings vendor (Voyage vs self-hosted BGE)                                            | Voyage, 1024-dim                                                        | Phase 1 start       |
| O-02 | Final IdP (Clerk vs Auth0 vs WorkOS vs Supabase)                                               | Clerk                                                                   | Phase 0 auth        |
| O-03 | Exact Claude model IDs per role and monthly budget per user                                    | Config placeholders; confirm at implementation                          | Phase 1 LLM gateway |
| O-04 | Hosting target (Fly.io / Render / Cloud Run / ECS)                                             | Docker Compose only until Phase 6                                       | Phase 6             |
| O-05 | Launch geography / data residency                                                              | Single region (US or EU – choose one)                                   | Phase 6             |
| O-06 | Gmail send in first public release                                                             | Draft-only                                                              | Phase 7             |
| O-07 | Email provider for notifications (Resend vs SES)                                               | Resend                                                                  | Phase 5             |
| O-08 | Web research provider for `web_search` tool (Brave / Tavily / Exa / Anthropic web search tool) | Anthropic server-side web search tool if enabled on account; else Brave | Phase 3             |
| O-09 | Default autonomy level per domain                                                              | `suggest` for all; `draft` for email                                    | Phase 5             |
| O-10 | CRM scope for MVP: native leads/opportunities tables only, or HubSpot adapter                  | Native tables only                                                      | Phase 8             |

## 3. Repository layout (binding)

```
zara/
├── apps/web/                          # Next.js
├── services/core/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   └── zara/
│       ├── config.py                  # pydantic-settings; no os.environ elsewhere
│       ├── db/                        # engine, session, rls context, base, mixins
│       ├── models/                    # SQLAlchemy models, one file per schema group
│       ├── schemas/                   # Pydantic request/response models
│       ├── api/                       # routers/, deps.py, errors.py, sse.py
│       ├── auth/
│       ├── agent/                     # graph.py, state.py, nodes/, ledger.py, prompts/
│       ├── routing/                   # router.py, lenses/{personal,executive,sales,developer}.py
│       ├── memory/                    # extract.py, store.py, retrieve.py, consolidate.py, expire.py
│       ├── graph/                     # entities, resolution, traversal
│       ├── tools/                     # registry.py, policy.py, pipeline.py, builtin/
│       ├── approvals/
│       ├── integrations/{base.py,gmail,gcal,gdrive,github}/
│       ├── events/                    # ingest.py, outbox.py, relay.py, dlq.py
│       ├── proactive/                 # detectors/, scoring.py, throttle.py, composer.py
│       ├── notifications/
│       ├── llm/                       # gateway.py, roles.py, budgets.py, embeddings/
│       ├── security/                  # crypto.py, taint.py, redact.py, ssrf.py
│       ├── workflows/                 # *_workflow.py, activities/
│       ├── observability/
│       └── entrypoints/{api,worker,ingest}.py
├── packages/api-client/
├── evals/{datasets,runners,security}/
├── tests/{unit,integration,e2e,contract,fixtures}/
├── infra/docker/ , infra/otel/
└── docs/{architecture,adr,runbooks,security}/
```

**Import rules (enforced by `import-linter`):**

- `api` → may import `agent, memory, tools, approvals, integrations(read-only status), notifications, schemas`.
- Only `tools` imports `integrations` (for execution). Only `llm` imports `anthropic`/embedding SDKs. Only `memory` writes `memory.*` tables. Only `events` writes `events.*`. Only `agent.ledger` writes `agent.*` run/step tables. Only `security.crypto` touches credential ciphertext.
- `workflows` may import anything in `zara/` but workflow _definition_ files may import only `temporalio`, `zara.workflows.types` and pure-data modules (determinism rule).

## 4. Implementation order (binding)

| #   | Milestone                  | Deliverables                                                                                                 | Acceptance gate                     |
| --- | -------------------------- | ------------------------------------------------------------------------------------------------------------ | ----------------------------------- |
| M0  | Scaffold                   | Monorepo, Compose (postgres+pgvector, temporal, otel-collector, minio, mailpit), CI, lint/type/test baseline | `make up` healthy; CI green         |
| M1  | DB foundation              | Base/mixins, RLS context, migrations 0001–0004 (core, audit, integrations, events), crypto module            | RLS cross-tenant tests pass         |
| M2  | Auth                       | IdP JWT verification, user provisioning, sessions table, `/auth/*`, `/me`                                    | Auth E2E + negative tests           |
| M3  | LLM gateway + embeddings   | Role routing, budgets, usage ledger, caching flags, Voyage provider, OTel spans                              | Gateway unit + live smoke test      |
| M4  | Memory + entities          | Migrations 0005–0006, extraction, store, hybrid retrieval, resolution, memory API                            | Retrieval eval ≥ thresholds (03)    |
| M5  | Agent core + router        | LangGraph graph, router, ledger, chat API + SSE, conversations                                               | Router eval; chat E2E               |
| M6  | Integrations               | Provider base, OAuth flows, token vault, Gmail/Calendar/GitHub sync, webhooks, outbox/relay/DLQ              | Contract tests; reconciliation test |
| M7  | Tools + policy + approvals | Registry, pipeline, policy engine, taint, approvals API/UI                                                   | Security suite passes               |
| M8  | Temporal runtime           | `AgentRunWorkflow`, `UserRuntimeWorkflow`, sync workflows, signals                                           | Kill-worker recovery test           |
| M9  | Proactive engine           | Detectors, scoring, throttling, composer, notifications, briefing, meeting prep                              | Noise-budget tests; feedback loop   |
| M10 | Frontend completion        | All screens in 08                                                                                            | Playwright suite                    |
| M11 | Hardening                  | Pen-test fixes, load, DR drill, deletion workflow, export                                                    | Production readiness list in 00 §31 |

**Rule:** Do not start M9 before M7's security acceptance passes.

## 5. Global conventions for implementers

1. Every table: `id uuid PK`, `tenant_id uuid NOT NULL`, `created_at`, `updated_at` (trigger-maintained) unless stated.
2. Every external-write tool call requires `idempotency_key`.
3. Never log token values, email bodies, or prompt text at INFO. Use redaction helpers.
4. Every public function that calls an LLM goes through `llm.gateway`.
5. No mock providers in `zara/` (only in `tests/`); CI check `scripts/check_no_mocks.py`.
6. All timestamps in API are RFC 3339 UTC.
7. Errors follow the problem-details format in 07-API.md.
