# Development

How to test and check a change to polyris itself, and how the console is built. Setting up a checkout: [CONTRIBUTING.md](../../CONTRIBUTING.md). Deploying it: [FROM_SOURCE.md](../deployment/FROM_SOURCE.md).

---

## API Input Validation

The Console API validates a date from a request with `validate_date` in `utils.py`:

```python
from utils import validate_date

valid, error = validate_date('2025-01-15')  # (True, None)
valid, error = validate_date('invalid')     # (False, 'Date must be in YYYY-MM-DD format')
```

---

## Running Tests

### All Tests

```bash
make test              # Runs: test-sdk + test-backend + test-integration + test-docs + test-lambdas + test-sfn-jsonata + test-ui
make check             # Runs: lint + generated-code drift + typecheck + smoke-pipelines + test
```

### Python SDK Tests

```bash
# SDK, backend and integration tests (smoke, snapshots, flow, templates, trigger rules, api routes)
python -m pytest tests/sdk tests/backend tests/integration -v

# Docs drift tests: their own session, since next to tests/backend `tests` resolves to console_api's test package
make test-docs

# Specific test files
pytest tests/sdk/test_smoke.py -v              # Core: DAG structure, ASL generation
pytest tests/sdk/test_asl_snapshots.py -v      # Golden file comparison of generated JSON
pytest tests/sdk/test_sfn_flow.py -v           # SFN template graph validation (every orchestration template)
pytest tests/sdk/test_templates.py -v          # orchestration_timeout, route table, resilience
pytest tests/backend/test_api_routes.py -v         # All 24 free API routes registered
pytest tests/sdk/test_coordination_trigger_rules.py -v  # The nine trigger rules and their truth table (polyris/coordination)

# Update ASL snapshots after intentional changes
SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots.py
```

### SFN JSONata Expression Tests

Tests critical JSONata expressions from SFN templates with mock inputs.
Catches: broken defaults, type conversion bugs, conditional logic errors.

```bash
cd tests/sfn_jsonata
npm install    # first time only
npm test       # expression tests, then a compile check of every expression in the templates
```

### Lambda Unit Tests

```bash
# Every Lambda suite, with no AWS profile, region or credentials, as CI runs them
make test-lambdas

# One suite: the Lambda's folder and the checkout's root on PYTHONPATH
cd sam/lambdas/coordination && PYTHONPATH=.:../../.. python3 -m pytest tests/ -v   # dispatcher, start-run, asset-fanout, sweeper
cd sam/lambdas/console_api && PYTHONPATH=.:../../.. python3 -m pytest tests/ -v    # API routes, pagination, validation
```

### UI Tests

```bash
cd ui
npm test          # Watch mode
npm run test:run  # Single run (CI)
```

---

## CI Pipeline

All tests run automatically on push/PR to main via GitHub Actions (`.github/workflows/ci.yml`).

| Job | What runs |
|-----|-----------|
| **Python 3.11/3.12** | ruff lint, mypy, SDK, backend, integration and docs tests, the coverage floor, generated-code drift, the open-core guard, pipeline imports |
| **Lambda Tests** | Syntax check, logger sync, every Lambda suite (console_api, coordination, bucket_cleanup, …) |
| **SFN Templates** | ASL JSON validation, `test_sfn_flow.py` (graph integrity), JSONata expression tests |
| **SAM** | `cfn-lint sam/template.yaml` |
| **UI** | npm lint, typecheck, `vitest run`, `next build`, the open-core build guard |
| **Platform archive** | `sam build` and the release archive `polyris-up` deploys |
| **Docs** | link check (lychee) and prose lint (Vale) |

---

## Code Quality Checks

### Python Syntax

```bash
python -m py_compile polyris/*.py
python -m py_compile sam/lambdas/**/*.py
```

### Type checking

```bash
make typecheck         # mypy on the whole polyris package, as CI runs it
```

### JavaScript linting

```bash
cd ui
npm run lint
npm run typecheck
```

---

## Console (UI)

The console is a static export of a React app: there is no Next.js server, and the browser calls the console API directly. What the console shows and does: [UI.md](../operations/UI.md).

### Stack

- **React 19**, **Next.js 16** (App Router, built as a static export), **TypeScript** in strict mode
- **React Query 5** (`@tanstack/react-query`): server state, caching, refetch
- **Zustand 5**: UI state (navigation, modals, filters)
- **ReactFlow 11** and **dagre**: the pipeline graph and its layout
- **Tailwind CSS 3** and **shadcn/ui**: styling and UI primitives; global CSS with BEM-prefixed classes per surface
- **Vitest** and **Testing Library**: tests

### State

| Layer | Tool | What it holds |
|-------|------|--------------|
| Server state | React Query 5 | Pipelines, tasks, runs, notifications, system status |
| UI state | Zustand 5 (`useAppStore`) | Selected pipeline, date, theme, modal visibility, filters |
| URL state | `useStoreInit` | Two-way sync of the URL and the store (`?pipeline=x&date=2025-01-15`) |

