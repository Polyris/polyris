# CLAUDE.md — Polyris Agent Instructions

Read this fully before writing any code. When in doubt, `grep` first.

---
## Repository context

**You are in `polyris` — the public, open-core repo.** It ships the free SDK (`polyris/`), the free console backend (`sam/lambdas/console_api/`), and the free UI (`ui/src/`, everything outside `src/ee/`). The private `polyris-ee` repo depends on it — never the reverse.

- **Never add proprietary/paid code here.** Paid code lives only in `polyris-ee`. `polyris._ee*` is excluded from the wheel; no `ee/` directory in this checkout.
- **The seam is enforced.** Free UI reaches paid surface only through the generated `@/ee-active.generated` stub (empty in OSS). `ui/scripts/check-oss-build.sh` CI gate strips `src/ee/` and asserts the build still typechecks.
- **Self-contained build/test:** `pip install -e ".[dev]"` then `python -m pytest tests/sdk tests/backend tests/integration`; for the UI, `cd ui && npm ci && npm run typecheck && npm test -- --run && npm run build`.

Adding a feature? Decide the tier first: authoring + basic read → free (here); operations / observability / governance → paid (private repo).

---
## Core Principles

These principles override everything else. Read before every change.

**1. No duplication**
One source of truth per concept. `grep` before writing new code, components, or constants.

**2. Follow existing patterns**
If there's an existing way — use it. DAL repos, pytest-mock, cors_response, log.error — all already exist.

**3. Idempotency**
Every operation must be safe to repeat. DynamoDB conditional updates, `_sfn_stop()` that ignores already-stopped.

**4. Stability first**
"Don't break what works" before "add the new thing". Every change: `pytest` + `cfn-lint` + syntax check.

**5. No fix on top of a fix — do it right**
If the solution is unclear — ask first. A minute of discussion beats an hour of fixing the wrong approach.

**6. Align on architectural decisions**
DynamoDB schema, SFN flow, API contract, file structure, deployment approach — requires explicit approval. After alignment, document in ADR (`docs/reference/DESIGN_DECISIONS.md`).

**7. Finish what you start**
Every task: code, tests, documentation, CHANGELOG. If something can't be finished — document exactly what remains and why.

**8. Asked a question — answer. Asked to do — do**
Don't jump to implementation when a question is asked. If unclear — ask, don't decide alone.

**9. Docs in the same commit as the code**
Every change affecting behavior, API, config, or architecture updates the relevant docs in **the same commit**. Stale docs are bugs.

**Exception:** `cmd_*` argparse handlers in `polyris/cli.py` may omit docstrings when they only unpack args and delegate to a documented function — the argparse help text is the doc.

**10. English only**
Docs, comments, ADRs, README, CHANGELOG — English, no exceptions.

**11. No stubs or "make it pass" workarounds in production code**
Never insert placeholder values, dummy data, or shortcut paths. Forbidden patterns:
- Returning empty/dummy values when the real logic is hard
- Skipping validation, auth, or error handling "for now"
- Disabling or weakening assertions in failing tests instead of fixing the underlying issue
- Catching and ignoring exceptions to mask bugs (`except SomeException: pass` without a justification comment is a presumptive bug — three legitimate uses: (1) unreachable defensive branch — document why; (2) optional absent dependency — log at debug; (3) explicit no-op guarding against dict mutation — comment the scenario)

If something can't be implemented properly: stop, document in `STATE.md`, discuss before shipping.

Test mocks (pytest-mock `mocker`, vitest `vi.fn()`) belong in `tests/` only — never in production.

**12. Maximize reuse — check before going custom**
Before creating anything new, `grep` for what already exists. Check: `BaseModal`, `action-btn`, `.dag-container`, `cors_response`, `log.error`, DAL repos. Extend existing things rather than forking. Custom only when nothing reusable exists. No wildcard imports (`from x import *`) — ever.

**13. Tests must verify integration contracts, not just function calls**
Mock-based tests that verify "function X was called with Y" don't check whether Y is valid in the real system. Pin **integration contracts**: DDB field names as named constants imported by both production code and tests; schema fixtures from real AWS responses; snapshot tests for route tables, SFN structure, DDB schema.

**14. Mock at the external boundary, never at internal logic**
Mock boundaries: `boto3.client`, `urllib.request.urlopen`, `open()`, `datetime.now`, `uuid.uuid4`. Everything inside runs real code. For DDB, use `moto` — production code calls `boto3` normally; moto intercepts at the SDK level.

Anti-pattern:
```python
mocker.patch('routes.backfill._scan_completed_partitions', return_value=set())
# Tests nothing useful — tests the mock, not the system.
```

**15. Smoke test happy paths before tagging a release**
Before any `vX.Y.Z` tag:
1. Deploy current main to a dev account
2. Run `pytest -m smoke` against the live deployment
3. Manually exercise one happy path per new feature in the UI

Tests in `tests/e2e/` marked `@pytest.mark.smoke` are skipped without `POLYRIS_API_URL`. They're the gate, not optional.

**16. New API endpoints require e2e tests in the same delivery**
New route → new `tests/e2e/test_*.py` in the same change. Verifies: 200/202 on happy path, response shape, 4xx on invalid input, 404 for non-existent IDs.

