# 08 — Frontend Specification

Stack (D-13): Next.js App Router, TypeScript (strict), Tailwind, shadcn/ui, TanStack Query, Zustand (UI-only), `openapi-typescript` client, Playwright, Vitest + Testing Library.
Principle: **thin client**. No business logic or authorization decisions in the browser or Next route handlers; the BFF only handles session cookies, SSE proxying, and signed-URL redirects.

## 1. Application routing

```
app/
├── (auth)/sign-in, sign-up                          # IdP components (Clerk)
├── (onboarding)/onboarding/[step]                   # profile → connect Google → connect GitHub → autonomy → notification prefs → backfill window
├── (app)/layout.tsx                                 # shell: sidebar, command palette (⌘K), notification bell, global kill-switch toggle
│   ├── page.tsx                                     # Home / Daily Brief
│   ├── chat/                                        # new chat
│   │   └── [conversationId]/page.tsx
│   ├── approvals/                                   # inbox
│   │   └── [approvalId]/page.tsx
│   ├── activity/                                    # timeline + audit
│   │   └── runs/[runId]/page.tsx                    # run detail (plan, steps, tools, cost, trace link)
│   ├── memory/                                      # browse / suggestions / retrieval explain
│   │   ├── [memoryId]/page.tsx
│   │   └── suggestions/page.tsx
│   ├── entities/                                    # people, companies, repos…
│   │   └── [entityId]/page.tsx                      # cross-domain relationship view
│   ├── tasks/page.tsx
│   ├── goals/ , goals/[goalId]
│   ├── projects/ , projects/[projectId]
│   ├── sales/ (pipeline, leads, opportunities)      # MVP minimal tables
│   ├── dev/ (repos, PRs, CI)                        # MVP minimal
│   ├── automations/                                 # detectors, autonomy levels, schedules, standing rules
│   ├── integrations/                                # list + [provider]/setup
│   └── settings/ (profile, notifications, privacy, data-export, security, billing)
├── (admin)/admin/... (internal; role=admin only)    # runs, DLQ, flags, cost
└── api/                                             # BFF only: /api/session, /api/stream/[...path], /api/auth/callback
```

Route guards: Next middleware checks session; server components call FastAPI with the user's token. Unauthorized → `/sign-in`. Role guard for `(admin)`.

## 2. Page hierarchy & key content

| Page         | Purpose                 | Key components                                                                                                                       |
| ------------ | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Home         | Daily brief + attention | `BriefingCard`, `TodayTimeline`, `AttentionStrip` (approvals, overdue, suggested memories, integration alerts), `InsightList`        |
| Chat         | Unified conversation    | `ConversationList`, `MessageList`, `Composer`, `RunPanel` (collapsible: route, plan, tools), `CitationChip`, `ApprovalCard` (inline) |
| Approvals    | Decision inbox          | `ApprovalList`, `ApprovalDetail` (preview, flags, edit, approve/reject), `StandingRuleDialog`                                        |
| Activity     | What ZARA did           | `ActivityTimeline` (filters), `RunDetail` (Plan, Steps, Tool calls, Cost, Trace link), `AuditTable`                                  |
| Memory       | Inspect/edit memory     | `MemoryTable` (search/filter), `MemoryDrawer` (content, sources, revisions, "why used"), `SuggestionInbox`, `BulkDeleteDialog`       |
| Entities     | People/companies/repos  | `EntityCard`, `RelationshipGraph` (1-hop mini graph; no heavy graph libs in MVP—list + simple SVG)                                   |
| Automations  | Control proactivity     | `DetectorToggleList`, `AutonomyMatrix` (domain × action), `QuietHoursEditor`, `ScheduleList`, `StandingRules`                        |
| Integrations | Connect and monitor     | `IntegrationCard` (status, scopes, last sync), `ConnectWizard`, `ScopeDiff`, `SyncProgress`, `DisconnectDialog`                      |
| Settings     | Account                 | profile, notification channels, data export/delete, sessions                                                                         |

## 3. Component architecture

