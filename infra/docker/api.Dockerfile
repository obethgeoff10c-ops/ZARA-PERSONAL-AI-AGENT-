# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

WORKDIR /app/services/core

# Dependencies are installed before source is copied so edits do not invalidate
# the expensive layer.
FROM base AS deps

COPY services/core/pyproject.toml services/core/uv.lock services/core/README.md ./
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS runtime

COPY --from=deps /app/services/core/.venv /app/services/core/.venv
ENV PATH="/app/services/core/.venv/bin:$PATH"

COPY services/core/pyproject.toml services/core/uv.lock services/core/README.md ./
COPY services/core/alembic.ini ./
COPY services/core/alembic ./alembic
COPY services/core/scripts ./scripts
COPY services/core/zara ./zara

RUN uv sync --frozen --no-dev

COPY infra/docker/api-entrypoint.sh /usr/local/bin/api-entrypoint
RUN chmod +x /usr/local/bin/api-entrypoint \
    && useradd --create-home --uid 10001 zara \
    && chown -R zara:zara /app

USER zara

EXPOSE 8000

ENTRYPOINT ["api-entrypoint"]
CMD ["uvicorn", "zara.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