Components read the store through `useShallow` selectors, so a change to an unrelated field does not re-render them. Query hooks live in `src/hooks/queries/`: `usePipelineQueries.ts` (pipelines, a pipeline's tasks, its runs), `useGlobalQueries.ts` (all tasks, all runs, task events, notifications) and `useSystemStatusQuery.ts` (the **System** pill: `/api/health`, and a failed pipelines read as a lost connection).

### Components

```
App.tsx
├── AppNav               — left rail: Pipelines, History, account menu, theme toggle
├── PipelinesSidebar     — pipeline list, groups, search, sparklines, schedule
├── Header               — breadcrumbs, System status pill, auto-refresh toggle, notifications
├── CommandPalette       — ⌘K search across pipelines and actions
├── HelpModal            — shortcuts and icons
└── Main content (switches on mainView)
    ├── PipelineDetail   — graph, date picker, run selector, run actions
    │   ├── DAGGraphFlow         — ReactFlow + dagre layout, status colours
    │   └── TaskDetailModal/     — tabs Details, History, Input / Output, Actions
    │       ├── ErrorDisplay     — formatted error output
    │       ├── LiveDuration     — live duration of a running task
    │       └── OutputCard       — the task's stored output
    ├── AllTasksView     — History → Tasks
    └── AllRunsView      — History → Runs
```

`PipelineDetail` loads `TaskDetailModal` lazily (`React.lazy()` and `Suspense`). Every modal builds on `BaseModal`: focus trap, Esc to close, click-outside dismiss, body scroll lock, the ARIA dialog role, and focus restored on close.

### Accessibility

- Modals: `role="dialog"`, `aria-modal="true"`, `aria-labelledby`, focus trap, Esc to close, focus restored
- Tabs: `role="tablist"` / `role="tab"`, `aria-selected`, Enter and Space
- Sidebar: a `nav` of buttons, `aria-current="page"` on the open pipeline; the list is one Tab stop, and J/K or the arrow keys move through it; group headers are buttons with `aria-expanded`
- Status badges: `aria-label="Task status: {status}"`
- Toasts: `role="alert"`, `aria-live="polite"`
- Sortable headers: `role="columnheader"`, `aria-sort`, Enter and Space
- Loading skeletons: `role="status"`, `aria-label="Loading..."`, `sr-only` text
- `CountdownTimer`: `role="timer"`, `aria-live="polite"`, a live `aria-label`
- Error boundary: `role="alert"`

### Tests

```bash
cd ui
npm test -- --run                                       # every test
npm test -- --run src/components/DAGGraphFlow.test.tsx  # one file
```

`src/test/` holds the shared setup:
- `factories.ts`: builders such as `createTask()`, `createDAG()`, `createPipeline()`, `createExecution()`; each takes an object of overrides.
- `mocks.tsx`: shared mocks for icons, `BaseModal` (renders its children), the shadcn/ui Button, config and the API. Import with `import '../test/mocks'`.
- `setup.ts`: global setup: a localStorage mock, a fetch mock, jsdom polyfills.

Mock ReactFlow by rendering nodes as `<div data-testid="rf-node-{id}">` and mocking `dagre`; mock the API with `vi.fn().mockResolvedValue()` on `utils/api`.

### Runtime config

The console reads `/config.js` at load, and its values win over the `NEXT_PUBLIC_*` values baked in at build. `API_URL` is the `ConsoleApiUrl` stack output followed by `/api`: the app appends route paths without `/api`.

```javascript
window.CONFIG = {
  API_URL: "https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/dev/api"
};
```

`polyris-up` writes it from the stack's outputs; on a stack deployed from a checkout, `ui/deploy.sh <stack> <region>` writes it and uploads the build.

### Run the console locally against a deployed API

The API's CORS allows any origin, so the dev server on `localhost` calls the deployed API directly.

```bash
cd ui
cp .env.example .env.local   # then set the values below
npm run dev                  # http://localhost:3000
```

Set the values in `ui/.env.local` and leave `ui/public/config.js` as it is: its empty `API_URL` and `null` `AUTH.enabled` fall through to `.env.local`, while a value there would win.

```env
# Full invoke URL: <id>.execute-api.<region>.amazonaws.com/<stage>/api
NEXT_PUBLIC_API_URL=https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/dev/api
NEXT_PUBLIC_AUTH_ENABLED=true
NEXT_PUBLIC_COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX
NEXT_PUBLIC_COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
NEXT_PUBLIC_COGNITO_REGION=us-east-1
```

The pool and the client are the stack's `CognitoUserPoolId` and `CognitoClientId` outputs. Amplify signs in with email and password, so there are no callback URLs to register for `localhost`. For an API deployed with `EnableCognitoAuth=false`, set `NEXT_PUBLIC_AUTH_ENABLED=false` and leave out the Cognito values.

`NEXT_PUBLIC_API_URL` must carry both the stage and `/api` (`…/dev/api`); the host alone, `/api` alone, or the stage without `/api` all answer 404.

**401 `UNAUTHORIZED` from the local console:** the API enforces auth by its own setting, and the local console is not sending a token it accepts.
1. Set `NEXT_PUBLIC_AUTH_ENABLED=true` in `ui/.env.local`. A literal `enabled: false` in `ui/public/config.js` would override it.
2. Set `NEXT_PUBLIC_COGNITO_USER_POOL_ID` and `NEXT_PUBLIC_COGNITO_CLIENT_ID` to what the `console-api` Lambda has in `COGNITO_USER_POOL_ID` and `COGNITO_CLIENT_ID`: a token from another pool is refused.
3. Restart `npm run dev`: `NEXT_PUBLIC_*` values are read at start (`rm -rf ui/.next` if one still looks stale). The browser caches `/config.js`, so reload with the cache disabled, or use a private window; `window.CONFIG` in the DevTools console shows what it loaded.
4. Sign out and in again: a session from another pool keeps sending the wrong token.