- **Layers:** `components/ui` (shadcn primitives) → `components/domain` (feature components: Approval*, Memory*, Run*) → `features/<name>` (page-level containers with data hooks) → `app/` routes (thin).
- **Data access:** one generated typed client (`@zara/api-client`); hooks in `features/*/queries.ts` (`useApprovals`, `useMemory`, …) using TanStack Query with query-key factories; mutations with optimistic updates only for low-risk toggles (pin, read, feedback). **No optimistic UI for approvals or deletions** (wait for server).
- **Forms:** react-hook-form + zod schemas generated from OpenAPI where possible.
- **Design system:** tokens in Tailwind config (color, spacing, radius, typography); light/dark; premium look: restrained palette, strong typography hierarchy, consistent 8-pt spacing, subtle motion (150–200 ms), no decorative clutter; domain color accents (Personal, Executive, Sales, Developer) used only for badges.
- **Accessibility:** WCAG 2.1 AA, keyboard-complete flows (approve/reject via keyboard), focus management in dialogs, `aria-live` for streaming content and new approvals, reduced-motion support.
- **Content safety rendering:** all model/third-party text rendered as text/markdown via a sanitizer (no raw HTML); **images and links from untrusted content are not auto-loaded** (links shown as `link text (domain)` with external-link confirmation; images replaced with placeholder + "load image" — mitigates markdown exfiltration).
- **Error boundaries** per route segment; global toast for problem+json errors with `request_id` copy button.

## 4. State management

| State                                              | Where                                                                                                    |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Server data (conversations, memory, approvals…)    | TanStack Query cache; invalidated by SSE `notification` stream events                                    |
| Live run/stream state                              | `useRunStream(runId)` reducer hook (not global store); persisted in memory only                          |
| UI state (sidebar, theme, drawers, composer draft) | Zustand with `persist` for draft/theme only (drafts per conversation in `localStorage`, cleared on send) |
| URL state (filters, tabs, selected item)           | search params (`nuqs`) so views are shareable/restorable                                                 |
| Auth/session                                       | Server cookies; client gets minimal `me` from query                                                      |

## 5. Streaming chat

- Transport: `fetch('/api/stream/chat', {method:'POST', body, headers:{Accept:'text/event-stream', 'Idempotency-Key': clientMessageId}})`, parsed by a small SSE parser over `ReadableStream` (supports POST + auth headers). Reconnect on drop: `GET /runs/{id}/events` with `Last-Event-ID` (exponential backoff 0.5→8 s, give up after 60 s → show "Reconnect" button; run continues server-side).
- Reducer handles event types from 07 §2: appends `token` deltas to the current assistant block (rAF-batched to ≤30 fps), attaches `citation` chips, updates `RunPanel` on `route.decided`, `plan.*`, `tool.*`.
- Optimistic user message with `client_message_id`; replaced by server message on `run.started`.
- Stop button → `POST /runs/{id}/cancel`.
- If `run.promoted`, UI shows "Working in the background — you can leave this page" and subscribes to `/notifications/stream`.
- Long tool phases show a progress chip ("Searching Gmail… 3 s").
- Scroll anchoring: auto-follow only if user is within 80 px of bottom.
- Citation chips open a side drawer with the memory/document snippet (untrusted content rendered in a sandboxed text view).

## 6. Real-time activity

- One multiplexed SSE connection per tab: `GET /notifications/stream` (BFF proxies, keeps cookie auth). Events: `notification.created`, `approval.requested`, `insight.created`, `integration.status`, `run.status`.
- Handlers invalidate specific query keys and update the unread badge; `approval.requested` triggers a persistent banner + browser tab title indicator.
- Activity page live-tails runs when "Live" toggle is on (polling fallback 10 s if SSE unavailable).
- Multi-tab dedupe via `BroadcastChannel` (only the leader tab opens the SSE; others receive via channel).

## 7. Approval UX

- **Where:** inline in chat (`ApprovalCard`), Approvals inbox, notification deep-link, email link (opens dashboard page; never acts on GET).
- **Card content:** (1) plain-language action title ("Send email to priya@acme.com"), (2) full rendered preview (recipients, subject, full body; calendar attendees/time; PR comment text), (3) **flags** — red/amber chips for: "Triggered by external content", "Recipient found only in an email you received", "New recipient domain", "Unusual volume"; (4) "Why ZARA wants to do this" (goal + linked citations), (5) expiry countdown, (6) risk tier badge.
- **Actions:** Approve (primary), Edit & approve (inline editable fields declared in `editable_fields`), Reject (with optional reason), "Always allow like this…" (creates standing rule; only for tier ≤3 and only when run not flagged tainted; requires confirm dialog describing scope/limits/expiry).
- **Safety UX:** Approve button disabled until preview fully rendered; tier 4 requires typing the action verb or step-up re-auth; approving sends `args_hash` shown in details (copyable); on 409 hash mismatch show "This action changed — review again".
- **Keyboard:** `A` approve, `R` reject, `E` edit, `J/K` navigate list (disabled while typing).
- After decision: shows result state (executing → done/failed) via run events.

