import { createApiClient, type ReadinessResponse } from "@zara/api-client";

import { env } from "@/lib/env";

export type DependencyStatus = "ok" | "error" | "unknown";

export interface Dependency {
  key: string;
  label: string;
  status: DependencyStatus;
  latencyMs: number | null;
  detail: string | null;
}

export interface SystemSnapshot {
  api: "online" | "offline";
  version: string | null;
  environment: string | null;
  checkedAt: string;
  dependencies: Dependency[];
}

const DEPENDENCY_LABELS: Record<string, string> = {
  database: "PostgreSQL + pgvector",
  redis: "Redis",
};

const client = createApiClient(env.apiInternalUrl);
const PROBE_TIMEOUT_MS = 3_000;

function toDependencies(readiness: ReadinessResponse): Dependency[] {
  return Object.entries(readiness.checks).map(([key, check]) => ({
    key,
    label: DEPENDENCY_LABELS[key] ?? key,
    status: check.status,
    latencyMs: check.latency_ms,
    detail: check.detail ?? null,
  }));
}

export async function getSystemSnapshot(): Promise<SystemSnapshot> {
  const checkedAt = new Date().toISOString();
  const signal = AbortSignal.timeout(PROBE_TIMEOUT_MS);

  const [health, readiness] = await Promise.allSettled([
    client.health({ signal }),
    client.readiness({ signal }),
  ]);

  if (health.status === "rejected") {
    return {
      api: "offline",
      version: null,
      environment: null,
      checkedAt,
      dependencies: [],
    };
  }

  return {
    api: "online",
    version: health.value.version,
    environment: health.value.environment,
    checkedAt,
    dependencies:
      readiness.status === "fulfilled"
        ? toDependencies(readiness.value)
        : [
            {
              key: "readiness",
              label: "Dependency checks",
              status: "unknown",
              latencyMs: null,
              detail: "Readiness probe did not complete",
            },
          ],
  };
}
