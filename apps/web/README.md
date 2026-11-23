# ZARA web

Next.js App Router shell for ZARA. See the repository [README](../../README.md)
and [development guide](../../docs/development.md).

```bash
npm run dev --workspace @zara/web
npm run lint --workspace @zara/web
npm run typecheck --workspace @zara/web
npm test --workspace @zara/web
npm run build --workspace @zara/web
```

Environment:

- `API_INTERNAL_URL` — server-side API origin (Compose: `http://api:8000`).
- `NEXT_PUBLIC_API_URL` — browser-visible API origin.

The foundation page is a server component that probes `/health` and `/ready` on
every request. It renders an offline state instead of crashing when the API is
unreachable.
