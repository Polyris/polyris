# Web Console (UI)

## Overview

The polyris Web Console is a React-based single-page application for monitoring and managing pipelines.

---

## Features

### Views

| View | Description |
|------|-------------|
| **Pipelines** | Main view with DAG visualization |
| **Assets** | Asset lineage and events |
| **Tasks** | All task instances across pipelines |
| **Runs** | Pipeline execution history |

### Pipeline View Modes

| Mode | Description |
|------|-------------|
| **🔀 DAG** | Interactive graph (React Flow) |
| **📊 Gantt** | Timeline of task execution |
| **📅 Calendar** | Historical executions by date |

---

## Components

### Sidebar Pipeline Cards

Each pipeline in the sidebar shows:
- **Status icon** — Color-coded (running/success/failed/idle)
- **Pipeline name** — Truncated with tooltip
- **Status + Schedule** — e.g. `failed · Mon @ 10:00 UTC`
- **Run sparkline** — Last 10 runs as colored mini-bars (green=success, red=failed, blue pulsing=running)

Schedule formatting:
| Expression | Display |
|------------|---------|
| `cron(0 8 * * ? *)` | `daily @ 08:00` |
| `cron(0 10 ? * MON *)` | `Mon @ 10:00` |
| `rate(6 hours)` | `every 6h` |
| Asset-triggered (no cron) | *(not shown)* |

> Schedule appears after pipelines are re-deployed so the registry gets the `schedule` field.

### DAGGraph (React Flow)

Interactive pipeline visualization:
- Drag to pan
- Scroll to zoom
- Click node to select task
- Color-coded by status
- 🔒 Lock/unlock button to enable node dragging

**Task Colors:**
| Status | Color |
|--------|-------|
| waiting | Gray |
| running | Blue (animated) |
| success | Green |
| failed | Red |
| skipped | Orange |
| stopped | Gray |
| waiting_paused | Yellow |

### GanttChart

Timeline visualization:
- Shows task start/end times
- Duration bars
- Parallel execution visible

### CalendarView

Historical view:
- Click date to view executions
- Status indicators per day
- Quick navigation

### AssetLineageFlow

Asset dependency graph:
- Producers → Assets → Consumers
- DAG triggers
- Search and filter
- Staleness indicators
- `wait_for` dependencies shown as consumer relationships

**Relationships shown:**
| Source | Relationship |
|--------|--------------|
| `outlets` | Task → Asset (producer) |
| `inlets` | Asset → Task (consumer) |
| `wait_for` | Asset → Task (consumer with freshness) |
| `schedule=[asset]` | Asset → DAG (trigger) |

**Filter checkboxes:**
| Option | Description |
|--------|-------------|
| **📦 Assets** | Always shown (base) |
| **🔧 Tasks** | Show producer/consumer tasks |
| **🚀 DAG Triggers** | Show triggered pipelines |

**Legend:**
- 🔧 Producer — Task that creates asset
- 📦 Asset — Data artifact
- 📥 Consumer — Task that uses asset
- 🚀 DAG Trigger — Pipeline triggered by asset

---

## Task Actions

### Task Detail Modal

Click any task to open the detail modal with:

**Details Tab:**
- **Duration** — Execution time
- **Status** — Current task status
- **Dependencies** — Number of upstream task dependencies
- **Execution Name** — Full execution identifier
- **Pipeline Execution** — Parent pipeline execution
- **Started/Finished** — Timestamps
- **Dependencies** — List of upstream tasks (if any)
- **Asset Dependencies** — Assets this task waits for via `wait_for` (with freshness constraint)
- **Trigger Rule** — When task triggers (if not `all_success`)
- **AWS Console** — Links to Step Functions executions

**History Tab:**
- Previous executions of this task
- Status, duration, timestamps

**Actions Tab:**
- Skip, Fail, Stop, Restart buttons

### From Task Modal

Click a task to open detail modal with actions:

