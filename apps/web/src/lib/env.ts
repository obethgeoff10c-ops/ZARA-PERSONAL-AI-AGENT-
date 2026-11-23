const DEFAULT_API_URL = "http://localhost:8000";

function withoutTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export const env = {
  appName: "ZARA",
  apiInternalUrl: withoutTrailingSlash(
    process.env.API_INTERNAL_URL ?? DEFAULT_API_URL,
  ),
  publicApiUrl: withoutTrailingSlash(
    process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_API_URL,
  ),
} as const;
