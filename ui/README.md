# Polyris Console

React 19 + Next.js 16 web console for monitoring and managing Polyris pipelines.

**Stack:** TypeScript, React Query 5 (server state), Zustand 5 (UI state), ReactFlow 11 (graphs), Tailwind 3, shadcn/ui.

## Structure

```
ui/
├── public/
│   └── config.js                   # Runtime config fallback
├── scripts/
│   └── css_audit.py                # CSS dead code analyzer
├── src/
│   ├── app/
│   │   ├── layout.tsx              # Root layout (fonts, providers)
│   │   ├── page.tsx                # Entry point → App
│   │   └── api/[...path]/route.ts  # API proxy (server-side → API Gateway)
│   ├── components/
│   │   ├── TaskDetailModal/        # Split modal (6 files + test + CSS)
│   │   │   ├── TaskDetailModal.tsx  # Main component (tabs, actions)
│   │   │   ├── ConsecutiveProgress.tsx
│   │   │   ├── DependencyStatusList.tsx
│   │   │   ├── ErrorDisplay.tsx
│   │   │   ├── LiveDuration.tsx
│   │   │   ├── helpers.ts          # evaluateDepStatus, trigger rule logic
│   │   │   └── index.tsx           # Barrel export
│   │   ├── ui/                     # shadcn/ui primitives
│   │   │   └── button, input, select, dialog, badge, checkbox, tabs, ...
│   │   ├── DAGGraphFlow.tsx        # Pipeline DAG (ReactFlow + dagre)
│   │   ├── AssetLineageFlow.tsx    # Asset lineage graph (ReactFlow)
│   │   ├── GanttChart.tsx          # Execution timeline
│   │   ├── CalendarView.tsx        # Monthly execution calendar
│   │   ├── PipelineDetail.tsx      # Pipeline detail view (lazy-loads modals)
│   │   ├── PipelinesSidebar.tsx    # Pipeline list with groups + search
│   │   ├── AssetsView.tsx          # Asset management view
│   │   ├── AllTasksView.tsx        # Cross-pipeline task list
│   │   ├── AllRunsView.tsx         # Cross-pipeline execution list
│   │   ├── Header.tsx              # Top bar (date, mode, nav)
│   │   ├── BackfillModal.tsx       # Backfill config (tasks/assets/date range)
│   │   ├── ActionModal.tsx         # Pipeline action confirmation
│   │   ├── CommandPalette.tsx      # ⌘K command palette
│   │   ├── HelpModal.tsx           # Keyboard shortcuts + API docs
│   │   ├── Notifications.tsx       # Real-time notification panel
│   │   ├── BaseModal.tsx           # Base modal (focus trap, ESC, overlay)
│   │   ├── Modal.tsx, ConfirmModal.tsx  # Simple dialogs on BaseModal
│   │   ├── Toast.tsx               # Toast notification system
│   │   ├── CountdownTimer.tsx      # wait_before countdown
│   │   ├── SortableHeader.tsx      # Sortable table column header
│   │   ├── Skeletons.tsx           # Loading skeletons (all views)
│   │   ├── ErrorBoundary.tsx       # Error boundary + retry
│   │   ├── LoginPage.tsx           # Cognito auth UI
│   │   ├── AuthGate.tsx            # Auth state gate
│   │   ├── UserMenu.tsx            # User dropdown
│   │   ├── Providers.tsx           # App-wide providers wrapper
│   │   ├── shared.module.css       # Shared component styles
│   │   └── *.module.css            # Per-component CSS modules
│   ├── hooks/
│   │   ├── queries/                # React Query hooks (server state)
│   │   │   ├── usePipelineQueries.ts   # Pipelines, detail, executions, metrics
│   │   │   ├── useAssetQueries.ts      # Assets, events, triggers, backfill
│   │   │   ├── useGlobalQueries.ts     # All-tasks, all-runs, consecutive progress
│   │   │   └── index.ts
│   │   ├── usePipelineActions.tsx   # Pipeline action handlers (run, stop, etc.)
│   │   ├── usePipelineData.ts      # Pipeline data aggregation
│   │   ├── useAuth.tsx             # Cognito authentication hook
│   │   ├── useUrlSync.ts           # URL ↔ app state synchronization
│   │   ├── useKeyboardShortcuts.tsx # Global keyboard shortcuts
│   │   ├── useTaskEvents.ts        # Task event timeline data
│   │   ├── useGlobalData.ts        # Cross-pipeline data aggregation
│   │   ├── useFocusTrap.ts         # Focus trap for modals
│   │   ├── usePersistedState.ts    # localStorage-backed state
│   │   └── index.ts
│   ├── stores/
│   │   ├── useAppStore.ts          # Zustand UI state (27 fields)
│   │   ├── useStoreInit.ts         # URL sync + store initialization
│   │   └── index.ts
│   ├── lib/
│   │   ├── api-server.ts           # Server-side API Gateway proxy
│   │   ├── config.ts               # Environment config
│   │   ├── amplifyConfig.ts        # Cognito/Amplify setup
│   │   ├── queryClient.tsx         # React Query client + query keys
│   │   └── utils.ts                # shadcn/ui cn() helper
│   ├── styles/
│   │   ├── globals.css             # Tailwind base + CSS custom properties
│   │   ├── index.css               # Module imports
│   │   └── modules/
│   │       ├── _layout.css         # App shell layout
│   │       ├── _navigation.css     # Sidebar, tabs, pills
│   │       ├── _tasks.css          # Task table, detail panel
│   │       ├── _dag.css            # DAG graph nodes, edges
│   │       ├── _assets.css         # Asset views, lineage
│   │       ├── _modals.css         # Modal layouts
│   │       ├── _status.css         # Status badges, colors
│   │       ├── _enhanced-ui.css    # Animations, transitions
│   │       ├── _utilities.css      # Utility classes (legacy)
│   │       ├── _accessibility.css  # sr-only, focus styles
│   │       ├── _base.css           # Reset, typography
│   │       ├── _mobile.css         # Responsive breakpoints
│   │       ├── _pipeline-pause.css # Pause state indicators
│   │       ├── _view-loader.css    # View transition loaders
│   │       └── login.css           # Login page
│   ├── test/
│   │   ├── factories.ts            # Test data builders
│   │   ├── mocks.tsx               # Shared mocks (icons, BaseModal, api)
│   │   └── setup.ts                # Vitest global setup
│   ├── types/
│   │   ├── index.ts                # All TypeScript interfaces
│   │   └── dagre.d.ts              # dagre type declarations
│   ├── utils/
│   │   ├── api.ts                  # Client-side API (get/post + auth)
│   │   ├── constants.ts            # Status enums, terminal states, polling
│   │   ├── countdown.ts            # Wait countdown computation
│   │   ├── dagHelpers.ts           # DAG traversal (upstream/downstream)
│   │   ├── formatters.ts           # Date, duration, schedule formatters
│   │   ├── staleness.ts            # Asset staleness calculation
│   │   ├── icons.tsx               # Icon re-exports + StatusIcon component
│   │   ├── logger.ts               # Structured logger
│   │   ├── storage.ts              # SSR-safe localStorage wrapper
│   │   └── index.ts                # Barrel export
│   └── App.tsx                     # Root component (sidebar + main views)
├── next.config.mjs
├── tailwind.config.js
├── tsconfig.json
├── vitest.config.ts
└── package.json
```

