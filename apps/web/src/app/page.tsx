import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SystemStatus } from "@/components/system-status";
import { env } from "@/lib/env";
import { getSystemSnapshot } from "@/lib/system";

export const dynamic = "force-dynamic";

function formatCheckedAt(value: string): string {
  return `${new Date(value).toISOString().slice(11, 19)} UTC`;
}

export default async function FoundationPage() {
  const snapshot = await getSystemSnapshot();
  const apiOnline = snapshot.api === "online";
  const dependencies = snapshot.dependencies;

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-12 md:py-20">
      <header className="max-w-3xl">
        <div className="flex flex-wrap items-center gap-3">
          <span
            aria-hidden="true"
            className="grid size-8 place-items-center rounded-md bg-foreground font-mono text-sm font-semibold text-background"
          >
            Z
          </span>
          <span className="font-mono text-sm tracking-[0.2em] text-muted-foreground">
            ZARA
          </span>
          <Badge variant="outline" className="font-mono text-xs">
            M0 · Foundation
          </Badge>
          {snapshot.environment ? (
            <span className="font-mono text-xs text-muted-foreground">
              env: {snapshot.environment}
            </span>
          ) : null}
        </div>

        <h1 className="mt-8 text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
          The foundation is{" "}
          <span
            className={
              apiOnline
                ? "text-emerald-600 dark:text-emerald-400"
                : "text-destructive"
            }
          >
            {apiOnline ? "running" : "offline"}
          </span>
          .
        </h1>
        <p className="mt-4 text-base leading-7 text-muted-foreground text-pretty">
          One orchestrator, one memory, one permission system. This screen only
          proves the scaffold: the API answers, the database and cache are
          reachable, and the browser consumes a typed contract generated from
          OpenAPI.
        </p>

        <div className="mt-7 flex flex-wrap items-center gap-3">
          <Button render={<a href={`${env.publicApiUrl}/docs`} />}>
            Open API docs
          </Button>
          <Button
            variant="outline"
            render={<a href={`${env.publicApiUrl}/ready`} />}
          >
            Readiness probe
          </Button>
          <span className="font-mono text-xs text-muted-foreground">
            checked {formatCheckedAt(snapshot.checkedAt)}
            {apiOnline && snapshot.version ? ` · v${snapshot.version}` : ""}
          </span>
        </div>
      </header>

      <section aria-labelledby="status-heading" className="mt-14">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h2 id="status-heading" className="text-lg font-medium">
              Live checks
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Each probe runs on request. Nothing here is cached or simulated.
            </p>
          </div>
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <SystemStatus
            label="FastAPI"
            description="Liveness + readiness"
            status={apiOnline ? "ok" : "error"}
            detail={apiOnline ? null : "API unreachable"}
          />
          {dependencies.map((dependency) => (
            <SystemStatus
              key={dependency.key}
              label={dependency.label}
              description="Readiness dependency"
              status={dependency.status}
              latencyMs={dependency.latencyMs}
              detail={dependency.detail}
            />
          ))}
          {dependencies.length === 0 ? (
            <SystemStatus
              label="Dependencies"
              description="Database and cache"
              status="unknown"
              detail="Start the API to run readiness checks"
            />
          ) : null}
        </div>
      </section>

      <section className="mt-14 grid gap-4 md:grid-cols-2">
        <Card className="shadow-none">
          <CardHeader>
            <CardTitle className="text-base">
              What this milestone includes
            </CardTitle>
            <CardDescription>
              Production-shaped plumbing, with no agent behaviour yet.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-2.5 text-sm text-muted-foreground">
              {[
                "Next.js App Router with strict TypeScript and shadcn/ui",
                "FastAPI with validated settings, JSON logs, and RFC 9457 errors",
                "SQLAlchemy 2 async sessions with tenant context hooks",
                "PostgreSQL 16, pgvector, and the Alembic baseline migration",
                "Redis readiness check, reserved for later rate limiting and SSE fan-out",
                "Docker Compose, GitHub Actions, pytest, and Vitest",
              ].map((item) => (
                <li key={item} className="flex gap-2.5">
                  <span
                    aria-hidden="true"
                    className="mt-2 size-1 shrink-0 rounded-full bg-foreground/40"
                  />
                  {item}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>

        <Card className="shadow-none">
          <CardHeader>
            <CardTitle className="text-base">
              Deliberately not here yet
            </CardTitle>
            <CardDescription>
              The specs stay authoritative as each milestone lands.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm leading-6 text-muted-foreground">
            <p>
              No fake agent, memory, integration, or workflow code. Those arrive
              in M1 onward against the contracts in{" "}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                docs/architecture
              </code>
              .
            </p>
            <p>
              Redis is provisioned and probed, but D-10 keeps queues in
              Temporal; Redis is not used for task processing in the MVP.
            </p>
          </CardContent>
        </Card>
      </section>

      <footer className="mt-auto pt-16">
        <p className="border-t pt-6 font-mono text-xs text-muted-foreground">
          ZARA · specs over guesses · no secrets in the repository
        </p>
      </footer>
    </main>
  );
}
