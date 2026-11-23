# ZARA

Persistent, proactive personal-intelligence agent: one orchestrator, one memory,
one permission system, and four context lenses (Personal, Executive, Sales,
Developer).

This repository is at **Milestone 0 — production monorepo foundation**. It runs
the real plumbing (API, web shell, PostgreSQL + pgvector, Redis, migrations,
logging, health checks, tests, CI) and deliberately contains **no agent,
memory, integration, tool, or workflow behaviour yet**.

## Stack

| Layer   | Choice                                                                                   |
| ------- | ---------------------------------------------------------------------------------------- |
| Web     | Next.js 16 (App Router), React 19, TypeScript strict, Tailwind CSS 4, shadcn/ui          |
| API     | FastAPI, Python 3.12, Pydantic v2, uvicorn                                               |
| Data    | PostgreSQL 16 + pgvector, SQLAlchemy 2 async (`asyncpg`), Alembic                        |
| Cache   | Redis 7 (provisioned and readiness-checked; not a queue — D-10 keeps queues in Temporal) |
| Tooling | uv, ruff, mypy strict, pytest, ESLint, Prettier, Vitest, GitHub Actions                  |
| Runtime | Docker Compose for local development                                                     |

## Repository layout

```
.
├── apps/web/                  Next.js application
├── packages/api-client/       Generated OpenAPI types + typed fetch client
├── services/core/             FastAPI service, SQLAlchemy, Alembic, tests
│   ├── zara/
│   │   ├── api/               app factory, health, problem+json errors, OpenAPI
│   │   ├── db/                engine, sessions, RLS context, base/mixins
│   │   ├── observability/     JSON logging, request correlation
│   │   └── entrypoints/       uvicorn entrypoint
│   ├── alembic/               migration environment and versions
│   └── tests/                 unit + integration tests
├── infra/docker/              Dockerfiles, entrypoint, PostgreSQL init
├── docs/architecture/         normative specifications (source of truth)
├── docs/development.md        developer guide
├── docker-compose.yml
├── Makefile
└── .github/workflows/ci.yml
```

The architecture specification places Python modules under `services/core/zara/`
(not a top-level `agent/` or `integrations/`). Those directories will be created
with their milestones, not as empty placeholders.

## Quick start

Requirements: Docker with Compose v2. No Docker Desktop-specific features are
used, so OrbStack, Colima, or Podman with the Docker socket also work.

```bash
cp .env.example .env
docker compose up --build
```

| URL                            | Purpose                           |
| ------------------------------ | --------------------------------- |
| <http://localhost:3000>        | Web foundation status             |
| <http://localhost:8000/health> | API liveness                      |
| <http://localhost:8000/ready>  | API readiness (PostgreSQL, Redis) |
| <http://localhost:8000/docs>   | OpenAPI UI (non-production)       |

The API container waits for PostgreSQL, runs `alembic upgrade head`, then starts
with reload enabled. Stop with `Ctrl+C` (foreground) or `make down`.

## Host-side development

```bash
make bootstrap                 # .env + uv sync + npm ci
docker compose up -d postgres redis
cd services/core && uv run alembic upgrade head
```

Then, in separate terminals:

```bash
cd services/core && uv run uvicorn zara.api.app:create_app --factory --reload --port 8000
npm run dev
```

## Commands

```bash
make help          # list targets
make up            # start the stack (build + background)
make logs          # follow logs
make migrate       # alembic upgrade head in the API container
make check         # ruff, mypy, pytest, eslint, tsc, vitest, next build
make verify        # check + docker compose config
make api-types     # regenerate OpenAPI JSON and the TypeScript client
make clean         # remove local caches (never database volumes)
```

Backend-only:

```bash
cd services/core
uv run pytest
uv run pytest --cov=zara --cov-report=term-missing
uv run mypy zara
uv run alembic upgrade head --sql
```

## Configuration

Copy `.env.example` to `.env`. Never commit `.env`.

| Variable                                              | Default (local)                                | Notes                                                                          |
| ----------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------ |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `zara` / `zara` / `replace-me`                 | Compose only; `.env.example` contains placeholders and production rejects them |
| `DATABASE_URL`                                        | `postgresql+asyncpg://...@localhost:5432/zara` | Must use `asyncpg`; Compose points it at `postgres`                            |
| `REDIS_URL`                                           | `redis://localhost:6379/0`                     | Optional; when set, `/ready` checks Redis                                      |
| `ENVIRONMENT`                                         | `local`                                        | `local`, `test`, `staging`, `production`                                       |
| `LOG_LEVEL`                                           | `INFO`                                         | JSON logs                                                                      |
| `CORS_ORIGINS`                                        | `http://localhost:3000,http://127.0.0.1:3000`  | Comma-separated bare origins                                                   |
| `ALLOWED_HOSTS`                                       | `localhost,127.0.0.1`                          | Comma-separated; rejects `*` in production                                     |
| `API_INTERNAL_URL`                                    | `http://localhost:8000`                        | Server-side web → API                                                          |
| `NEXT_PUBLIC_API_URL`                                 | `http://localhost:8000`                        | Browser-visible API origin                                                     |

Production adds checks: HTTPS CORS origins, no wildcard hosts, and a real
database password. Settings live in `services/core/zara/config.py`; do not read
`os.environ` elsewhere.

## Security posture (M0)

- No secrets or credentials are committed; `.env` is ignored and `.env.example`
  contains replaceable placeholders. Production startup rejects placeholder
  passwords.
- Errors are RFC 9457 problem+json and never include stack traces, SQL, or DSNs.
- Logs redact authorization, cookies, tokens, passwords, and secret-shaped
  strings.
- CORS is an explicit allowlist; trusted-host checking is enabled.
- Database sessions expose `rls_session()` for tenant isolation in M1.
- Containers run as non-root (API `uid 10001`, web `node`).
- Compose publishes ports on `127.0.0.1` only.

Authentication, authorization, tool policy, approvals, and RLS policies arrive
in later milestones against `docs/architecture/09-SECURITY-TESTING.md`.

## Documentation

- [`docs/architecture/README.md`](docs/architecture/README.md) — normative specs
  and precedence.
- [`docs/development.md`](docs/development.md) — migrations, configuration,
  logging, tests, API generation, troubleshooting.

## Scope guardrails

- Specs override code. Do not invent architecture that is not in
  `docs/architecture/`.
- Do not disable lint or type checks to make a build pass.
- No mock providers in `zara/`; fakes belong in `tests/` and `evals/`.
- Do not start M9 (proactive engine) before M7 security acceptance.