**17. Verify integration with the rest of the application on every change**
Before commit:
- What else reads this? `grep -r '<field_name>'` widely.
- What else writes this? Migration needed for existing rows?
- Touch SFN templates → run `pytest tests/sdk/test_asl_snapshots.py`. Touch UI types → run `tsc --noEmit`. Touch CSS class names → run vitest.

Critical zones: DDB schema changes; API contracts; shared constants (`BackfillStatus`, `BackfillLimits`); BEM CSS renames; ADR-governed behaviors.

**18. Don't build features for hypothetical use cases**
No real user today → feature doesn't ship. Hardcoded numbers with a calculated justification (AWS quota-derived, business invariant) should NOT be made tunable — edit + redeploy is one command; a tunability layer is config schema, IAM, caching, validation, tests, docs, and forever support.

**19. Keyboard shortcuts on every new surface (ADR #64, revised #64.1)**

Standard mapping by surface type:
- **Top-level navigation** (App.tsx only): `1`-`9` switch primary views. **Reserved — no other surface may bind them.**
- **List views**: `⌘R` refresh, `/` focus filter, `j`/`k` next/prev row, `Enter` open row.
- **Detail pages**: `⌘R` refresh, `Esc` back to list.
- **Multi-tab containers**: letter keys matching the first letter of each tab name.
- **Modals with primary action**: `Esc` close, `⌘↵` submit.

After wiring, update `HelpModal::KeyboardShortcutsTab`. Use `useKeyboardShortcuts` hook with `SHORTCUTS` catalog. Don't add raw `document.addEventListener('keydown', …)`.

Before adding a shortcut, check conflicts: `grep -rn "'<key>'" ui/src`. App.tsx's `1`-`5` are global — never re-bind at a non-App surface. A PR that adds a numeric shortcut at a non-App surface is broken and must be rejected.

**20. Top-level view sync across three files (ADR #65)**

Adding a top-level view → update all three in the same PR:
1. `ui/src/types/index.ts` — `MAIN_VIEWS` and `MainView` type
2. `sam/template.yaml` — regex in `ConsoleUiUrlRewriteFunction.FunctionCode`
3. `ui/src/app/page.tsx` — `validViews` array

Missed any → clicking the new tab redirects to `/pipelines/` (S3 404 → CloudFront returns `/index.html` → RootPage default redirect). Backend (`sam deploy`) and frontend (`deploy-ui.sh`) must ship together.

**21. The build must be clean — zero errors and zero warnings**
Before declaring done: `ruff check`, `mypy`, `pytest`; `eslint`, `tsc --noEmit`, `vitest` — all green with nothing flagged. Suppress only at the narrowest scope with a written justification. Blanket-disabling is a Principle #11 violation.

**22. Pure-logic core has a coverage floor — ratchet, never regress**
Enforced by `fail_under = 100` in `pyproject.toml`, run via `make test-cov`. When you raise coverage, raise the floor in the same PR. AWS/CLI modules are `omit`-ed (exercised by e2e/smoke). Lines unreachable through the public API get `# pragma: no cover` with a written reason — never a fabricated mock-around test.

**23. Completeness = whole-codebase impact, verified and reported**
A change is done only once its blast radius across **both repos (CE + EE)** has been swept. Before declaring done:
1. **Consumers sweep.** `grep -rn` every touched symbol (function, constant, type, enum member, field, CSS class) across CE and EE, prod and tests.
2. **Pattern sweep.** Find every other site of the same pattern — array form and OR-comparison form, backend and frontend, both repos.
3. **Producer↔consumer contract.** Both sides agree on the value. Regenerate and re-check drift.
4. **Dead-after-change.** Remove unused imports, unreachable branches, orphaned CSS rules.
5. **Full suite.** Run ALL test locations (CE + EE vitest + merged overlay), not just the feature's tests.

End with a **completeness report** — mandatory:
> Changed: A, B, C. Blast radius: swept N consumers (CE + EE + tests); M sites — migrated P, left Q because R. Contract: both sides checked. Dead code: none / removed X. Suites: all green. Not touched: Z — because R.

**24. OSS describes what OSS ships — silence about everything else**
Test: *would a user reading only this repo be able to use this?* If no, don't mention it — not as "coming soon", "available in paid", "greyed out in OSS", or any comparison. The paid overlay owns its own docs.

- Editing something? Ask "does OSS ship this?" If no, remove the reference.
- CHANGELOG, ADR, or design note discussing a non-OSS feature → write it in `polyris-ee`.
- Comparison phrases are the common violation: "X is not yet in OSS", "compared to the full Z" — the comparison itself is the violation, not just specific feature names.
- Exception: ADRs documenting how the OSS/paid boundary mechanism itself works may discuss both sides — that's the topic.

**25. Documentation: Clear / Concise / Structured + Diátaxis**

Every doc fits exactly one purpose:
| Category | Purpose | Lives in |
|---|---|---|
| Tutorial | Learning-oriented, zero context assumed | `README.md#try-it-now` |
| How-to | Goal-oriented, "how do I do X" | `docs/getting-started/`, `docs/deployment/` |
| Reference | Lookup, technical spec | `docs/features/`, `docs/reference/` |
| Explanation | Understanding-oriented, "why this design" | `docs/architecture/`, ADRs |

Detailed application: `docs/CLAUDE.md`. Read before editing anything under `docs/`.

**26. Every fix must produce a rule — not just a patch**
After fixing a bug, ask: *what rule would have prevented this?* Write it in CLAUDE.md or `ui/CLAUDE.md` in the **same commit** as the fix. A patch without a rule fixes one instance; a rule prevents the class.

**27. Enums crossing SDK ↔ backend ↔ frontend go through `polyris/constants.py` + `sync_enums`**
If a value set means the same thing in Python and TypeScript, it belongs in `polyris/constants.py` and is emitted by `python -m polyris.codegen.sync_enums`. Bare string literals across multiple files are how a new member gets added in one place and silently forgotten in others.

When adding a value: (1) add to `polyris/constants.py`, (2) register in `sync_enums.py` if new class, (3) run `python -m polyris.codegen.sync_enums`, (4) import the enum everywhere — never bare strings, (5) add a parity test (#28).

**28. Constants coupled across languages/artifacts need a parity test**
Any string written in one artifact and read verbatim in another must have a parity test asserting the same value on both sides. The type system can't enforce string parity across JSON templates or Python↔TypeScript.

Examples: `_PUSH_MARKER_FIELD` written by `xcom.push()`, read by SFN template — parity in `test_xcom_coupled_constants_parity.py`. Marker fields `_manually_resolved`/`_resolution`/`_reason`/`_operator` — same test. `SCHEDULE_PRESETS` ↔ `infer_cron_cadence` — `TestSchedulePresetsParity` in `test_granularity.py`. Generated enums covered by `codegen --check`.

**29. adr-index.md count must match its row count**
`docs/reference/adr-index.md` opens with "_N ADRs indexed (X inline, Y standalone)_". When you add or remove an ADR, update the count in the same commit:
```bash
grep -Ec "^\| [0-9]+ \| " docs/reference/adr-index.md
grep -Ec "^\| [0-9]+ \| .* \| inline \|" docs/reference/adr-index.md
```

**30. Date-scoped DDB rows are shared across same-date runs — gate UI reads on settled state**
Rows keyed by `(pipeline, task, DATE)` — `output#*`, `input#*`, manual-resolution markers — are shared across every same-date run. A UI that renders such a row unconditionally shows a prior run's content when the current task isn't settled yet.

Rule: every consumer of a date-scoped canonical row must gate on `task.status ∈ TASK_SETTLED_STATUSES`. Non-settled tasks render a "pending" empty state, not the stale row. See `OutputCard.tsx` (`!isSettled` branch) for the reference implementation.

**31. Canonical-row writers must distinguish "protect real data" from "block stale metadata refresh"**
`attribute_not_exists(#field)` alone conflates two concerns: (1) never clobber genuine user data, (2) always refresh stale metadata written by the same writer. A second same-date manual resolution must supersede the first.

Correct shape:
- **GetItem-then-Update:** read current row; if field is absent or is your own synthetic marker, do unconditional Update; if it's real user data, skip and emit `_notify_warn_*` (Principle #38).
- **Structured sentinel** (advanced, fragile): `attribute_not_exists(#field) OR contains(#field, :marker_sentinel)`.

Apply when touching shared DDB keys (date-scoped rows, cache rows, aggregation snapshots). Exclusively-owned rows (per-run task record, single-writer configs) don't need this.

**32. Break-review + architect-pass before claiming "done"**
Any significant change is not done until independently break-reviewed AND architect-reviewed:
1. Author signals "I think this is done."
2. Spawn break-review (`general-purpose` + `/break` skill) — reassurance is failure; report what couldn't be ruled out.
3. Address every finding — Closed / Partial / Still open, by file:line.
4. Spawn architect review — judge coherence, ADR quality, abstraction fit, backwards-compat.
5. Close every architect-flagged blocker before the claim reaches the maintainer.

**33. Never change `Description` on `AWS::IAM::ManagedPolicy` — CloudFormation replaces the policy and hits a name collision**
CFN treats `Description` as update-requires-replacement. Editing it → delete + create with same `ManagedPolicyName` → `EntityAlreadyExists` → rollback. Hit twice in 1.0.0 delivery.

How to apply: editing an IAM policy → change only the `Statement` block; leave `Description` and `ManagedPolicyName` alone. Before editing any CFN property on a named resource, check the AWS docs for "Update requires: Replacement".

**34. Two writers to the same DDB row must both handle "row already exists" explicitly**
Once a second writer joins a row (Lambda + SFN, or operator action + SFN), `attribute_not_exists` means "silently do nothing if someone got here first" — almost never what you want.

Before adding a second writer: answer per row: "when writer B fires and writer A's data is present — protect, overwrite, or fail loudly?" If "protect if fresh, overwrite if stale" → you need GetItem-then-Update (#31).

**35. ECS TaskDefinition and Batch JobDefinition — reference by family name, never by pinned revision ARN**
Any template change to `Command`, `Image`, etc. creates a new revision (`:2`, `:3`, …). A DAG pinned to `arn:...:task-definition/family:1` keeps calling revision 1 with its stale Command even after the template change ships. Hit in 1.0.0 smoke — `polyris-test-xcom-all-render:2` created but DAG still called `:1`.

In `@task.ecs_task(task_definition=...)` and `@task.batch_job(job_definition=...)`: drop the trailing `:N`. Only pin `:N` when deliberately holding a specific version — document why.

---

## Date picker is per-workspace, not global (ADR #106)

`useAppStore.date` is the **pipeline page's scope only**. Never read it from a workspace view (Runs, Tasks). A feed's empty date means "every date"; a page's empty date means nothing at all. Runs and Tasks each own their date filter in their own URL (`runFilter.date`, `taskFilter.date`).

---

## OSS/EE architecture patterns

### Component / plugin boundaries (ADR #97)

Where a set of extensions must be switchable per build — console routes, UI features, schema adapters — use a registry with explicit registration:
- Each component exposes `register(...)` and adds its pieces to a shared registry.
- The runner (`console_api/main.py`) holds an explicit `ROUTE_MODULES` list — **not** package discovery. Explicit over implicit.
- Prefer `register(router)` call over decorator-as-import-side-effect.

This is only for switchable plugin sets. Ordinary code uses plain functions and direct imports. A registry where a plain call would do is over-engineering.

### Open-core UI surface (ADR #99)

Free surface: `ui/src/` (everything outside `src/ee/`). Paid tiers: `ui/src/ee/team/` and `ui/src/ee/enterprise/`. `ui/scripts/gen-ee-active.mjs` generates `src/ee-active.generated.ts` — an empty stub in OSS.

Free code reaches paid surface **only** through `@/ee-active.generated`. `src/ee/` may import free modules — never the reverse. `ee/team/` never imports `ee/enterprise/`.

Tier decision:
- Authoring + basic read + running + live-run intervention → **free** (`src/components`, `src/hooks`)
- Pipeline-level lifecycle / backfill / assets / alert integrations / PATs / observability / config mutation → **Team** (`src/ee/team/`)
- Governance / cost / SSO / RBAC / cross-account → **Enterprise** (`src/ee/enterprise/`)
- Unsure between paid tiers → Team.

When adding a paid feature:
1. **Self-contained component**: add to tier's `components/views`, add typed slot to `PaidSurface` in `ee-contract.ts`, register in tier's `surface`. Free host: `{X ? <X … /> : <EeFeatureFallback/>}`. Enterprise additionally gates on `can('<capability>')`.
2. **Cross-cutting handlers woven into a free host**: render-prop provider in tier's `views`. Host: `Provider ? <Provider …>{h => content(h)}</Provider> : content(null)`.
3. **Queries**: tier's queries live in `src/ee/<tier>/hooks/queries/`. If a free component calls it, it stays in `src/hooks/queries/`. When a paid surface absorbs a query's last free caller, move the query in the same change.
4. **Tests follow tier**: paid tests → `src/ee/<tier>/`; free tests → `src/`. When a free host's test needs the surface, mock `@/ee-active.generated`'s `paidSurface`.

---

## What is Polyris

Serverless data pipeline orchestration on AWS Step Functions. Python DSL → generates ASL → deploys via CloudFormation (`polyris-deploy`).

Core idea: each task = one `dependency_wrapper` SFN execution that waits for deps, runs the task, then signals downstream tasks via `notify_dependents`.

---

## Project Layout

> Canonical full-system layout. See **Repository context** for what this repo physically ships.

```
polyris/              # Python DSL library + CLI (polyris-deploy, polyris-init)
pipelines/            # Pipeline definitions — each pipeline has a dag.py
sam/
  template.yaml       # ALL AWS resources — single source of truth
  sfn_templates/      # SFN definitions as .tpl.json — edit HERE, never in template.yaml
    dependency_wrapper/sfn.tpl.json
    helpers/
      run_task/sfn.tpl.json
      failure_handler/sfn.tpl.json
      notify_dependents/sfn.tpl.json
      registration/sfn.tpl.json
      register_pipeline/sfn.tpl.json
      restart_task/sfn.tpl.json
      restart_wrapper/sfn.tpl.json
      pause_waiter/sfn.tpl.json
      notify_asset_consumers/sfn.tpl.json
  lambdas/
    console_api/      # REST API (63 routes full build, 27 free), DAL repos, route handlers
    evaluate_deps/    # Evaluates trigger rules (all_success, all_done, etc.)
    query_subscriptions/  # Finds downstream subscribers for a completed task
    check_assets/     # Validates asset freshness for wait_for
    notify_asset_subscribers/  # Triggers asset-based pipelines
ui/                   # React 19 + Next.js 16 + Zustand 5 + React Query 5
tests/
  sdk/                # ASL snapshot tests, template tests, SFN flow tests
  backend/            # API route tests (pytest-mock, mocker fixture)
  integration/        # Integration tests
  sfn_jsonata/        # JSONata expression tests (Node.js)
```

**Pipeline files:** `dag.py` — NOT `__main__.py` (renamed in v70.x).

---

## Deployment

```bash
# Infrastructure
cd sam && sam build && sam deploy --profile <profile>

# UI only
cd ui && npm ci && npm run build && ./deploy.sh --profile <profile>

# Pipeline
cd pipelines/my-pipeline && polyris-deploy --stage dev --profile <profile>
```

**SFN edit workflow:** edit `sam/sfn_templates/*/sfn.tpl.json` → `sam build && sam deploy`. `sam build` inlines `.tpl.json` into `DefinitionString` automatically.

### Lambda packaging: `polyris` SDK comes from `requirements.txt` (ADR #102)

The console Lambda declares the SDK in `sam/lambdas/console_api/requirements.txt`:
```
polyris @ git+https://<PUBLIC_REPO_URL>@vX.Y.Z
```

**Things Claude must NOT propose:**
- ❌ Re-adding a `console_api/polyris` symlink. No repo-root `polyris/` exists after the split.
- ❌ Adding `sam/lambdas/console_api/Makefile` with `BuildMethod: makefile`. SAM CustomMakeBuilder runs from a scratch dir; relative paths can't reach the repo root. Failed live smoke 2026-05-22.
- ❌ Vendor copy via top-level `make sam-build` wrappers or `samconfig.toml` tricks. Plain `sam build` must just work from `requirements.txt`.

Regression tests: `test_lambda_packages_polyris_via_requirements`, `test_lambda_local_makefile_not_reintroduced`, `test_template_has_no_buildmethod_makefile_for_console_api`.

### DynamoDB GSI changes — one op per update

`Cannot perform more than one GSI creation or deletion in a single update` is an AWS hard limit — not a polyris bug. Don't propose code changes to "fix" it. Fix is operational: delete the old GSI manually via `aws dynamodb update-table --global-secondary-index-updates`, wait for completion, re-run `sam deploy`. Avoid renaming GSIs — add-new-first, remove-old-later as separate PRs.

---

## DynamoDB Tables

| Repo | Table suffix | PK | SK | GSIs |
|------|-------------|----|----|------|
| `executions_repo` | `pipeline-tokens` | `execution_name` | — | `pipeline-execution-index`, `date-pipeline-index` |
| `dep_subscriptions_repo` | `dep-subscriptions` | `dependency_key` | `subscriber` | `subscriber-index` |
| `pipelines_repo` | `pipeline-registry` | `pipeline_name` | — | — |
| `asset_events_repo` | `asset-events` | `asset_name` | `event_time` | `date-index` |
| `queued_events_repo` | `queued-asset-events` | `dag_date` | `asset_name` | — |
| `task_events_repo` | `task-events` | `task_run_id` | `event_time` | `run-index`, `execution-name-index` |
| `asset_subscriptions_repo` | `asset-subscriptions` | `asset_name` | `pipeline_name` | — |
| `api_tokens_repo` | `api-tokens` | `token_id` | — | `hash-index`, `owner-index` |

**CRITICAL — two separate subscription tables:**
- `dep_subscriptions_repo` / `DependencySubscriptionsTable` — task-to-task deps within pipeline
- `asset_subscriptions_repo` / `AssetSubscriptionsTable` — cross-pipeline asset triggers

Never mix these up. `query_subscriptions` Lambda reads `DependencySubscriptionsTable`. `registration` SFN writes to `DependencySubscriptionsTable`. `check_assets` uses `AssetSubscriptionsTable`.

**Key fields in `pipeline-tokens` (executions_repo):**
- `execution_name` — `{task_name}-{date}-{pipeline_execution_short}`
- `pipeline_execution` — `{pipeline_name}-run-{date}-{hex8}`
- `pipeline_execution_short` — last 20 chars of `pipeline_execution`, strips `.` and `:`
- `status` — see TaskStatus constants
- `wrapper_execution_arn` — ARN of dependency_wrapper SFN execution
- `task_execution_arn` — ARN of run_task SFN execution
- `orchestration_token` — `.waitForTaskToken` for signaling deps ready
- `wait_token` — token for notify_dependents to signal ready

**dependency_key format:** `{upstream_task_name}-{pipeline_execution_short}` — must match exactly between writer (registration) and reader (query_subscriptions).

---

## Zustand + React effect closures — URL sync gotcha (ADR #63)

When a mount-once effect schedules `store.setDate(Y)` and a push effect in the same commit reads `store.date`, the push effect reads the **stale pre-update value** — Zustand state doesn't propagate mid-commit.

**Wrong pattern:** `useRef(false)` as initialized guard → push effect's closure captures stale `store.date`, strips URL param via `pushState`, URL flickers.

**Fix:** `useState(false)` — `setIsInitialized(true)` triggers a re-render; the push effect's second firing reads fresh `store.date` from the new render's closure.

```ts
const [isInitialized, setIsInitialized] = useState(false);

useEffect(() => {
    if (isInitialized) return;
    store.setDate(urlState.date || today);
    setIsInitialized(true);
}, []);

useEffect(() => {
    if (!isInitialized) return;
    updateUrl({ date: store.date !== today ? store.date : undefined });
}, [store.selectedPipeline?.name, isInitialized]);
```

Symptom: "click X → ends up at default state Y", URL flickers to correct params then resets. Pinned by `useStoreInit.test.ts`.

---

## Step Functions

### 14 State Machines (11 orchestration + 3 test)

| Name | Type | Purpose |
|------|------|---------|
| `polyris-dependency-wrapper` | STANDARD | One per task — waits for deps, runs task, signals downstream |
| `polyris-dep-run-task-helper` | STANDARD | Executes the actual task SFN/Lambda/Glue/etc |
| `polyris-failure-handler` | STANDARD | Handles failures, notifies, updates DynamoDB |
| `polyris-registration-helper` | STANDARD | Registers pipeline on deploy |
| `polyris-pause-waiter` | STANDARD | Holds execution during pipeline pause |
| `polyris-bulk-backfill` | STANDARD | Orchestrates a range of dates for a pipeline |
| `polyris-notify-dependents` | EXPRESS | Finds and signals downstream tasks when upstream completes |
| `polyris-restart-task-helper` | EXPRESS | Stops wrapper + restarts task |
| `polyris-restart-wrapper` | EXPRESS | Starts new dependency_wrapper for restart |
| `polyris-notify-asset-consumers` | EXPRESS | Triggers asset-based pipelines |
| `polyris-register-pipeline` | EXPRESS | Registers pipeline in registry |
| `polyris-test-quick` | STANDARD | Demo/test task (fast) |
| `polyris-test-success` | STANDARD | Demo/test task (always succeeds) |
| `polyris-test-failure` | STANDARD | Demo/test task (always fails) |

### Calling SFNs from SFN

```
Standard SFN  → startExecution.sync:2              ✅
Express SFN   → aws-sdk:sfn:startSyncExecution      ✅
Express SFN (fire-and-forget) → states:startExecution ✅
Express SFN   → startExecution.sync:2              ❌ WRONG — fails silently
```

### DefinitionSubstitutions — subscriptions_table mapping

In `template.yaml`, always verify:
- `RegistrationHelperSfn.subscriptions_table` → `DependencySubscriptionsTable` ✅
- `NotifyDependentsSfn.subscriptions_table` → `DependencySubscriptionsTable` ✅
- `QuerySubscriptionsFunction.SUBSCRIPTIONS_TABLE` → `DependencySubscriptionsTable` ✅

---

## Lambda Functions

| Function | Purpose | Key env vars |
|----------|---------|-------------|
| `console-api` | All REST API endpoints | `TOKENS_TABLE`, `REGISTRY_TABLE`, etc. |
| `query-subscriptions` | Find downstream subscribers | `SUBSCRIPTIONS_TABLE` → `DependencySubscriptionsTable` |
| `evaluate-deps` | Evaluate trigger rules | `TOKENS_TABLE` |
| `check-assets` | Asset freshness checks | `SUBSCRIPTIONS_TABLE` → `AssetSubscriptionsTable` |
| `notify-asset-subscribers` | Trigger asset pipelines | `SUBSCRIPTIONS_TABLE` → `AssetSubscriptionsTable` |
| `ui-bootstrap` | Copy UI to S3 on deploy | — |

**Structured logging:** Lambda handlers use `polyris/logger.py` (`log.info`/`log.warn`/`log.error`) for JSON lines CloudWatch can parse. CLI tools (`cli.py`, `register.py`, `init.py`, `output.py`) use `print()` — correct, they are the event stream consumer. Known gap: `evaluate_deps`, `notify_asset_subscribers`, `check_assets`, `query_subscriptions`, `ui_bootstrap` still use bare `print()` — migrate opportunistically.

---

## Task Status Constants (`_shared/constants.py`)

```python
TaskStatus.TERMINAL = {SUCCESS, FAILED, SKIPPED, UPSTREAM_FAILED, ABORTED}
# NOTE: STOPPED is NOT terminal — task can be restarted
TaskStatus.SUCCESS_STATES = {SUCCESS, SKIPPED}
TaskStatus.FAILURE_STATES = {FAILED, UPSTREAM_FAILED, ABORTED}
TaskStatus.WAITING_STATES = {WAITING, WAITING_PAUSED, WAITING_DELAY, DEPS_READY, WAITING_DECISION}
```

After changing `_shared/constants.py`: run `make sync-constants` to copy to evaluate_deps.

---

## API Routes (console_api/main.py)

Routes self-register via `ROUTE_MODULES` list (ADR #97). Each module exposes `register(router)`; `main.py` exposes the assembled `ROUTES` table `(METHOD, '/api/path') → (handler_fn, 'param_key')`.

**Surface (ADR #98, #110):** 27 free routes in OSS build, 63 in full. Free = authoring + basic read + running + live-run intervention (task actions skip/fail/success/stop/restart, execution stop/pause/resume/extend). Team = pipeline-level lifecycle, backfill, assets console, alert integrations, PATs, observability, config mutation.

Path params go via query string (`/api/tokens?id=…`) — single `/{proxy+}` integration means no `template.yaml` change for new routes.

**Auth (ADR #65, #66):** `authenticate()` → `authorize()` at top of `handler()`. Accepts Cognito JWT or PAT (`plrs_…`). Scopes: `read` ⊂ `write` ⊂ `admin`. `AUTH_ENABLED` env (default `false`). Public paths: `/api/health*`, `/api/metrics`, `/api/action/*`.

When adding an endpoint:
1. **Decide the tier.** Free → `routes/<mod>.py`. Team → `ee/team/<mod>.py`. OSS must never import `ee`.
2. Add handler to module, register in `register(router)`.
3. New module? Add to `main.py`'s `ROUTE_MODULES` (free) or `ee/__init__.py`'s `MODULES` (Team). Existing module needs no `main.py` change.
4. Free handlers also go in `routes/__init__.py` barrel; Team handlers don't.
5. Tests follow tier. Update route-count guards: free-subset in `tests/sdk/test_templates.py`, full-63 in `ee/team/tests/test_route_table_ee.py`.

---

## Backend Patterns

**DAL repositories for all DynamoDB access in `console_api` (100%).** Always use DAL repos:
```python
from dal import executions_repo, pipelines_repo
# Never: boto3.resource('dynamodb').Table('...')
# Never: repo.table.get_item(...)  — use repo.get(key)
```

One deliberate exception: `notify/registry.py` has no `dal/` and one table accessor — `registry_table()` is the accessor. A DAL for a single function would be over-engineering.

**GSI INCLUDE projections:** when a field is absent from `NonKeyAttributes`, fetch from base table with `executions_repo.batch_get_triggered_by` (chunks at 100, retries `UnprocessedKeys`). Never loop N individual `GetItem` calls.

**Error handling:**
```python
from botocore.exceptions import ClientError, BotoCoreError
try:
    result = executions_repo.get(key)
except (ClientError, BotoCoreError) as e:
    log.error("context", "message", error=str(e))
    return cors_response(500, {'error': str(e)})
```

**Re-raise permission errors** — never return `{}` or `[]` silently on AccessDeniedException.

**Stop pipeline hierarchy** (always stop in this order):
1. Pipeline SFN execution (reconstruct ARN: `sfn_arn.replace(':stateMachine:', ':execution:') + ':' + pipeline_execution`)
2. All `wrapper_execution_arn` from task items (deduplicated)
3. All `task_execution_arn` from task items
4. Update DynamoDB → `stopped`/`aborted`

---

## Error Visibility (ADR #38)

**Lambda rules:**
- Never swallow `AccessDeniedException` — always `raise`
- Every `except Exception` must log `error=str(e)` with function context
- If an error blocks downstream tasks → write `_notify_warn_` record to `pipeline-tokens`

**`_notify_warn_` pattern** (infrastructure errors → Notifications bell in UI):
```python
executions_repo.put({
    'execution_name': f'_notify_warn_{execution_name}',
    'task_name': task_name,
    'pipeline_execution': pipeline_execution,
    'pipeline_name': pipeline_name,
    'date': date,
    'status': 'failed',
    'error': f'Context: {error}',
    'finished_at': datetime.now(timezone.utc).isoformat(),
    'ttl': int(datetime.now(timezone.utc).timestamp()) + 86400
})
```

**Special prefixes in `pipeline-tokens`** — internal records:
- `_pause_{pipeline_execution}` — pause state
- `_notify_warn_{execution_name}` — infrastructure warning

**CRITICAL:** All loops iterating `pipeline-tokens` items MUST filter `_` prefixed records:
```python
for item in items:
    if item.get('execution_name', '').startswith('_'):
        continue
```
Violating this → `_notify_warn_` appears in All Tasks / pipeline status / runs.

**SFN templates:** `Catch` blocks must preserve `$states.errorOutput` in Output.

---

## UI Patterns

- React Query only for data fetching (`useQuery` in `hooks/queries/`)
- Add `queryKey` to `lib/queryClient.tsx` for new queries
- Components never import `api` directly — use hooks
- Styling: shadcn primitives only, no other UI libraries (mui/chakra/mantine/antd = 0). Tailwind CSS for utilities; BEM-prefixed global CSS for surface-specific bits. No `.module.css`.
- Icons: Lucide React via `icons.tsx`
- Runtime config: `window.CONFIG` (set by `/config.js`) is source of truth, `NEXT_PUBLIC_*` is build fallback — `getConfig()` is window-first; use `??` for booleans (ADR #94)
- **Status constants are for orchestration only.** `TASK_SUCCESS_STATUSES` etc. encode which statuses unblock downstream tasks. Display code (counters, labels, badges) uses explicit checks: `t.status === 'success'`, never `TASK_SUCCESS_STATUSES.includes(t.status)`. See `ui/CLAUDE.md` for details.
- `ErrorBoundary.tsx` is the only sanctioned class component — React's error boundary API has no hook equivalent. Don't write any other class components.
- TypeScript strict mode is on (`tsconfig.json`). When you reach for `any`, stop — use `unknown` + narrow, or write the type out.

### Responsive Layout (ADR #40)

UI must work on desktop (≥1024px), tablet (≤1024px), and mobile (≤768px / ≤480px).

- Use **flex/grid chains** (`flex: 1; min-height: 0`) for height inheritance — never `calc(100vh - Npx)`
- Heavy components (ReactFlow, Gantt, charts): `height: 100%` + parent flex chain + baseline `min-height`
- Tables wrap in `.table-full`; tabs that may overflow: `overflow-x: auto; flex-wrap: nowrap; scrollbar-width: thin`
- Mobile breakpoints defined in `_mobile.css`: ≤1024px tablet, ≤768px mobile, ≤480px small

Before merging UI changes:
1. No `calc(100vh - Npx)` / `calc(100vw - Npx)` magic numbers
2. New full-screen views verify mobile rules in `_mobile.css`
3. New modals: use `BaseModal` or add own `@media` queries
4. Tap targets ≥36px; no fixed `width: Npx` on top-level containers

---

## SDK / Generators

After changing `generators.py`:
```bash
SNAPSHOT_UPDATE=1 python -m pytest tests/sdk/test_asl_snapshots.py tests/sdk/test_asl_snapshots_steps.py
python -m pytest tests/sdk/test_asl_snapshots.py  # verify
```
60 snapshot tests total: 33 Task + 27 Step, 28 golden files.

---

## Testing

```bash
# Before every delivery — must all pass
python3 -m pytest tests/sdk/ tests/backend/ -q    # ~1310 tests
cfn-lint sam/template.yaml                         # 0 errors

# Full suite
python3 -m pytest tests/ -q
cd ui && npx vitest run

# Core coverage floor
make test-cov
```

**Verified invariants** (enforced by `tests/sdk/test_claude_md_claims.py`):
- No wildcard imports (0 across all `.py` files)
- DAL repositories for all DynamoDB access in `console_api` (100%)
- TypeScript strict, no `any` in production code (100%)
- No `.module.css` files (0); components never import `api` directly (100%)
- Type hints on public Python functions (91%) floor — measured continuously by `test_claude_md_claims.py`

**Test rules:**
- pytest-mock (`mocker` fixture) everywhere — no `unittest.mock` import at all, including `MagicMock` as a bare object factory (use `mocker.MagicMock`)
- Backend tests in `tests/backend/`, SDK tests in `tests/sdk/`
- Coverage floor is a ratchet — raise the floor when you raise coverage (#22)
- **`dal/__init__.py` submodule shadowing:** `from dal.executions_repo import executions_repo` replaces the submodule attribute on the `dal` package with the singleton instance. Tests patching module-level globals on a `dal` submodule must use `importlib.import_module('dal.executions_repo')` to get the actual module object.

---

## SFN Templates — Common Pitfalls

1. **`.tpl.json` is not valid JSON** — `${var}` breaks `json.load()`. CI strips numeric vars with regex.
2. **Express SFN via sync:2 fails silently** — use `startSyncExecution` instead.
3. **DefinitionUri vs inline** — always edit `sfn_templates/`, never inline in `template.yaml`.
4. **pipeline_execution_short** — last 20 chars of `pipeline_execution`, with `.` and `:` stripped. Format: `-{date}-{hex8}`.
5. **dependency_key** — `{task_name}-{pipeline_execution_short}`. Must match exactly between writer (registration) and reader (query_subscriptions).

---

## Documentation

After significant changes:
- `CHANGELOG.md` — new version entry
- `docs/reference/DESIGN_DECISIONS.md` — new ADR
- Version must match across: `pyproject.toml`, `polyris/__init__.py`, `ui/package.json`

---

## Agentic workflow tooling

`docs/tools/AGENTIC_WORKFLOW.md` documents the `/shape` → `/build` → `/break` loop, the Stop hook, and the destructive-command guard. If you change a checkable claim here, update `tests/sdk/test_claude_md_claims.py` in the same change.

---

## Archive Delivery

```bash
# CE (public)
cd /home/claude/work && zip -qry -y /mnt/user-data/outputs/polyris-public.zip polyris
# EE (private — ee/ overlay only)
cd /home/claude/work && zip -qry -y /mnt/user-data/outputs/polyris-ee-private.zip polyris-ee
```

Releases are cut from git tags. CE tag `vX.Y.Z` builds PyPI + self-hosted bundle; EE tag `platform-vX.Y.Z` overlays `ee/` onto CE@`CE_REF`. See `polyris-ee/docs/development/DEV_AND_RELEASE.md`.

---

## Key ADRs (read before touching these areas)

| # | Topic | Decision |
|---|-------|---------|
| 22 | Data source | UI reads DynamoDB only, never SFN API |
| 23 | DAG lookup | snapshot → registry → inferred |
| 24 | Registration | `polyris-deploy` boto3 call, not EventBridge |
| 26 | Testing | pytest-mock (`mocker`) everywhere |
| 28 | Exceptions | `(ClientError, BotoCoreError)` for AWS calls; route-level catch-all OK |
| 34 | Infrastructure | AWS SAM (not Terraform/OpenTofu/Pulumi) |
| 35 | Pipeline deploy | `polyris-deploy` (CFN) replaces Pulumi |
| 37 | SFN definitions | `DefinitionUri` + `AWS::Serverless::StateMachine` |
| 38 | Error visibility | `_notify_warn_` records + always log `error=str(e)` |
| 39 | Assets | pipeline_registry is source of truth, asset_registry removed |
| 40 | Responsive layout | Flex chains, no viewport magic numbers; mobile rules in `_mobile.css` or per-component `@media` |
| 41 | URL routing | CloudFront Function rewrites + `window.history` for per-route deep state |
| 94 | Runtime config | `window.CONFIG` (from `/config.js`) wins over baked `NEXT_PUBLIC_*`; `getConfig` is window-first, `??` for booleans |
