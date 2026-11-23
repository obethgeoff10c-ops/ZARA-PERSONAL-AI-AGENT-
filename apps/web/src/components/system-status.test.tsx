import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SystemStatus } from "./system-status";

describe("SystemStatus", () => {
  it("renders an operational dependency with latency", () => {
    render(
      <SystemStatus
        label="PostgreSQL + pgvector"
        status="ok"
        latencyMs={4.26}
      />,
    );

    expect(screen.getByText("PostgreSQL + pgvector")).toBeInTheDocument();
    expect(screen.getByText("Operational")).toBeInTheDocument();
    expect(screen.getByText("4.3 ms")).toBeInTheDocument();
  });

  it("renders an unavailable dependency with its reason", () => {
    render(
      <SystemStatus
        label="Redis"
        status="error"
        latencyMs={2000}
        detail="ConnectionError"
      />,
    );

    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("ConnectionError")).toBeInTheDocument();
  });

  it("does not invent latency when a check was not run", () => {
    render(<SystemStatus label="Dependency checks" status="unknown" />);

    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
