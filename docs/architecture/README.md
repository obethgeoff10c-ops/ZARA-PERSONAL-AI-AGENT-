# ZARA architecture specifications

These files are the source of truth for implementation. They are normative and
read-only during feature work.

| Document                                                       | Contents                                                             |
| -------------------------------------------------------------- | -------------------------------------------------------------------- |
| [00-ARCHITECTURE-v1.md](./00-ARCHITECTURE-v1.md)               | Approved system architecture                                         |
| [01-README-DECISIONS-ORDER.md](./01-README-DECISIONS-ORDER.md) | Binding decisions D-01–D-21, open decisions, repo layout, milestones |
| [02-DATABASE.md](./02-DATABASE.md)                             | Schemas, SQLAlchemy conventions, migrations, RLS                     |
| [03-MEMORY-ENGINE.md](./03-MEMORY-ENGINE.md)                   | Memory types, extraction, lifecycle, retrieval                       |
| [04-AGENT-STATE-ROUTER.md](./04-AGENT-STATE-ROUTER.md)         | Agent state, checkpointing, context router                           |
| [05-TOOLS.md](./05-TOOLS.md)                                   | Tool registry, policy, execution pipeline                            |
| [06-PROACTIVE-WORKFLOWS.md](./06-PROACTIVE-WORKFLOWS.md)       | Proactive engine and Temporal workflows                              |
| [07-API.md](./07-API.md)                                       | REST/SSE contracts and error format                                  |
| [08-FRONTEND.md](./08-FRONTEND.md)                             | Routes, components, streaming UX                                     |
| [09-SECURITY-TESTING.md](./09-SECURITY-TESTING.md)             | Security controls, test strategy, acceptance criteria                |

## Precedence

1. `00-ARCHITECTURE-v1.md` for architecture.
2. `01-README-DECISIONS-ORDER.md` for binding decisions, including conflicts.
3. The remaining numbered specifications for their domains.
4. Code and README documentation must not contradict the above.

If an implementation need is not covered, record the question in the milestone
PR or an ADR under `docs/adr/`; do not silently invent a decision.

## Milestone 0

This repository currently implements M0 only: monorepo scaffold, API and web
shells, PostgreSQL/pgvector and Redis plumbing, logging, configuration,
health checks, migrations baseline, tests, Compose, and CI. Agent, memory,
integration, tool, and workflow code are intentionally absent until M1+.