| Action | Description |
|--------|-------------|
| **Skip** | Mark as skipped, notify dependents |
| **Fail** | Mark as failed, notify dependents |
| **Stop** | Force stop running execution |
| **Restart** | Retry failed task |

### Pipeline Actions

| Action | Description |
|--------|-------------|
| **⏸️ Pause** | Pause execution. Running tasks complete, new tasks wait. |
| **▶️ Resume** | Resume paused execution. |
| **⏰ +12h** | Extend pause timeout by 12 hours. |
| **⏹️ Stop** | Stop all tasks immediately. |

### From Slack

Interactive Slack notifications include buttons:
- **Skip** — Continue pipeline without this task
- **Fail** — Mark failed and continue
- **Restart** — Retry the task

---

## Backfill Modal

Run pipeline for date range:

### Options
- **Start Date** — First date to run
- **End Date** — Last date to run
- **Mode** — Tasks or Assets selection

### Task Mode
- Select specific tasks to run
- Unselected tasks are skipped
- Tasks with `skip_on_backfill=True` are unchecked by default (e.g. scrapers)
- User can override defaults by toggling any task on/off

### Asset Mode
- Select assets to materialize
- Pipeline runs tasks needed for those assets

### Auto Variables
When running backfill, these variables are auto-generated:

| Variable | Example |
|----------|---------|
| `current_date` | "2025-07-25" |
| `date_compact` | "20250725" |
| `year` | "2025" |
| `month` | "07" |
| `day` | "25" |
| `day_of_week` | "friday" |
| `previous_date` | "2025-07-24" |
| `is_backfill` | true |
| `ALLOW_UNSUCCESSFUL_SPIDER_RUN` | True |

### Failure Handling During Backfill
When a task fails during backfill, Slack and PagerDuty notifications are **suppressed** to avoid noise. The task enters `waiting_decision` status and waits up to 5 hours for a decision via the UI (skip, fail, or restart). If no action is taken, it auto-fails after timeout.

---

## Auto-Refresh

The UI uses polling for updates:

| State | Interval |
|-------|----------|
| Active (tasks running) | 3 seconds |
| Idle (no active tasks) | 30 seconds |

The UI automatically detects when tasks are running and increases polling frequency.

---

## Filtering

### History paging (Runs + Tasks)

