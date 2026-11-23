import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

import type { DependencyStatus } from "@/lib/system";

const STATUS_STYLES: Record<
  DependencyStatus,
  { label: string; dot: string; badge: string }
> = {
  ok: {
    label: "Operational",
    dot: "bg-emerald-500",
    badge:
      "border-emerald-600/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  },
  error: {
    label: "Unavailable",
    dot: "bg-destructive",
    badge: "border-destructive/20 bg-destructive/10 text-destructive",
  },
  unknown: {
    label: "Unknown",
    dot: "bg-muted-foreground",
    badge: "border-border bg-muted text-muted-foreground",
  },
};

export interface SystemStatusProps {
  label: string;
  status: DependencyStatus;
  latencyMs?: number | null;
  detail?: string | null;
  description?: string;
}

export function SystemStatus({
  label,
  status,
  latencyMs = null,
  detail = null,
  description,
}: SystemStatusProps) {
  const presentation = STATUS_STYLES[status];

  return (
    <Card className="shadow-none">
      <CardHeader className="gap-2">
        <div className="flex items-start justify-between gap-3">
          <CardTitle className="text-base font-medium">{label}</CardTitle>
          <Badge
            variant="outline"
            className={cn("gap-1.5 font-medium", presentation.badge)}
          >
            <span
              aria-hidden="true"
              className={cn("size-1.5 rounded-full", presentation.dot)}
            />
            {presentation.label}
          </Badge>
        </div>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        <dl className="flex items-center justify-between gap-3">
          <dt>Latency</dt>
          <dd className="font-mono tabular-nums text-foreground">
            {latencyMs === null ? "—" : `${latencyMs.toFixed(1)} ms`}
          </dd>
        </dl>
        {detail ? (
          <p className="mt-2 border-t pt-2 font-mono text-xs">{detail}</p>
        ) : null}
      </CardContent>
    </Card>
  );
}
