/**
 * Typed client generated from the FastAPI OpenAPI contract.
 *
 * Regenerate with `npm run api:types` at the repository root after changing an
 * API schema. Never hand-edit `schema.d.ts`.
 */
import type { components } from "./schema";

export type HealthResponse = components["schemas"]["HealthResponse"];
export type ReadinessResponse = components["schemas"]["ReadinessResponse"];
export type CheckResult = components["schemas"]["CheckResult"];
export type ProblemDetail = components["schemas"]["ProblemDetail"];
export type FieldError = components["schemas"]["FieldError"];

export type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

export interface RequestOptions {
  /** Statuses treated as success. Readiness returns 503 with a valid body. */
  acceptStatuses?: readonly number[];
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export interface ApiClient {
  health(options?: RequestOptions): Promise<HealthResponse>;
  readiness(options?: RequestOptions): Promise<ReadinessResponse>;
}

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetail | null;

  constructor(status: number, problem: ProblemDetail | null) {
    super(problem?.detail ?? `API request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }
}

function isProblem(value: unknown): value is ProblemDetail {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    "status" in value &&
    typeof (value as { code: unknown }).code === "string"
  );
}

export function createApiClient(
  baseUrl: string,
  fetchImpl: FetchLike = (input, init) => fetch(input, init),
): ApiClient {
  const root = baseUrl.replace(/\/+$/, "");

  async function request<T>(
    path: string,
    options: RequestOptions = {},
  ): Promise<T> {
    const acceptStatuses = options.acceptStatuses ?? [200];
    const response = await fetchImpl(`${root}${path}`, {
      method: "GET",
      headers: { Accept: "application/json", ...options.headers },
      signal: options.signal,
      cache: "no-store",
    });

    const contentType = response.headers.get("content-type") ?? "";
    const body: unknown = contentType.includes("json")
      ? await response.json()
      : null;

    if (!acceptStatuses.includes(response.status)) {
      throw new ApiError(response.status, isProblem(body) ? body : null);
    }

    return body as T;
  }

  return {
    health: (options) => request<HealthResponse>("/health", options),
    readiness: (options) =>
      request<ReadinessResponse>("/ready", {
        ...options,
        acceptStatuses: [200, 503],
      }),
  };
}
