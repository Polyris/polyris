# Web Console (UI)

## Contents

- [Overview](#overview)
- [Features](#features)
- [Components](#components) — layout, sidebar, DAG viewer, task modal
- [Task Actions](#task-actions)
- [Auto-Refresh](#auto-refresh)
- [Filtering](#filtering)
- [Help Modal](#help-modal)
- [Notifications Panel](#notifications-panel)
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [Technical Stack](#technical-stack)
- [State Management](#state-management)
- [Component Architecture](#component-architecture)
- [Accessibility](#accessibility)
- [Testing](#testing)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Development](#development)
- [Browser Support](#browser-support)
- [Screenshots](#screenshots)

## Overview

The polyris Web Console is a React-based single-page application for monitoring and managing pipelines.

---

## Features

### Views

| View | Description |
|------|-------------|
| **Pipelines** | Main view with DAG visualization |
| **History** | Runs and tasks across pipelines, with a **Runs** / **Tasks** toggle |

---

## Components

### Pipelines sidebar

The sidebar lists every registered pipeline, one line each. It reads the runs of the last 14 days:
- **Status icon** — the latest run's status (success, failed, running, aborted). A running run that waits for an operator shows a question mark (a decision) or a pause sign. A pipeline with no run in those 14 days shows a small hollow circle. A run is running until every task has finished; its name and hover say how many tasks failed. Only runs started on 1.3 count, so after the upgrade a pipeline's status appears with its first run.
- **Pipeline name** — truncated; hover for the full name and schedule
- **Run sparkline** — up to the last 10 runs, oldest on the left, coloured by status; empty slots are a faint track, so the newest run is always the rightmost bar. Hover it, or read the row with a screen reader, for the runs spelled out
- **Schedule** — see the table below; hidden on tablets, where the sidebar is narrow

The row of the open pipeline is highlighted; when the pipeline is listed twice, under **Needs attention** and in its group, only the first row you can see is.

**Needs attention.** Pipelines whose latest run failed, waits for your decision, or is still running with a failed task are also listed on top. A run that is running without a failed task or a decision to wait for, a paused run or an aborted run does not put a pipeline there: a stop is not a failure (ADR #112). Click the header to fold the section; its count stays.

**Groups.** Set `group=` on the `DAG` to list a pipeline under that group ([DSL reference](../features/DSL.md#dag-parameters)); groups are compared without case. Pipelines without a group go under **Ungrouped**, at the bottom. When no pipeline sets a group, the list is flat. Click a group to fold it, or use the button next to the count to fold or unfold all of them; the console remembers folded groups in this browser. A folded group still shows how many of its pipelines need attention, and is highlighted when it holds the open pipeline. A group you add shows after the pipeline's next deploy or run.

**Search.** Type to filter by pipeline name or group; every word must match. While you search, the sidebar shows the matches only, in their groups, all open. Enter opens the first match. The command palette (`⌘K`) matches pipeline names the same way.

While the pointer or the keyboard focus is in the list, a refresh updates statuses, sparklines and a folded group's count in place. It does not move or add rows, so a row does not shift under your click; only a pipeline that is gone leaves at once. The rows follow once the pointer and the focus have left the list; opening a pipeline from the keyboard counts as leaving it. When a refresh fails, the list stays and a line above it says from when its statuses are. When some runs could not be read, the line says a status may be out of date; after an upgrade, it says while the index of runs is still being built. Why a refresh fails is said by the **System** status in the header: amber **no connection** or **API error**, with the reason in its panel.

Schedule formatting:
| Expression | Display |
|------------|---------|
| `cron(0 8 * * ? *)` | `daily @ 08:00` |
| `cron(0 10 ? * MON *)` | `Mon @ 10:00` |
| `rate(6 hours)` | `every 6h` |
| `@daily`, `@hourly`, … (presets) | `daily`, `hourly`, … |
| Anything else | as written, cut to fit the row |
| `schedule=[asset]` | `on` and the asset, e.g. `on raw_orders`; with several, `on 3 assets` (hover for them, e.g. `on (a & b) \| c`) |
| No schedule | `manual` |

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

## Task Actions

### Task Detail Modal

Click any task to open the detail modal with:

**Details Tab:**
- **Duration** — Execution time
- **Status** — Current task status
- **Dependencies** — Number of upstream task dependencies
- **Why it waits** — For a waiting task: the reasons the engine recorded and since when, the deadline of its wait or decision, when its `wait_before` countdown ends, and what its trigger rule makes of its upstreams
- **Attempt** — Two chips when retries have occurred: ↻ N (manual Restarts) and ⭮ N/M (in-SFN auto-retry index / max retries). Hidden when `restarts=0` and `retry_attempt=0`.
- **Execution Name** — Full execution identifier
- **Child Pipeline** — For a task that runs another pipeline, a link to it
- **Pipeline Execution** — Parent pipeline execution
- **Started/Finished** — Timestamps
- **Asset Dependencies** — Assets this task waits for via `wait_for` (with freshness constraint)
- **Trigger Rule** — When task triggers (if not `all_success`)
- **AWS Console** — Links to the task's `run_task` execution, and to the task's own execution when it is a Step Functions task
- **Task Type** — The kind of task (`sfn`, `lambda`, `glue`, …)

**History Tab:**
- The task's timeline: its dependency decisions, starts, automatic retries, the end and any operator's decision, each with its time

**Input / Output Tab:**
- The variables and upstream outputs the task read, and its stored output

**Actions Tab:**
- Skip, Fail, Mark Successful, Stop, Restart buttons

### From Task Modal

Click a task to open detail modal with actions:

| Action | Description |
|--------|-------------|
| **Skip** | Mark as skipped: its dependants run as after a success |
| **Fail** | Mark as failed: dependants that need its success end `upstream_failed`, and the rest of the run goes on |
| **Mark Successful** | Mark as successful and record the facts of its outlets: its dependants start |
| **Stop** | Stop an unfinished task and its work: it ends `stopped`, which its dependants' rules count as a failure |
| **Restart** | Run a finished or undecided task again; tick *Also rerun the dependants its failure or stop decided* to rerun them too |

### Pipeline Actions

| Action | Description |
|--------|-------------|
| **⏸️ Pause** | Pause the run: running tasks complete, no task starts. A pause lasts until the Resume. |
| **▶️ Resume** | Resume the paused run: the tasks it held start. |
| **⏹️ Stop** | Stop the run: its unfinished tasks stop, dependants first, within seconds. |

The page reads a pause and a Stop from the run itself: it offers Resume as soon as the run is paused, and while a Stop is in progress it says so and offers no action on the run.

---

## Auto-Refresh

The UI polls for updates while the **Auto** toggle in the header is on; click it to pause polling.

| State | Interval |
|-------|----------|
| A task of the open run counts down its `wait_before` | 15 seconds |
| Otherwise | 30 seconds |

---

## Filtering

### History paging (Runs + Tasks)

Both halves of History load newest-first, one page at a time (ADR #113). The footer's
count reflects what is loaded and says whether that is everything: **`50+ runs`** while
the API still has older rows, **`73 runs`** once the feed is exhausted. **Show older
runs / tasks** loads the next page and appends it.

Two paginations sit in that footer and they are not the same thing: the `‹ ›` pager
walks the rows already loaded, ten at a time; **Show older** extends the feed itself.

How far back you can go depends on the filter. Without a date, **Runs** and **Tasks** reach
the runs started in the last 90 days, with a pipeline chosen or not; only runs started on 1.3
are there. A date reaches that date in both, runs started before the upgrade included. With a
rare status, a page of **Tasks** can come back short or empty while **Show older** still reads
further: each page reads a bounded number of tasks.

### Tasks View
- **Pipeline** — Filter by pipeline name
- **Status** — Filter by task status (the Status cell also shows compact retry chips: ↻ for manual Restart, ⭮ for auto-retry)
- **Date** — Filter by execution date

The **Source** column shows what started the run: Console, Schedule or Asset.

### Runs View
A single table of pipeline executions (ADR #95). A run is running until every task has
finished; while it runs, its status says how many tasks failed (`running, 2 failed`).
A run started before 1.3 with a task left unfinished shows `stale`, and so does the task:
1.3 finishes neither, and the upgrade stopped the run.
- **Pipeline** — Filter by pipeline name
- **Status** — Filter by run status (succeeded/failed/running/aborted)
- **Date** — Filter by the run's logical date

---

## Help Modal

Press **?** or click Help to see:

### Shortcuts
Every keyboard shortcut, grouped by where it applies

### Icons
All status icons explained

---

## Notifications Panel

Shows recent alerts, whatever the date of the run:
- Failed tasks of the last 48 hours, one per run
- Tasks waiting for a decision, one per run, with when it fails unless you decide; first, the most urgent first
- Coordination warnings, named by their kind (see [alerts.md](../features/alerts.md))

Click notification to navigate to related task/pipeline.

---

## Keyboard Shortcuts

The Help modal (`?`) lists every shortcut. The most used:

| Key | Action |
|-----|--------|
| `?` | Open help |
| `⌘K` | Command palette |
| `Esc` | Close modal |
| `⌘R` | Refresh |
| `/` | Focus the pipeline search (Pipelines page); `Enter` there opens the first match, `↓` moves into the list |
| `J` / `K` | Next / previous pipeline or group, while the sidebar list has focus; `Esc` goes back to the search |

---

## Technical Stack

- **React 19** — UI framework
- **Next.js 16** — App Router, built as a static export
- **TypeScript** — Type safety
- **React Query 5** (`@tanstack/react-query`) — Server state, caching, refetch
- **Zustand 5** — UI state management (navigation, modals, filters)
- **ReactFlow 11** — DAG visualization
- **dagre** — Automatic graph layout
- **Tailwind CSS 3 + shadcn/ui** — Styling and UI primitives
- **Global CSS** — BEM-prefixed classes per surface, theme-aware variants
- **Vitest + Testing Library** — unit and integration tests

---

## State Management

Server data and UI state are deliberately separated:

| Layer | Tool | What it holds |
|-------|------|--------------|
| **Server state** | React Query 5 | Pipeline list, task details, metrics, notifications, system status. Caching, background refetch, optimistic updates. |
| **UI state** | Zustand 5 (`useAppStore`) | Selected pipeline, date, view mode, theme, modal visibility, filters. |
| **URL state** | `useStoreInit` | Bidirectional URL ↔ store sync (`?pipeline=x&date=2025-01-15&view=dag`). |

**Performance pattern:** All components use `useShallow` selectors to subscribe to only the specific store fields they need, preventing unnecessary re-renders when unrelated state changes.

React Query hooks live in `hooks/queries/` — `usePipelineQueries.ts` (pipelines, a pipeline's tasks, its runs), `useGlobalQueries.ts` (all tasks, all runs, task events, notifications), `useSystemStatusQuery.ts` (the System pill: `/api/health` as a tagged state, and a failed pipelines read as `connection`).

---

## Component Architecture

```
App.tsx
├── AppNav               — left rail: Pipelines, History, account menu, theme toggle
├── PipelinesSidebar     — pipeline list, groups, search, sparklines, schedule
├── Header               — breadcrumbs, System status pill, auto-refresh toggle, notifications
├── CommandPalette       — ⌘K fuzzy search across pipelines + actions
├── HelpModal            — shortcuts and icons
└── Main content (switches on mainView)
    ├── PipelineDetail   — DAG, date picker, run selector, run actions
    │   ├── DAGGraphFlow         — ReactFlow + dagre layout, status coloring
    │   └── TaskDetailModal/     — tabs Details, History, Input / Output, Actions
    │       ├── ErrorDisplay     — formatted error output
    │       ├── LiveDuration     — real-time duration for running tasks
    │       └── OutputCard       — the task's stored output
    ├── AllTasksView     — History → Tasks: cross-pipeline task table with filters
    └── AllRunsView      — History → Runs: pipeline runs, with filters
```

`TaskDetailModal` is **lazy-loaded** via `React.lazy()` + `Suspense` in PipelineDetail to reduce initial bundle (~50KB deferred).

### Modal System

All modals extend `BaseModal` which provides: focus trap (Tab/Shift+Tab), ESC to close, click-outside dismiss, body scroll lock, ARIA dialog role, and focus restoration on close.

---

## Accessibility

The console implements WCAG-aligned accessibility across all interactive components:

- **Modals**: `role="dialog"`, `aria-modal="true"`, `aria-labelledby`, focus trap, ESC close, focus restoration
- **Tabs** (TaskDetailModal, PipelineDetail view modes): `role="tablist"` / `role="tab"`, `aria-selected`, keyboard Enter/Space navigation
- **Sidebar**: a `nav` of buttons, `aria-current="page"` on the open pipeline; the list is one Tab stop, and J/K or Up/Down move through it
- **Status badges**: `aria-label="Task status: {status}"` for screen readers
- **Toasts**: `role="alert"`, `aria-live="polite"`
- **Sortable headers**: `role="columnheader"`, `aria-sort`, keyboard Enter/Space
- **Loading skeletons**: `role="status"`, `aria-label="Loading..."`, `sr-only` text
- **CountdownTimer**: `role="timer"`, `aria-live="polite"`, dynamic `aria-label`
- **Error boundary**: `role="alert"` on error display
- **Group headers** (sidebar): buttons with `aria-expanded`

---

## Testing

```bash
npm test -- --run                   # All tests
npm test -- --run src/components/DAGGraphFlow.test.tsx  # Single file
```

**Test infrastructure (`src/test/`):**

- `factories.ts` — builder functions: `createTask()`, `createDAG()`, `createPipeline()`, `createAsset()`, `createExecution()`, etc. All accept override objects.
- `mocks.tsx` — shared mocks for icons (Proxy-based), BaseModal (renders children directly), shadcn/ui Button, config, API. Import with `import '../test/mocks'`.
- `setup.ts` — global setup: localStorage mock, fetch mock, jsdom polyfills.

**Mocking patterns:**

- **ReactFlow** (DAGGraphFlow): mock `reactflow` module to render nodes as `<div data-testid="rf-node-{id}">`, mock `dagre` for layout.
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

`polyris-up` installs the console with the platform: S3 + CloudFront, with `config.js` written from the stack's outputs. From a checkout, `ui/deploy.sh <stack> <region>` uploads the output of `npm run build` to the console's bucket.

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
site; a non-empty `API_URL` there wins over `.env.local`, so for local work
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
> stage and `/api` (`…/dev/api`). The host alone, `/api` alone, or the stage
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
> 3. **Clear stale caches, then restart.** `NEXT_PUBLIC_*` are baked at
>    startup, so editing `.env` needs a `npm run dev` restart (and
>    `rm -rf ui/.next` if a value still looks stale). Crucially,
>    `/config.js` is loaded by a plain `<script src="/config.js">` with no
>    cache-busting — the **browser caches it**. After editing
>    `ui/public/config.js` hard-reload with cache disabled (DevTools →
>    Network → "Disable cache" → reload) or use a fresh private window.
>    Otherwise the old `AUTH.enabled` keeps winning. Quick check: run
>    `window.CONFIG` in the DevTools console and confirm `AUTH.enabled` is
>    what you expect.
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
│  📋 acme-daily                                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│    ┌─────────┐     ┌─────────┐     ┌─────────┐         │
│    │ scrape  │────▶│ process │────▶│  merge  │         │
│    │   ✓     │     │   ●     │     │   ○     │         │
│    └─────────┘     └─────────┘     └─────────┘         │
│                                                         │
└─────────────────────────────────────────────────────────┘
```