## Architecture

### State Management: Zustand + React Query Split

Server data and UI state are deliberately separated:

| Layer | Tool | What it holds | Example |
|-------|------|--------------|---------|
| **Server state** | React Query 5 | API data, caching, refetch | Pipeline list, task details, metrics |
| **UI state** | Zustand 5 | Navigation, modals, filters | Selected pipeline, view mode, theme |
| **URL state** | `useStoreInit` | Bidirectional URL ↔ store sync | `?pipeline=x&date=2025-01-15&view=dag` |

**Store subscription pattern (useShallow):** All components use `useShallow` selectors to prevent unnecessary re-renders:

```tsx
// ✅ Good — only re-renders when these 3 fields change
const { selectedPipeline, date, viewMode } = useAppStore(useShallow(s => ({
    selectedPipeline: s.selectedPipeline,
    date: s.date,
    viewMode: s.viewMode,
})));

// ❌ Avoid — subscribes to ALL 27 store fields
const store = useAppStore();
```

Exception: `useStoreInit.ts` intentionally uses bare `useAppStore()` for mount-only initialization.

### Modal System

All modals extend `BaseModal` which provides: focus trap (Tab/Shift+Tab), ESC to close, click-outside to close, body scroll lock, ARIA dialog role, and focus restoration.

`TaskDetailModal` and `BackfillModal` are **lazy-loaded** via `React.lazy()` in PipelineDetail to reduce initial bundle (~50KB deferred).

### Component Architecture

```
App.tsx
├── PipelinesSidebar    — pipeline list, groups, search, sparklines
├── Header              — date picker, view mode tabs, modals, theme
└── Main content (switches on mainView)
    ├── PipelineDetail  — DAG/Gantt/Calendar + execution selector
    │   ├── DAGGraphFlow      — ReactFlow + dagre layout
    │   ├── GanttChart        — timeline bars
    │   └── CalendarView      — monthly grid
    ├── AllTasksView    — cross-pipeline task table
    ├── AllRunsView     — cross-pipeline execution table
    └── AssetsView      — asset list + AssetLineageFlow graph
```

