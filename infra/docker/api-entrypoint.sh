#!/bin/sh
# Wait for PostgreSQL, apply migrations, then start the given command.
set -eu

if [ "${ZARA_AUTO_MIGRATE:-true}" = "true" ]; then
  echo "waiting for postgres..."
  python - <<'PY'
import os
import socket
import sys
import time
from urllib.parse import urlsplit

raw = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
parts = urlsplit(raw)
host = parts.hostname or "postgres"
port = parts.port or 5432
deadline = time.monotonic() + 60

while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        time.sleep(1)
else:
    sys.exit(f"postgres at {host}:{port} did not become reachable within 60s")
PY

  echo "applying migrations..."
  alembic upgrade head
fi

exec "$@"
