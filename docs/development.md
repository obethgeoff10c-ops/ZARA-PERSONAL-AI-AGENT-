# Local development

## Prerequisites

- Docker with Compose v2 (Docker Desktop, OrbStack, Colima, or Podman with the
  Docker socket). No Docker Desktop-specific features are used.
- Python 3.12 and [uv](https://docs.astral.sh/uv/) for host-side backend work.
- Node.js 22 or newer (24 is used in CI) for host-side frontend work.

Apple Silicon is supported: all base images are multi-arch (`python:3.12-slim`,
`node:24-bookworm-slim`, `pgvector/pgvector:pg16`, `redis:7.4-alpine`).

## First run

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:3000>. The API is at <http://localhost:8000>, docs at
<http://localhost:8000/docs>, liveness at `/health`, readiness at `/ready`.

The API entrypoint waits for PostgreSQL, runs `alembic upgrade head`, then
starts uvicorn with reload. Migrations therefore execute on every fresh start
and are idempotent. Set `ZARA_AUTO_MIGRATE=false` to run them yourself.

## Without Docker

Start PostgreSQL 16 with pgvector and Redis by any means, then:

```bash
cp .env.example .env
make bootstrap
cd services/core && uv run alembic upgrade head
```

Run the API and web app in separate terminals:

```bash
cd services/core && uv run uvicorn zara.api.app:create_app --factory --reload --port 8000
npm run dev
```

`.env.example` points at `localhost`; Compose overrides the URLs with container
hostnames.

## Database and migrations

- The Alembic baseline (`0001`) creates extensions, all schemas, the UUIDv7
  helper, and the `updated_at` trigger.
- The version table lives in `core.alembic_version`.
- Create a revision: `make migration name=add_memory_tables`.
- Apply inside Compose: `make migrate`.
- Verify emitted SQL without a database:
  `cd services/core && uv run alembic upgrade head --sql`.
- Downgrades exist for development only; production history is forward-only.

RLS is not exercised yet because M0 has no tenant tables. `zara.db.session.rls_session`
already sets `app.tenant_id`, `app.user_id`, and `app.role` as transaction-local
config for M1.

## Configuration

All settings are declared in `services/core/zara/config.py`. Invalid values
fail at startup, not at first request. Notable validation:

- `DATABASE_URL` must use `postgresql+asyncpg://`.
- `CORS_ORIGINS` is a comma-separated list of bare origins; paths and
  credentials are rejected.
- `ALLOWED_HOSTS` must be bare hosts and cannot be `*`.
- In `ENVIRONMENT=production`, CORS must be HTTPS, `ALLOWED_HOSTS` cannot be a
  wildcard, and placeholder database passwords are rejected.

Do not read `os.environ` outside `config.py`.

## Logging and errors

- Every log line is JSON with `timestamp`, `level`, `logger`, `message`,
  `service`, `environment`, and `request_id` when in a request.
- Sensitive keys (`authorization`, `token`, `password`, `secret`, cookies) and
  Bearer/`key=value` patterns are redacted.
- Errors use RFC 9457 `application/problem+json` with `type`, `title`, `status`,
  `detail`, `code`, `request_id`, and field `errors` for validation failures.
- Unhandled exceptions return a generic message; details stay in server logs.

## Health checks

| Endpoint      | Meaning                   | Dependencies                                   |
| ------------- | ------------------------- | ---------------------------------------------- |
| `GET /health` | Process is alive          | none                                           |
| `GET /ready`  | Process can serve traffic | PostgreSQL, plus Redis when `REDIS_URL` is set |

`/ready` returns `503` with a body listing each dependency when a check fails.
It never reveals connection strings or exception messages, only the exception
class name for the relevant dependency.

## Tests

```bash
make api-test          # unit tests, integration skipped
make web-test          # Vitest
ZARA_INTEGRATION=1 DATABASE_URL=... make api-test-all
```

Backend tests live in `services/core/tests/{unit,integration}`. Frontend tests
are colocated as `*.test.tsx`.

## API contract

FastAPI is the contract. Regenerate after changing a route or schema:

```bash
make api-types
```

This writes `packages/api-client/openapi.json` and
`packages/api-client/src/schema.d.ts`. Never edit generated files by hand. The
web app imports the typed client through the `@zara/api-client` workspace.

## Quality gates

```bash
make check        # ruff, mypy, pytest, eslint, tsc, vitest, next build
make verify       # check + docker compose config
```

CI runs the same commands, plus migrations against a real PostgreSQL service
and `docker compose build`.

## Conventions

- Python: 3.12, async SQLAlchemy 2, Pydantic v2, `ruff`, `mypy --strict`.
- TypeScript: strict plus `noUncheckedIndexedAccess`; no `any` without a reason.
- Never disable a lint or type rule to make a build pass.
- Never log tokens, credentials, email bodies, or prompt text at INFO.
- No fake providers in `zara/`; fakes belong in `tests/`.
- Timestamps are UTC; API timestamps are RFC 3339.

## Troubleshooting

**Port already in use.** Stop the conflicting process or change the published
port in `docker-compose.yml`.

**`database did not become reachable`.** Check `docker compose logs postgres`.
The healthcheck requires the `pgvector/pgvector:pg16` image to start cleanly.

**`pgvector is not installed`.** The compose image includes it. For an external
PostgreSQL, install the `vector` extension package for that server version.

**CORS failure.** Add the exact browser origin to `CORS_ORIGINS`; trailing
slashes and paths are rejected by design.

**401/403 are not implemented yet.** Authentication begins in M2; only system
endpoints exist today.