## Development

```bash
cp .env.example .env.local
# Edit .env.local: NEXT_PUBLIC_API_URL=https://<id>.execute-api.<region>.amazonaws.com/<stage>/api

npm install
npm run dev     # http://localhost:3000
```

## Environment Variables

| Variable | Where | Purpose |
|----------|-------|---------|
| `NEXT_PUBLIC_API_URL` | Client | Full API Gateway invoke URL, incl. stage and `/api` (e.g. `…/dev/api`) |
| `NEXT_PUBLIC_AUTH_ENABLED` | Client | Enable Cognito auth |
| `NEXT_PUBLIC_COGNITO_USER_POOL_ID` | Client | Cognito pool ID |
| `NEXT_PUBLIC_COGNITO_CLIENT_ID` | Client | Cognito app client ID |
| `NEXT_PUBLIC_COGNITO_REGION` | Client | AWS region |


## Build

```bash
npm run build   # Next.js production build
npm run lint    # ESLint (0 errors required for CI)
```

## Testing

```bash
npm test -- --run           # All 511 tests
npm test -- --run src/components/DAGGraphFlow.test.tsx  # Single file
```

**Test infrastructure:**

- `test/factories.ts` — builder functions: `createTask()`, `createDAG()`, `createPipeline()`, `createAsset()`, etc. All accept override objects.
- `test/mocks.tsx` — shared mocks for icons (Proxy-based), BaseModal, shadcn/ui Button, config, API. Import with `import '../test/mocks'`.
- `test/setup.ts` — global setup: localStorage mock, fetch mock, jsdom polyfills.

**Test patterns:**

- ReactFlow components (DAGGraphFlow, AssetLineageFlow) — mock `reactflow` to render nodes as `<div>` elements, mock `dagre` for layout.
- Modals — mock `BaseModal` to render children directly (no portal/animation).
- Icons — Proxy mock returns `<span data-testid="icon-{name}" />` for any icon.
- API — mock `utils/api` with `vi.fn().mockResolvedValue()`.

## Deployment

The console is a **static export** deployed to **S3 + CloudFront** via
`./deploy.sh` (a.k.a. `sam/deploy-ui.sh`), which reads CloudFormation outputs and
generates `config.js` with the real API Gateway URL and Cognito settings.

See [docs/operations/UI.md](../docs/operations/UI.md) for the full guide, including
running the UI locally against a deployed API (with and without Cognito).

## Styling Conventions

The project uses a hybrid styling approach:

| System | When to use | Examples |
|--------|------------|---------|
| **Tailwind** | Layout, spacing, typography, responsive | `flex items-center gap-2`, `text-sm font-medium` |
| **CSS Modules** (`styles/modules/`) | Complex animations, pseudo-selectors, DAG/ReactFlow, theme-aware variants | `_dag.css`, `_status.css` |
| **Component CSS** (`*.module.css`) | Per-component scoped styles | `ActionModal.module.css` |
| **shadcn/ui** | UI primitives | `<Button variant="destructive">`, `<Dialog>` |
| **Inline styles** | Dynamic runtime values only | `style={{ left: node.x }}` |

**Do not** use inline styles for static styling — use Tailwind or CSS classes instead.

## Accessibility

- All modals: `role="dialog"`, `aria-modal`, `aria-labelledby`, focus trap, ESC close
- Tabs (TaskDetailModal, PipelineDetail): `role="tablist"` / `role="tab"`, `aria-selected`, keyboard Enter/Space
- Status badges: `aria-label="Task status: {status}"`
- Toasts: `role="alert"`, `aria-live="polite"`
- Sortable headers: `role="columnheader"`, `aria-sort`, keyboard support
- Skeletons: `role="status"`, `aria-label="Loading..."`
- Sidebar: `role="listbox"` / `role="option"`, `aria-selected`, keyboard navigation
- CountdownTimer: `role="timer"`, `aria-live="polite"`

## ESLint Suppressions

All `eslint-disable react-hooks/exhaustive-deps` are documented in `stores/useStoreInit.ts` (6 suppressions) and `PipelineDetail.tsx` (1 suppression). Each has an inline comment explaining which deps are omitted and why (mount-only effects, stable Zustand setters, stable callback refs).

## CSS Dead Code (v68 audit)

154 potentially unused CSS classes out of 744 total (21%). Top offenders:

- `_utilities.css` — 48 unused (replaced by Tailwind)
- `_dag.css` — 37 unused (legacy pre-ReactFlow)
- `_assets.css` — 35 unused (legacy pre-refactor)

Run audit: `python3 scripts/css_audit.py`