## 8. Memory editing

- Table columns: content (truncate), kind badge, domain badges, importance/confidence meters, source count, updated, pinned. Filters: kind, status, domain, entity, source type, date, pinned/suggested.
- Drawer: editable content (textarea + preview), kind-specific payload form (e.g., commitment due date, counterparty picker), importance slider, expiry picker, pin toggle, **Sources** (quotes with links), **Revision history** (diff), **Used in** (recent runs where retrieved), **Delete** (with "don't remember this again" default on).
- Suggestions inbox: card per suggestion with evidence quote and Confirm / Edit / Reject; bulk actions.
- Edits require explicit Save (no autosave); show "You edited this — ZARA won't overwrite it" note.
- Bulk delete flows require step-up and show counts before confirm.
- Empty/error/loading skeletons for each state.

## 9. Integration setup

- `ConnectWizard` steps: (1) What ZARA will do/not do (plain-language scope explanations, "read-only"/"drafts only" labels), (2) Choose scope level (Gmail: read + drafts), data window (30/90/180 days), labels/folders/repos selection, (3) Redirect to provider OAuth (`POST /integrations/{p}/connect` → `authorization_url`), (4) Callback returns to progress screen showing initial sync (progress via `/integrations` polling or SSE `integration.status`), (5) Summary and test ("Ask ZARA about your next meeting").
- Statuses with explicit UI: `pending`, `active`, `syncing`, `needs_reauth` (prominent Reconnect CTA + banner), `paused`, `error` (with last_error, "Retry"), `revoked`.
- Google: explain verification/“unverified app” screens during beta if applicable (copy block).
- GitHub: install GitHub App → select repos → return.
- Disconnect dialog: choose retain/archive/purge derived data; requires step-up.
- Never show tokens or raw provider payloads.

## 10. Responsive design

- Breakpoints: `sm 640, md 768, lg 1024, xl 1280`. Mobile-first.
- <md: bottom tab bar (Home, Chat, Approvals(badge), Activity, More); sidebar collapses; chat composer fixed with safe-area insets; Approval cards full-screen sheets; tables → stacked cards.
- md–lg: collapsible icon sidebar; drawers as side sheets.
- ≥lg: three-pane chat (conversations | thread | run panel/context).
- Touch targets ≥44 px; no hover-only affordances; PWA manifest + web push groundwork (post-MVP).
- Performance budgets: LCP < 2.5 s (4G), JS per route < 180 KB gzipped (excluding shared), streaming first token render < 100 ms after event.

## 11. Frontend testing

- Component tests (Vitest/RTL) for ApprovalCard (flags, disabled states), MemoryDrawer, SSE reducer (event ordering, reconnect, duplicate seq), sanitizer (XSS corpus), ConnectWizard states.
- Playwright E2E: sign-in → connect (mock IdP/provider _in test env only_) → chat stream → approval flow → memory edit/delete → kill switch → responsive viewport suites (Pixel 7, iPhone 14, desktop).
- Visual regression on key screens (Playwright screenshots), axe-core accessibility checks in CI (0 serious/critical violations).

## 12. Acceptance criteria — Frontend

1. All screens in §2 implemented with loading/empty/error states; Lighthouse accessibility ≥ 95 on Home, Chat, Approvals, Memory.
2. Streaming: tokens appear incrementally; connection drop mid-stream recovers without duplicated text (E2E with forced disconnect).
3. Approval UX: cannot approve without preview; hash-mismatch path tested; tier-4 step-up enforced; keyboard operable.
4. Untrusted content XSS/markdown-exfil corpus (≥50 payloads) renders inert (no network requests to attacker hosts — verified via Playwright request interception).
5. Memory edits persist and show revision history; deleted memory disappears from list and search immediately after server confirm.
6. Mobile layouts verified at 360×640, 390×844, 768×1024, 1440×900 with no horizontal scroll.
7. No secrets/tokens in client bundles or storage (static scan + runtime check).