Both halves of History load newest-first, one page at a time (ADR #113). The footer's
count reflects what is loaded and says whether that is everything: **`50+ runs`** while
the API still has older rows, **`73 runs`** once the feed is exhausted. **Show older
runs / tasks** loads the next page and appends it.

Two paginations sit in that footer and they are not the same thing: the `‹ ›` pager
walks the rows already loaded, ten at a time; **Show older** extends the feed itself.

How far back you can go depends on the filter: one pipeline reaches as far as the rows
still exist (bounded by their TTL), a date reaches that date, and no filter reaches the
last 14 days.

### Tasks View
- **Pipeline** — Filter by pipeline name
- **Status** — Filter by task status
- **Date** — Filter by execution date

### Runs View
Unified Run/Activity feed (ADR #95): a single table of pipeline executions
**and** Backfills. Backfill rows show partition progress in place of an
execution id, a `bl-status-pill` in the 6-state Backfill vocabulary
(pending/running/completed/failed/partial/canceled — `partial` shows the
`X/Y` ratio), and link to the backfill detail page. The dedicated Backfills
view remains for backfill-only browsing.
- **Pipeline** — Filter by pipeline name
- **Status** — Filter by run status; the dropdown groups Run statuses
  (succeeded/failed/running/aborted) and Backfill statuses separately
- **Date** — Filter by date (executions by logical date; backfills by
  partition range)

### Assets View
- **Search** — Filter by asset name
- **Group** — Filter by asset group
- **Staleness** — Fresh/Warning/Stale

---

## Help Modal

Press **?** or click Help to see:

### Icons Legend
All status icons explained

### API Reference
Quick reference for REST API endpoints

---

## Notifications Panel

Shows recent alerts:
- Failed tasks
- Pipeline failures
- Asset events

Click notification to navigate to related task/pipeline.

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `?` | Open help |
| `Esc` | Close modal |
| `r` | Refresh |

---

## Technical Stack

- **React 19** — UI framework
- **Next.js 16** — App Router, server-side API proxy
- **TypeScript** — Type safety
- **React Query 5** (`@tanstack/react-query`) — Server state, caching, refetch
- **Zustand 5** — UI state management (navigation, modals, filters)
- **ReactFlow 11** — DAG and asset lineage visualization
- **dagre** — Automatic graph layout
- **Tailwind CSS 3 + shadcn/ui** — Styling and UI primitives
- **CSS Modules** — Component-scoped styles, theme-aware variants
- **Vitest + Testing Library** — 536+ tests (unit + integration)

---

## State Management

Server data and UI state are deliberately separated:

| Layer | Tool | What it holds |
|-------|------|--------------|
| **Server state** | React Query 5 | Pipeline list, task details, metrics, assets. Caching, background refetch, optimistic updates. |
| **UI state** | Zustand 5 (`useAppStore`) | Selected pipeline, date, view mode, theme, modal visibility, filters (27 fields total). |
| **URL state** | `useStoreInit` | Bidirectional URL ↔ store sync (`?pipeline=x&date=2025-01-15&view=dag`). |

**Performance pattern:** All components use `useShallow` selectors to subscribe to only the specific store fields they need, preventing unnecessary re-renders when unrelated state changes.

React Query hooks live in `hooks/queries/` — `usePipelineQueries.ts` (pipelines, detail, executions, metrics, mutations), `useAssetQueries.ts` (assets, events, triggers, backfill), `useGlobalQueries.ts` (all-tasks, all-runs, consecutive progress).

---

## Component Architecture

```
App.tsx
├── PipelinesSidebar     — pipeline list, groups, search, sparklines, schedule
├── Header               — date picker, view mode tabs, theme toggle, modals
├── Notifications        — real-time notification panel
├── CommandPalette       — ⌘K fuzzy search across pipelines + actions
└── Main content (switches on mainView)
    ├── PipelineDetail   — DAG/Gantt/Calendar + execution selector
    │   ├── DAGGraphFlow         — ReactFlow + dagre layout, status coloring
    │   ├── GanttChart           — timeline bars with click-to-select
    │   ├── CalendarView         — monthly execution grid
    │   ├── TaskDetailModal/     — split into 6 sub-components:
    │   │   ├── TaskDetailModal  — tabs (Details/Dependencies/Events), actions
    │   │   ├── ConsecutiveProgress — consecutive asset date progress bars
    │   │   ├── DependencyStatusList — upstream/downstream with trigger rules
    │   │   ├── ErrorDisplay     — formatted error output
    │   │   ├── LiveDuration     — real-time duration for running tasks
    │   │   └── helpers.ts       — evaluateDepStatus, trigger rule logic
    │   └── BackfillModal        — date range + task/asset selection
    ├── AllTasksView     — cross-pipeline task table with filters
    ├── AllRunsView      — unified Run/Activity feed (executions + backfills) with filters
    └── AssetsView       — asset list + AssetLineageFlow graph
        └── AssetLineageFlow — ReactFlow graph with prefix filtering + search
```

`TaskDetailModal` and `BackfillModal` are **lazy-loaded** via `React.lazy()` + `Suspense` in PipelineDetail to reduce initial bundle (~50KB deferred).

### Modal System

All modals extend `BaseModal` which provides: focus trap (Tab/Shift+Tab), ESC to close, click-outside dismiss, body scroll lock, ARIA dialog role, and focus restoration on close.

---

## Accessibility

The console implements WCAG-aligned accessibility across all interactive components:

- **Modals**: `role="dialog"`, `aria-modal="true"`, `aria-labelledby`, focus trap, ESC close, focus restoration
- **Tabs** (TaskDetailModal, PipelineDetail view modes): `role="tablist"` / `role="tab"`, `aria-selected`, keyboard Enter/Space navigation
- **Sidebar**: `role="listbox"` / `role="option"`, `aria-selected`, keyboard Up/Down/Enter navigation
- **Status badges**: `aria-label="Task status: {status}"` for screen readers
- **Toasts**: `role="alert"`, `aria-live="polite"`
- **Sortable headers**: `role="columnheader"`, `aria-sort`, keyboard Enter/Space
- **Loading skeletons**: `role="status"`, `aria-label="Loading..."`, `sr-only` text
- **CountdownTimer**: `role="timer"`, `aria-live="polite"`, dynamic `aria-label`
- **Error boundary**: `role="alert"` on error display
- **Group headers** (sidebar, assets): `role="button"`, `aria-expanded`, keyboard support

---

## Testing

```bash
npm test -- --run                   # All 536+ tests
npm test -- --run src/components/DAGGraphFlow.test.tsx  # Single file
```

**Coverage:** 35 test files covering all major components, hooks, stores, and queries.

**Test infrastructure (`src/test/`):**

- `factories.ts` — builder functions: `createTask()`, `createDAG()`, `createPipeline()`, `createAsset()`, `createExecution()`, etc. All accept override objects.
- `mocks.tsx` — shared mocks for icons (Proxy-based), BaseModal (renders children directly), shadcn/ui Button, config, API. Import with `import '../test/mocks'`.
- `setup.ts` — global setup: localStorage mock, fetch mock, jsdom polyfills.

**Mocking patterns:**

- **ReactFlow** (DAGGraphFlow, AssetLineageFlow): mock `reactflow` module to render nodes as `<div data-testid="rf-node-{id}">`, mock `dagre` for layout.
- **Modals**: mock `BaseModal` to render children directly (no portal/animation).
- **Icons**: Proxy mock returns `<span data-testid="icon-{name}" />` for any imported icon.
- **API**: mock `utils/api` with `vi.fn().mockResolvedValue()`.

---

## Configuration

The UI loads config at runtime from `/config.js` (which wins over baked
`NEXT_PUBLIC_*` — ADR #94). `API_URL` is the full API Gateway invoke URL,
**including the stage and the trailing `/api`** — route paths in the app are
appended without `/api` (so the base must end in `/api`):

```javascript
window.CONFIG = {
  API_URL: "https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/dev/api"
};
```

On a real deploy `ui/deploy.sh` writes this automatically as the `ConsoleApiUrl`
CloudFormation output + `/api`.

---

## Deployment

- **Deployment** — S3 + CloudFront via `./ui/deploy.sh`
- **Option B** — CloudFront + S3 (all in AWS)

---

## Development

### Local UI against a deployed API

The console is a **static export** — there is no Next.js server and no
server-side proxy, so the browser calls the deployed API Gateway **directly**.
This works from `localhost` out of the box: the API's CORS is wide open
(`Access-Control-Allow-Origin: *`, all methods, `Authorization` allowed), and it
answers `OPTIONS` preflight, so GET/POST/PUT/DELETE all succeed cross-origin.

```bash
cd ui
npm run dev            # http://localhost:3000
```

For **local development, use `ui/.env.local`** (below). Leave `ui/public/config.js`
alone — its default empty `API_URL` falls through to your `.env.local`.
`config.js` is the *runtime* file `ui/deploy.sh` generates for the **deployed**
site; a non-empty `API_URL` there wins over `.env.local`, so for local work just
use `.env.local`.

**Without auth** (API deployed with `AUTH_ENABLED=false`):
```env
# Full invoke URL: <id>.execute-api.<region>.amazonaws.com/<stage>/api
NEXT_PUBLIC_API_URL=https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/dev/api
NEXT_PUBLIC_AUTH_ENABLED=false
```

**With Cognito** (API deployed with `AUTH_ENABLED=true`): add the pool/client the
`console-api` Lambda validates against. Amplify signs in with email/password
directly — there are **no OAuth callback URLs to register** for localhost.
```env
NEXT_PUBLIC_API_URL=https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/dev/api
NEXT_PUBLIC_AUTH_ENABLED=true
NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX
NEXT_PUBLIC_COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
NEXT_PUBLIC_COGNITO_REGION=us-east-1
```

> Common gotcha: `NEXT_PUBLIC_API_URL` must be the **full** URL with **both** the
> stage and `/api` (`…/dev/api`). Just the host, or just `/api`, or the stage
> without `/api`, all 404. The variable is `NEXT_PUBLIC_API_URL` — not
> `API_GATEWAY_URL` (that was the old Route-Handler proxy, removed with the static
> export).

> **`Cannot connect to API: UNAUTHORIZED` (401)?** The local UI isn't sending a
> token the deployed API accepts. `NEXT_PUBLIC_AUTH_ENABLED` only controls whether
> the **UI sends** a token — the API enforces based on its own `AUTH_ENABLED` (set
> at deploy), so a 401 means the API has auth **on** while the UI isn't
> authenticating. Work through this checklist:
>
> 1. **Turn auth on in the UI** — `NEXT_PUBLIC_AUTH_ENABLED=true` in `ui/.env.local`.
>    (`ui/public/config.js` ships with `AUTH.enabled: null` precisely so this env
>    var wins locally; if you ever set `enabled: false` there it silently overrides
>    the env var — `config.ts` resolves `enabled` with `??`.)
> 2. **Match the API's Cognito pool/client** — set `NEXT_PUBLIC_COGNITO_USER_POOL_ID`
>    and `NEXT_PUBLIC_COGNITO_CLIENT_ID` to **exactly** what the `console-api` Lambda
>    has (`COGNITO_USER_POOL_ID` / `COGNITO_CLIENT_ID` env vars — the Lambda is the
>    source of truth). A token from a *different* pool is rejected → 401. If the
>    deployed `/config.js` shows a different pool than the Lambda, that's deploy
>    drift — rerun `ui/deploy.sh`.
> 3. **Clear stale caches, then restart** — `NEXT_PUBLIC_*` are baked at startup, so
>    editing `.env` needs a `npm run dev` restart (and `rm -rf ui/.next` if a value
>    still looks stale). Crucially, `/config.js` is loaded by a plain
>    `<script src="/config.js">` with no cache-busting, so the **browser caches it**
>    — after editing `ui/public/config.js` you must hard-reload with cache disabled
>    (DevTools → Network → "Disable cache" → reload) or use a fresh private window,
>    or the old `AUTH.enabled` keeps winning. Quick check: run `window.CONFIG` in the
>    DevTools console and confirm `AUTH.enabled` is what you expect.
> 4. **Sign out, then sign in** — a session cached from a previous/different pool
>    survives a restart and keeps sending the wrong token. (A fresh private window
>    covers this and the config.js cache at once — no cached session, no extensions.)
>
> Or, if you want token-free local access, redeploy the API with `AUTH_ENABLED=false`
> and set `NEXT_PUBLIC_AUTH_ENABLED=false`.

---

## Browser Support

- Chrome (latest)
- Firefox (latest)
- Safari (latest)
- Edge (latest)

---

## Screenshots

### DAG View
```
┌─────────────────────────────────────────────────────────┐
│  📋 acme-daily              🔀 DAG  📊 Gantt  📅 Cal │
├─────────────────────────────────────────────────────────┤
│                                                         │
│    ┌─────────┐     ┌─────────┐     ┌─────────┐         │
│    │ scrape  │────▶│ process │────▶│  merge  │         │
│    │   ✓     │     │   ●     │     │   ○     │         │
│    └─────────┘     └─────────┘     └─────────┘         │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Asset Lineage
```
┌─────────────────────────────────────────────────────────┐
│  📦 Assets                        🔍 Search...          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│    🔧 scrape ────▶ 📦 raw ────▶ 📥 process             │
│                                                         │
│    🔧 process ───▶ 📦 processed ───▶ 🚀 feeds-dag      │
│                                                         │
└─────────────────────────────────────────────────────────┘
```
