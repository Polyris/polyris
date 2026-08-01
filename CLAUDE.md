# CLAUDE.md — Polyris Agent Instructions

Read this fully before writing any code. When in doubt, `grep` first.

---
## Repository context

**You are in `polyris` — the public, open-core repo.** It ships the free SDK
(`polyris/`), the free console backend (`sam/lambdas/console_api/`), and the free
UI (`ui/src/`, everything outside `src/ee/`). This repo is the **single source of
truth for the core** — the private `polyris-ee` repo depends on it, never the
reverse.

- **Never add proprietary/paid code here.** The AI assistant (`polyris/_ee/`),
  paid console routes (`console_api/ee/`), and paid UI (`ui/src/ee/`) live only in
  the private `polyris-ee` repo. `polyris._ee*` is excluded from the wheel
  (`pyproject.toml`) and there is no `ee/` directory in this checkout.
- **The seam is enforced, not conventional.** `main.py` registers only the free
  `ROUTE_MODULES` and appends paid routes via `try: import ee` (absent here, so the
  OSS build serves the free routes only). Free UI reaches any paid surface solely
  through the generated `@/ee-active.generated` stub (empty in this repo).
  `ui/scripts/check-oss-build.sh` proves in CI that the UI typechecks + builds with
  `src/ee/` absent — a stray `@/ee/…` import fails the build.
- **Build/test here is self-contained:** `pip install -e ".[dev]"` then
  `python -m pytest tests/sdk tests/backend tests/integration`; for the UI,
  `cd ui && npm ci && npm run typecheck && npm test -- --run && npm run build`.

Adding a feature? Decide the tier first (see **Open-core UI surface** below):
authoring + basic read → free (here); operations / observability / governance →
paid (the private repo).

---
## Core Principles

These principles take priority over everything else. Read before every change.

**1. No duplication**
One source of truth for every concept. Before creating anything — check if it already exists.
`grep` before writing new code, new components, new constants.

**2. Follow existing patterns**
Follow established patterns in the project. If there's an existing way — use it.
Don't invent new approaches without need. DAL repos, pytest-mock, cors_response, log.error — all already exist.

**3. Idempotency**
Every operation must be safe to repeat.
DynamoDB conditional updates, SFN `_sfn_stop()` that ignores already-stopped — these are examples.

**4. Stability and reliability first**
When implementing a feature or fix: first "don't break what works", then "add the new thing".
Happy path must not suffer from error path changes and vice versa.
Every change goes through: `pytest` + `cfn-lint` + syntax check.

**5. No fix on top of a fix — do it right**
If the solution is unclear or there are doubts — **ask first** before writing code.
Better to spend a minute discussing than an hour fixing the wrong approach.

**6. Align on architectural decisions**
Any change that affects: DynamoDB schema, SFN flow, API contract, file structure,
deployment approach — requires explicit approval before implementation.
After alignment — document in ADR (`docs/reference/DESIGN_DECISIONS.md`).
Research, comparing options, and reading other projects' source code are
inputs to a good decision — do them freely, then bring findings to the
alignment conversation.

**7. Finish what you start**
Never leave work half-done. Every task must be completed fully:
code, tests, documentation, CHANGELOG. If something objectively cannot be finished —
document exactly what remains and why (backlog, TODO, comment).

**8. Asked a question — answer. Asked to do — do**
If asked a question — give the answer, don't jump into doing things.
If asked to do something — do it. If something is unclear — ask, don't decide on your own.

**9. Always keep documentation up to date**
Every change that affects behavior, API, config, or architecture must be reflected
in the relevant docs in the same delivery. Stale docs are bugs.

**10. Documentation and code comments are always and only in English**
All documentation, code comments, ADRs, README files, and CHANGELOG entries
must be written in English. No exceptions.

**11. No stubs, mocks, or "make it pass" workarounds in production code**
Never insert placeholder values, hardcoded dummy data, commented-out logic,
or shortcut paths that make something "kind of work" without actually solving the problem.
This includes:
- Returning empty/dummy values when the real logic is hard
- Skipping validation, auth, or error handling "for now"
- Disabling, skipping, or weakening assertions in failing tests instead of fixing the underlying issue
- Hardcoding values that should be derived from configuration/state
- Adding `# TODO: implement` next to fake return values that ship to production
- Catching and ignoring exceptions to mask bugs

If something cannot be implemented properly right now: stop, document the gap explicitly
in `STATE.md` (Decided, not done) or the relevant doc, and discuss with the user
before shipping. A clearly-documented gap is acceptable. A silent fake that "works" is not.

Test mocks (pytest-mock `mocker` fixture, vitest `vi.fn()`) are different — they belong
in `tests/` and `*.test.tsx` files only, never in production code paths.

**12. Maximize reuse — check before going custom**
Before creating anything new (component, helper, CSS class, constant, util function,
modal, button style, etc.) — actively search for what already exists that could be
reused or extended. Custom one-off implementations are a last resort, not a first reach.

The same applies to whole modules and subsystems — before building a type system,
parser, validator, or DSL from scratch, check the Python ecosystem (`pyarrow`,
`pydantic`, `sqlalchemy`, `jsonschema`, etc.) and what comparable projects
(Dagster, dbt) do. Use directly when it fits, as a bridge when dependency
cost is too high (see ADR #42 for the bridge pattern), build from scratch only as
a last resort. Document the trade-off in the relevant ADR.

Concrete workflow before writing new code:
- `grep` for similar names, similar functionality, similar selectors
- Check existing primitives: BaseModal, action-btn, nav-tab, .dag-container card style,
  cors_response, asStringArray, log.error, DAL repos, util icons, format helpers
- If a similar thing exists but doesn't quite fit → **extend or generalize it**, don't fork it
- If multiple places do the same thing inline → **unify into a shared helper/component**
- Only create custom when nothing reusable exists AND the new thing won't itself become
  yet another duplicate

Examples of unification done right in this project:
- `cors_response()` — one helper, all 63 routes use it
- `BaseModal` — one wrapper, all modals consume it
- `.dag-container` card style — reused for `.alf-lineage-flow-container`, `.adp-page`,
  `.av-catalog`, `.av-recent-events-panel` (instead of writing a new card style each time)
- DAL repository pattern — one base, all entities reuse the contract
- `task_variables.py` — single schema, no parallel copies

Examples of what this principle forbids:
- Writing a new `display: flex; align-items: center; gap: 0.5rem` inline class instead of
  using the existing `.flex-row-center` utility
- Creating a second "warning banner" component when one already exists
- Inlining the same date-format logic in 3 components instead of extracting `formatTime()`
- A new `card-rounded-bordered` CSS class when `.dag-container` already encodes that look

When in doubt — ask: "does something like this already exist?" then `grep` to confirm.

**13. Tests must verify integration contracts, not just function calls**

Mock-based unit tests fail silently when they verify "function X was called with Y"
without checking whether Y is valid in the real system. A test that mocks
`query_by_date` returning `[]` doesn't verify that the DDB field name in the
filter is correct, or that the field exists at all.

Tests must pin **integration contracts**, not just internal logic:
- DDB field names live as named constants imported by both production code and
  tests. If a SFN template writes `status` to a DDB row, the constant
  `StatusField = 'status'` is imported by the writer, the reader, and the test —
  changing it in one place is caught by failing tests in others.
- Schema fixtures captured from real AWS responses (one-time `sam logs` capture,
  reused forever). Parametrize over them.
- Snapshot tests for critical contracts: API route table, SFN template structure,
  DDB schema, UI type definitions.

Failure mode to avoid: a green test suite where a deployed change silently doesn't
work because the unit tests mock around the actual bug.

**14. Mock at the external boundary, never at internal logic**

If a test mocks an internal helper, it's testing "I called the helper", not
"the system works". The mock boundary belongs at external dependencies:
- AWS SDK (`boto3.client('dynamodb')`, `boto3.client('stepfunctions')`)
- HTTP I/O (`urllib.request.urlopen`, `requests.get`)
- File I/O (`open()`, `pathlib.Path.read_text()`)
- Time/randomness (`datetime.now`, `uuid.uuid4`)

Everything inside that boundary should be real code running real logic. For DDB,
use `moto` (in-memory fake AWS API) — production code calls `boto3` normally,
moto intercepts at the SDK level, behavior is real.

Anti-pattern:
```python
mocker.patch('routes.backfill._scan_completed_partitions', return_value=set())
# Tests nothing useful — it tests the mock, not the code.
```

Pattern:
```python
mocker.patch('boto3.client')  # boundary mock
# OR with moto:
@mock_dynamodb
def test_real_flow():
    table = boto3.resource('dynamodb').create_table(...)
    # Real production code path executes against the fake table.
```

**15. Smoke test happy paths before tagging a release**

CI can't reach AWS, but a release tag without a live smoke is a leap of faith.
Before any `vX.Y.Z` tag:
1. Deploy current main to a dev account
2. Run `pytest -m smoke` against the live deployment
3. Manually exercise one happy path per new feature in the UI

Tests marked `@pytest.mark.smoke` live in `tests/e2e/` and are skipped without
`POLYRIS_API_URL`. They're not optional before release; they're the gate.

The lesson from v0.78's discovery: a test suite that's 100% green on mocks
silently shipped a `pipeline_status` vs `status` field-name bug. One smoke test
that hit real DDB would have caught it in 30 seconds.

**16. New API endpoints require e2e tests in the same delivery**

If a new route shows up in `routes/__init__.py`, a new test must show up in
`tests/e2e/test_*.py` in the same change. This is part of Principle #7
(Finish what you start) — an endpoint without an e2e test is half-shipped.

The e2e test verifies:
- 200/202 response on a happy-path payload
- Basic response shape (keys exist, types match)
- 4xx behavior on at least one invalid input
- 404 behavior for non-existent IDs (where applicable)

E2E tests can be skipped without AWS (decorated with `_skip_if_no_api()`), but
they must exist in the codebase. A skipped test that runs once is infinitely
more valuable than a missing test that never runs.

**17. Verify integration with the rest of the application on every change**

Local correctness ≠ system correctness. Every change — even a one-line edit —
must be evaluated against the rest of the codebase before commit:

- **What else reads this?** If you rename a DDB field, what Lambda, SFN, UI hook,
  or e2e test references it? `grep -r '<field_name>'` widely.
- **What else writes this?** If you change a record shape, what writes records
  with the old shape, and is migration needed for existing rows?
- **Backward compatibility?** Deployed Lambda code is reading old SFN templates
  during the SAM deploy window. Stage your changes so reader-after-writer is
  always safe.
- **Snapshot/integration tests still pass?** If you touch SFN templates, run
  `pytest tests/sdk/test_asl_snapshots.py`. If you touch UI types, run
  `tsc --noEmit`. If you touch CSS class names, run vitest for component tests.

Critical zones where #17 applies most strongly:
- DDB schema changes — read by N Lambdas, written by M SFN states, queried by K
  UI hooks
- API contracts — Lambda handler + UI hook + CLI command + e2e test must all
  agree on payload/response shape
- Shared constants (`BackfillStatus`, `BackfillLimits`) — used by handler, SFN
  template, UI types, tests
- BEM CSS renames — referenced by `.tsx`, `.css`, `.test.tsx`, possibly inline styles
- ADR-governed contracts — changing an ADR-documented behavior requires updating
  the ADR in the same delivery

Concrete workflow before commit:
```bash
# What else uses this symbol?
grep -rn 'old_field_name' --include='*.py' --include='*.tsx' --include='*.json' --include='*.md'
# What snapshot tests depend on the changed surface?
pytest tests/sdk/test_asl_snapshots.py tests/sdk/test_templates.py
# What e2e tests cover this?
grep -l 'route_name\|endpoint_path' tests/e2e/
```

The discipline costs 30 seconds. Missing it costs an outage.

**18. Don't build features for hypothetical use cases**

If there's no real user / scenario / measured need today, the feature
doesn't ship — even if it looks "easy" and "future-proof". YAGNI ("you
aren't gonna need it") wins by default.

The pattern to avoid: "I see a hardcoded number, let me make it tunable
via SSM/env-var/config-file so future operators have flexibility." If
the hardcoded number has a real reason (AWS quotas, business invariants,
algorithmic bounds) — the tunability is overhead with no payoff. Edit
the file + redeploy is one command; a tunability layer is config schema,
IAM permissions, caching logic, validation, tests, documentation, and
forever support.

Concrete examples from this codebase (don't repeat):
- `MAX_PARALLEL = 10` exists because AWS StartExecution burst limit is
  200/sec; 10 concurrent × ~45ms iteration = ~220/sec peak — calculated
  bound, not a magic number. Tunability would be premature.
- Hardcoded `TTL_DAYS = 30` matches AWS execution retention by design.
  Tunability would create drift between Backfill records and child
  executions in DDB.
- Hardcoded `PREFLIGHT_MAX_PARTITIONS = 100` exists because >100 means
  >100 DDB queries on a 29s API GW timeout. Tunability would invite
  setting it to 1000 and hitting the timeout in production.

Before proposing tunability as a "quick win," ask:
1. Who is the user who wants to change this **today**? (Not "someone
   might want…")
2. What's the **cost** of edit + redeploy vs the **cost** of supporting
   a tunability layer forever?
3. Is the existing value **justified** (calculated bound) or
   **arbitrary** (picked because seemed reasonable)?

Tunability is only worth adding when (1) has a concrete answer, (2)
favors tunability, and (3) shows the existing value is arbitrary.

Quick wins from competitive reviews are **proposals**, not obligations.
Each proposal goes through this filter before implementation; the
"don't build it" outcome is as valid as the "build it" outcome.

**19. Keyboard shortcuts on every new surface (ADR #64, revised #64.1)**

When adding a new view, page, modal, tab container, or list with filter
controls, wire the standard shortcuts before merge. The standard
mapping is fixed by surface type:

- **Top-level navigation** (App.tsx only): numeric keys `1`-`9` switch
  between primary views (`1`=Pipelines, `2`=Assets, `3`=Tasks,
  `4`=Runs, `5`=Backfills). **These keys are reserved at this level —
  no other surface may bind them.**
- **List views** (BackfillsListPage, AllTasksView, AllRunsView, etc.):
  `⌘R` refresh, `/` focus filter, `j`/`k` next/prev row, `Enter` open
  highlighted row.
- **Detail pages** (BackfillDetailPage, AssetDetailPage, etc.):
  `⌘R` refresh, `Esc` back to list.
- **Multi-tab containers within a page** (TaskDetailModal,
  AssetDetailPage tabs, PipelineDetail viewModes, HelpModal): **letter
  keys matching the first letter of each tab name** (e.g., `d`=DAG,
  `g`=Gantt, `c`=Calendar; `o`=Overview, `s`=Schema). When the first
  letter is taken by another tab in the same surface, pick a short
  memorable alternative.
- **Modals with primary action** (BackfillModal, ConfirmModal-style):
  `Esc` close (typically inherited via BaseModal), `⌘↵` (ctrl+enter)
  submit the primary action.

After wiring, **update `HelpModal::KeyboardShortcutsTab`** to reflect
the new bindings. The user-facing shortcut list and the actually-wired
shortcuts MUST stay in sync — drift here is a documentation bug.

Implementation:
- Use `useKeyboardShortcuts` hook from `ui/src/hooks/useKeyboardShortcuts.tsx`
  with constants from the `SHORTCUTS` catalog where applicable. For
  surface-specific letter keys, inline string literals are fine — the
  catalog is for cross-surface concerns.
- Don't add raw `document.addEventListener('keydown', …)` in components.
- Inline `onKeyDown` for `Enter`/`Space` on individual focusable elements
  (table rows, buttons, sidebar items) is **accessibility**, not
  shortcut — that stays as-is.

Before adding a new shortcut, **check for conflicts**:
1. `grep -rn "'<key>'" ui/src` — any existing handler for this key?
2. App.tsx global nav uses `1`-`5`. Never re-bind at non-App surface.
3. `SHORTCUTS` catalog reserves `j`/`k`/`/`/`?`/`⌘R`/`⌘K`/`Esc`/`⌘⇧T` —
   re-binding requires removing the original first or namespacing by
   surface (e.g., `c` in AssetDetailPage = Checks is fine because
   `SHORTCUTS.COLLAPSE` is unwired globally).

The rule means: a PR adding a new list view without `⌘R` wired is
incomplete in the same sense as a PR adding a new endpoint without an
e2e test. **A PR that adds a numeric shortcut at a non-App surface is
broken** and must be rejected.

**20. Top-level view sync across three files (ADR #65)**

When adding a top-level view (something that appears as a tab in
Header.tsx and maps to a Next.js route `/{view}/`), update **all three**
locations in the same PR:

1. `ui/src/types/index.ts` — `MAIN_VIEWS` and `MainView` type
2. `sam/template.yaml` — regex in `ConsoleUiUrlRewriteFunction.FunctionCode`
   (alternation `(pipelines|assets|tasks|runs|backfills|...)`)
3. `ui/src/app/page.tsx` — `validViews` array for legacy `?view=` redirect

**If any of the three is missed**, the symptom is "click the new tab,
get redirected to /pipelines/". This is caused by S3 returning 404 for
the directory path → CloudFront `CustomErrorResponses` masquerading as
200 with `/index.html` → RootPage default redirect. The URL flickers
briefly to `/newview/` then settles on `/pipelines/`. Easy to miss
during smoke testing.

Deploy contract: both backend (`sam deploy`) and frontend
(`deploy-ui.sh`) must ship together. Shipping only the frontend with a
new view doesn't help — CloudFront still 404s.

The CloudFront function code has an inline `⚠️` comment pointing to
the other two files. Don't remove it.

**21. The build must be clean — zero errors and zero warnings**
"It compiles" is not the bar; "it type-checks, lints, builds, and tests clean — with no
warnings" is. Before declaring any change done, the full toolchain must be green with
nothing flagged: the Python and UI checks listed in **Repository context** (`ruff check`,
`mypy`, `pytest`; `eslint`, `tsc --noEmit`, the production build, `vitest`). A warning is
not "fine for now" — a drifting warning count is how real errors hide in the noise.

When something is flagged, resolve it rather than ignore it: either fix the underlying
issue, or — only when the flagged pattern is genuinely correct (e.g. a strict new lint
rule over-flagging a deliberate pattern) — suppress it at the narrowest scope with a
written justification (`// eslint-disable-next-line <rule> -- <why this is safe>`, a
scoped rule in the lint config, or the pytest/ruff equivalent). Blanket-disabling a rule
across the codebase, or suppressing a warning to mask a real bug, is a Principle #11
violation — not a clean build.

**22. Pure-logic core has a coverage floor — meaningful tests, not a vanity number**
The pure-logic core (DSL, generators/codegen, validation, schema, steps, partitions,
assets, dag, task — everything deterministic and AWS-free) carries a CI coverage floor,
enforced by `[tool.coverage.report] fail_under` in `pyproject.toml` and run via
`make test-cov`. The floor is a **ratchet**: when you raise coverage, raise the floor in
the same PR — it must never regress.

The floor is 100% of the *measured* core, not of the whole tree. AWS/CLI/deploy/build
modules (`deploy`, `register`, `init`, `local`, `output`, `roles`, `codegen/sync_enums`,
and the `codegen/check_*` drift-checkers) are in the coverage `omit` list — they're
exercised by e2e/smoke (#15, #16), and mocking AWS or the filesystem just to colour their
lines would only test the mock (#11, #13, #14). For the logic that *is* under the floor,
the rule against mock-gaming still governs *how* 100% is reached: a line that cannot be
driven by a real test through the public API gets a `# pragma: no cover` with a written
reason, never a fabricated mock-around test. Coverage measures whether real behaviour is
exercised — read it that way.

Per the maintainer's directive the measured core is now at **100%** — reached by testing all real logic and marking the irreducibly-untestable lines (`__main__` guards, defensive branches unreachable through the public API, import guards for installed optional deps, external-lib/AWS factories needing boto3/pyiceberg) with `# pragma: no cover` plus a written reason. Fabricated mock-tests to colour lines stay forbidden (Principles #11/#13). AWS/CLI modules in the omit list remain e2e-covered. Current floor: **100%** — `fail_under = 100` enforces that every measured (non-omitted) module stays fully covered; new logic must arrive with a test or a justified pragma. Every measured module — generators, validation, assets, schema, steps, partitions, resolver, dag, task, task_group, granularity, helpers, config, cli, constants, xcom, and the pydantic/pyarrow/glue adapters — is at 100%.
Frontend mirror: vitest coverage on logic/hooks/reducers follows the same ratchet
philosophy; purely presentational components are exempt. (The frontend gate is wired on
once a baseline is measured — until then the rule stands as policy.)

**23. Completeness = whole-codebase impact, verified and reported — not feature-local green**

Work is never "done" after verifying only within the feature/fix boundary. #17 says to
*evaluate* system impact; #23 makes it a **required gate with a written report**. A change
is complete only once its blast radius across **both repos (CE + EE)** has been swept and
the sweep is shown. Repeatedly, real issues surface only "at the edge" — the missing step
was always a systematic radius sweep, not effort in the moment.

Before declaring any change done, run and record all five:

1. **Consumers sweep.** For every symbol you touched (function, constant, type, enum,
   enum *member*, field, CSS class, component, prop), `grep -rn` all references across
   **CE and EE**, prod and tests — not just the file you edited. A changed symbol with an
   unswept consumer is an unfinished change.
2. **Pattern sweep.** If you unified / canonicalized / renamed anything, find **every**
   other site of the same pattern — array form *and* OR-comparison form, backend *and*
   frontend, both repos. Migrate them, or list each one explicitly with the reason it is
   intentionally left (custom grouping, different styling, genuinely out of scope).
3. **Producer↔consumer contract.** If the change crosses an API / DDB / codegen boundary,
   verify **both** sides agree (backend emits X ⇒ every enum / type / UI consumer accepts
   X, and vice-versa). Regenerate and re-check drift.
4. **Dead-after-change.** After editing, confirm nothing became dead: unused imports,
   now-unreachable branches, orphaned CSS rules, enum members no value can produce. Remove
   them in the same change.
5. **Full suite, never isolated.** Run **all** test locations (discover them with `find`
   for `pytest.ini` / `conftest.py`; CE + EE vitest; the merged EE overlay), not just the
   feature's own tests. Mock and contract regressions are invisible in an isolated run and
   surface only in the full suite.

Then end with a **completeness report** — mandatory, not optional:
> Changed: A, B, C. Blast radius: swept N consumers of each (CE + EE + tests); M sites of
> the pattern — migrated P, left Q because R. Contract: both sides checked. Dead code: none
> (or: removed X). Suites: all green (list them). Not touched: Z — because R.

No report ⇒ not done. The maintainer may reject any "done" that lacks it, or simply ask
**"show me the blast radius."** This gate reduces misses; it does not promise zero — which
is exactly why the explicit report exists: so verification is auditable, not trusted blindly.

---



## Date picker is per-workspace, not global (ADR #106)

The date scopes a workspace's data, so its control lives with that data — and, just as
importantly, so does its **state**. Assets are current-state (no date; per-date history is
on the Team matrix); the pipeline page scopes the date inside its execution-history drawer;
and Runs and Tasks each own their date filter (`runFilter.date` / `taskFilter.date`, both in
their own workspace's URL). There is no global date picker left.

`useAppStore.date` is now the **pipeline page's scope, and nothing else**. Never read it
from a workspace view. The two are not the same idea and cannot share a value: a feed's
empty date means "every date", a page's empty date means nothing at all. While Runs drove
the global one, clearing its filter left the pipeline page scoped to a day called `''` —
a bare graph, no banner, and the run it should have shown sitting right there.

## Coding Philosophy

The 12 Core Principles above are **hard rules** — violations get
flagged at review and the PR doesn't merge. This section captures
**taste** — the broader style frame that explains *why* the rules
exist and how to make judgment calls in cases the rules don't cover.

### Python

Three layered references, applied in this order when they conflict:

1. **PEP 8 + Google Python Style Guide** — surface conventions: naming
   (`snake_case` for vars/funcs, `PascalCase` for classes,
   `UPPER_SNAKE` for constants), 4-space indent, 80-100 char lines,
   docstrings on every public function/class, type hints everywhere
   non-trivial, import ordering (stdlib → third-party → local).
   *In this codebase*: see `polyris/`, `sam/lambdas/console_api/`.
   `pytest`, `pyright`, and `ruff` enforce most of it.

   **CLI argparse handlers exception**: `cmd_*` functions in
   `polyris/cli.py` and similar entry-point dispatchers may omit
   docstrings when they only unpack argparse args and delegate to a
   documented function. The help text on the argparse subparser IS
   the user-facing documentation; a function docstring would duplicate
   it. Apply this exception sparingly — anything with real logic still
   needs the docstring.

2. **The Zen of Python (PEP 20)** as taste guide. The lines that bite
   hardest in serverless data pipelines:
   - *Explicit is better than implicit.* Don't `from x import *`. Don't
     rely on dict key insertion order across Python versions (we do —
     it's guaranteed since 3.7, but if you find yourself depending on
     it, write it down).
   - *Errors should never pass silently. Unless explicitly silenced.*
     Hard rule #4 above; restated here because it's the single biggest
     source of "wait, why didn't this fail" debugging sessions.
   - *Special cases aren't special enough to break the rules. Although
     practicality beats purity.* Both matter; the second wins when the
     cost of "purity" is concrete and the cost of "practicality" is
     vague.
   - *There should be one — and preferably only one — obvious way to do
     it.* Drives Hard rule #2 (no duplication). When two parts of the
     codebase do the same thing differently, one of them is wrong.
   - *Now is better than never. Although never is often better than
     *right* now.* The ADR/BACKLOG split — write down "we'll do X
     later" instead of shipping a half-done X today.

3. **12-Factor App** for application architecture, with serverless
   adaptations:
   - *Codebase*: two repos — public `polyris` (CE: free SDK,
     console backend, UI) and private `polyris-ee` (paid `ee/`
     overlay). EE depends on CE via the `polyris` package; CE never
     depends on EE.
   - *Dependencies*: `pyproject.toml` (SDK) + `requirements.txt`
     (Lambdas) + `package.json` (UI). Pinned exactly, never `latest`.
   - *Config*: environment variables + CloudFormation parameters.
     Never hardcode bucket names, table names, stage names.
   - *Backing services*: DynamoDB tables, S3 buckets, SFN ARNs treated
     as **attached resources** — referenced by ARN passed in
     env vars, never assumed by hardcoded name.
   - *Build, release, run*: `sam build` (build) → `sam deploy` (release)
     → Lambda invocation (run). Each stage has a clean boundary.
   - *Processes*: Lambdas are **stateless**. No in-memory cache across
     invocations (cold-start state OK; warm-state state is a bug).
   - *Concurrency*: Lambda + SFN concurrency model handles this. Don't
     write Python `threading` code; let the platform parallelize.
   - *Disposability*: every Lambda must boot fast (cold start budget
     <1s) and shut down clean (no orphaned HTTP connections, no
     half-written DDB transactions — use transactWriteItems for atomic
     multi-item updates).
   - *Logs*: structured JSON to `stdout`, picked up by CloudWatch.
     Use `polyris/logger.py` (`log.info`/`log.warn`/`log.error`) in
     Lambda handlers — these emit JSON lines that alarms and
     dashboards can parse. `console_api` follows this fully (~160
     `log.*` calls). **Known gap (BACKLOG)**: a handful of small
     Lambdas — `evaluate_deps`, `notify_asset_subscribers`,
     `check_assets`, `query_subscriptions`, `ui_bootstrap` — still
     use bare `print()` for warnings/errors. CloudWatch captures
     `stdout` regardless, so logs are visible; the gap is they're
     unstructured strings, harder to parse for alarms. Migrate
     opportunistically.
   - **CLI tool exception**: `polyris/cli.py`, `polyris/register.py`,
     `polyris/init.py`, `polyris/output.py`, `polyris/_ee/ai/*` use
     `print()` heavily — that's correct. CLI tools **are** the event
     stream consumer, not producer. Don't migrate these to
     structured logging.
   - *Dev/prod parity*: imperfect for us (no local SFN simulator).
     Mitigate via integration tests against real AWS in a dev stack.

   12-Factor items that don't map cleanly to this stack:
   - *Port binding*: API Gateway handles; Lambdas don't bind ports.
   - *Admin processes*: admin operations go through `console_api`
     endpoints, not separate one-shot processes.

### Frontend

Less external canon; more pattern-driven. The conventions in priority
order:

1. **React function components + hooks**. Use `useState` for
   component state, `useReducer` for state with non-trivial
   transitions, `useMemo` for derived values, `useEffect` for side
   effects, `useRef` for DOM access or imperative handles. When
   `useEffect`'s deps array starts feeling like a fight against the
   linter, the component is doing too much — split it.

   **Only sanctioned class component**: `ErrorBoundary.tsx`. React's
   error boundary API (`getDerivedStateFromError`, `componentDidCatch`)
   has no hook equivalent yet — class is the only option. Don't write
   any other class component; if you find yourself reaching for one,
   it's a sign the pattern needs rethinking.

2. **TypeScript strict mode**. `noImplicitAny`, `strictNullChecks`,
   `noFallthroughCasesInSwitch`, all on (see `tsconfig.json`). When
   you reach for `any`, stop and ask if the type is genuinely unknown
   (then `unknown` + narrow) or if you just haven't written it out
   yet (then write it). `as` casts are a smell; use them only at
   trust boundaries (JSON.parse, third-party libs without types).

3. **shadcn/ui as the component primitive layer**. shadcn copies
   components into the codebase rather than depending on an npm
   package; that means we can (and do) customize them freely. When
   you need a button, dropdown, dialog, etc., look in
   `ui/src/components/ui/` first. Underneath: Radix UI for behavior,
   Tailwind for styling. **Don't pull in another component library**
   (Material-UI, Chakra, Mantine) — pick the right shadcn primitive
   and customize.

4. **Tailwind utility classes for styling**. Plus our BEM-prefixed
   global CSS for surface-specific bits (`bl-*`, `bd-*`, `adp-*`,
   etc. — see Hard rule #10). Never `.module.css` files (dead in this
   codebase). Never inline `style={{ ... }}` for static properties;
   inline style is for dynamic values only (computed widths,
   transforms, etc.).

5. **State separation**:
   - **Component state** (`useState`) for things that only one
     component cares about (open/closed, hover state, form input
     before submit).
   - **Zustand** (`useAppStore`) for global UI state shared across
     pages (selected pipeline, view mode, theme).
   - **React Query** (`hooks/queries/`) for server state — anything
     that comes from API. Never store API responses in Zustand;
     React Query owns the cache.

6. **One file, one purpose**. Long components get split into
   sub-components in the same file when they're tightly coupled, or
   their own file when they're independently testable. The
   sub-component split is for *readability*, not for *reuse* — splitting
   for hypothetical reuse leads to APIs that don't fit when reuse
   actually arrives.

7. **Accessibility is not optional**. Every interactive element gets
   `role`, `aria-label` (or `aria-labelledby`), `tabIndex`. Keyboard
   nav works (Tab to reach, Enter/Space to activate, Esc to dismiss).
   The dev tools' Lighthouse a11y score should stay above 95 — it's
   easier to fix as you go than to retrofit.

### Component / plugin boundaries (ADR #97)

Where a **set of extensions must be switchable per build** — console routes, UI
features, schema adapters, and especially anything that crosses the open-core /
proprietary line — use a registry with explicit registration:

- Each component exposes `register(...)` and adds its own pieces (routes, nav
  entries, adapters) to a shared registry.
- The runner (e.g. `console_api/main.py`) holds an **explicit list** of modules
  and calls `register` on each. Building is driven by which modules are in the
  list — open-core builds register fewer, proprietary builds register more.
- Explicit over implicit: a listed set of modules, **not** package discovery
  (`entry_points` / walking installed packages). Implicit may sit on top later,
  but the explicit list is the source of truth.
- Prefer an explicit `register(router)` call over a decorator that registers as an
  import side effect — explicit is easier to debug and order.

**Boundary:** this is *only* for switchable plugin sets. Ordinary code uses plain
functions and direct imports. A registry where a plain call would do is
over-engineering — it violates Principle #12. Don't introduce extension points for
a set that is not actually switchable.

### Open-core UI surface (ADR #99)

The console mirrors the backend split (ADR #97/#98/#100) at build time. The
proprietary **paid surface** lives under `ui/src/ee/`, organised into one package
per tier — `ui/src/ee/team/` and `ui/src/ee/enterprise/`; everything outside
`src/ee/` is public by construction. Two boundaries, two mechanisms (ADR #100):
**free↔paid** is the physical strip (the public build ships without `src/ee/`);
**team↔enterprise** is a runtime entitlement — both paid tiers ship in one bundle
and `can()` gates Enterprise features (no second strip).

How the seam resolves:
- Each tier package exports a `surface` object from its `index.ts`
  (`ui/src/ee/team/index.ts`, `ui/src/ee/enterprise/index.ts`) — the concrete
  components / providers that tier contributes. Tiers fill disjoint slots.
- `ui/scripts/gen-ee-active.mjs` runs on `predev` / `prebuild` / `pretypecheck`:
  it scans `src/ee/*/index.ts`, merges the `surface` of every present tier into a
  single `paidSurface`, and writes `src/ee-active.generated.ts` — an empty stub
  when `src/ee/` is absent (OSS). A new tier needs no edit here: add
  `src/ee/<tier>/index.ts` and it is picked up automatically.
- Free code reaches the surface **only** through `@/ee-active.generated`, typed
  via `@/ee-contract` (the `PaidSurface` slots + shared prop / param types).

**Invariants:** free code never imports `src/ee/` directly (always via the
generated active module); `src/ee/` *may* import free modules — never the
reverse; and `ee/team/` never imports `ee/enterprise/` (enterprise may import
team). `ui/scripts/check-oss-build.sh` enforces the free↔paid direction in CI by
stripping `src/ee` and asserting the OSS build still typechecks + builds.

When adding a paid UI feature:
1. **Decide the tier.** Authoring + basic read + running + live-run intervention
   → free (`src/components`, `src/hooks`). Pipeline-level lifecycle / backfill /
   assets / alert integrations / PATs / observability / config mutation → **Team**
   (`src/ee/team/…`). Governance / cost / SSO / RBAC / cross-account →
   **Enterprise** (`src/ee/enterprise/…`). Unsure between paid tiers → Team
   (the lower paid tier).
2. **Self-contained component** (panel, view, modal): place it under the tier's
   `components` / `views` (`src/ee/team/components`, or `src/ee/enterprise/…`),
   add a typed slot to `PaidSurface` in `ee-contract.ts`, and register the real
   component in that tier's `surface` (`src/ee/<tier>/index.ts`). The free host
   captures the slot (`const X = paidSurface.X;`) and renders
   `{X ? <X … /> : <EeFeatureFallback/>}` — the const-in-closure narrows, and the
   slot is empty in OSS. **Enterprise slots additionally gate on `can()`**,
   because an Enterprise component is *present* in the paid bundle on a Team
   deployment but not *entitled*:
   `const can = useCan(); … {X && can('<capability>') ? <X … /> : <EeFeatureFallback/>}`.
   Add the capability key to the backend registry (`console_api/ee/entitlements.py`)
   under the `enterprise` tier and enforce its data routes with `@requires` —
   `can()` is UX only. Team slots need no `can()` (granted on any paid deployment).
3. **Cross-cutting handlers woven into a free host's UI** (e.g. pipeline actions
   across the toolbar, pause banner, and task modal): put the hook + any
   tier-only confirm modal behind a **render-prop provider** in the tier's `views`
   that owns the hook and exposes handlers via `children(handlers)`. The host
   wraps its content — `Provider ? <Provider …>{h => content(h)}</Provider> :
   content(null)` — and gates each control on the handlers being present (for
   Enterprise, additionally on `can()`). The former example, the pipeline-actions
   provider, became **free** in ADR #110 (its handlers hit only free routes) and
   now lives at `src/components/PipelineActionsProvider.tsx`, wrapped
   unconditionally; the pattern remains for any *paid* cross-cutting handler.
4. **Queries**: a tier's queries live in `src/ee/<tier>/hooks/queries/`. A query a
   *free* component needs stays in `src/hooks/queries/` even if a paid tier also
   uses it. `useCan` / `useTier` / `useCapabilitiesQuery` are free
   (`@/hooks/queries`) — every build calls them.
   The test is *who calls it*, not who owns the feature. `useBackfillsListQuery`
   used to be cited here as the free-side example because a free Header badge
   consumed it; that badge is now the Team `BackfillNavTab`, so the hook has no
   free caller and belongs under `src/ee/team/hooks/queries/`. Left in
   `src/hooks/queries/` it is dead code in the OSS bundle pointing at a route
   that 404s there — and its test asserts against an endpoint the free build
   does not serve. When a paid surface absorbs a query's last free caller,
   move the query in the same change.
5. **Tests follow tier**: a paid component / hook's test lives next to it under
   `src/ee/<tier>/` (stripped in OSS); free tests stay under `src/`. When a free
   host's test needs the surface, mock `@/ee-active.generated`'s `paidSurface`.

To add a whole new tier: `mkdir src/ee/<tier>` with an `index.ts` exporting a
`surface`, add its capability set to `PLANS` in `console_api/ee/entitlements.py`,
and the generator picks it up — no edit to the generator or to free code.

### How philosophy meets the hard rules

The Core Principles up top are derived from this philosophy, not
separate from it:
- *No duplication* = "one obvious way to do it" + DRY
- *No silent excepts* = "errors should never pass silently"
- *No stubs* = "now is better than never" applied responsibly
- *No fix-on-top-of-fix* = "if the implementation is hard to explain,
  it's a bad idea"
- *Idempotency* = serverless 12-factor disposability requirement

When you face a judgment call the hard rules don't cover, the
question is: *which way pulls toward this philosophy, and which way
pulls away?* Pull toward.

### This section describes the codebase, not an aspiration

The audit log (v0.78.9) actually measured each principle against
current code. Snapshot at v0.78.10:

**Already followed in practice** — no work needed, just keep doing:
- No wildcard imports (0 across all `.py` files)
- Type hints on public Python functions (91%)
- DAL repositories for all DynamoDB access in `console_api` (100% as of v0.93.1 —
  three route/helper sites bypassed it via `dynamodb.Table(...)` until then). Adding
  DDB access there means a repo in `console_api/dal/`, never a raw table handle in a
  handler. The one deliberate exception is `notify/registry.py`: that Lambda has no
  `dal/` and one table accessor, so `registry_table()` *is* the accessor — a DAL for
  a single function would be the over-engineering Principle #12 warns about.
- Config via env + CFN params, no hardcoded resource names (100%)
- Stateless Lambdas, no warm-state caching (100%)
- TypeScript strict, no `any` in production code (100%)
- No `.module.css` files (0)
- Components never import `api` directly (100%)
- shadcn primitives only, no other UI libraries (mui/chakra/mantine/antd = 0)
- React Query for server state, Zustand for global UI state, useState for component state — clean separation

**Known gaps** — flagged for opportunistic cleanup:
- `print()` in `evaluate_deps`, `notify_asset_subscribers`, `check_assets`,
  `query_subscriptions`, `ui_bootstrap` instead of `polyris/logger.py`
- Docstrings on `~80` CLI `cmd_*` argparse handlers (mostly safe under
  the documented exception above, but worth a pass)
- 3 inline `style={{ margin: '10px' }}` on ReactFlow Panels in
  `AssetLineageFlow.tsx` could move to CSS classes
- ARIA audit on icon-only buttons (most buttons have text content and
  don't need `aria-label`, but a targeted sweep of icon-only ones
  hasn't been done)

None of the gaps are silent — they're all in STATE.md or this section.
If you're touching one of these files for another reason, fix the gap
while you're there. Don't open a PR just to fix them in isolation
unless you're doing a deliberate hygiene pass.

---


## What is Polyris

Serverless data pipeline orchestration on AWS Step Functions. Python DSL → generates ASL → deploys via CloudFormation (`polyris-deploy`).

Core idea: each task = one `dependency_wrapper` SFN execution that waits for deps, runs the
task, then signals downstream tasks via `notify_dependents`.

---

## Project Layout

> Canonical full-system layout. See **Repository context** at the top for
> what *this* repo physically ships.

```
polyris/              # Python DSL library + CLI (polyris-deploy, polyris-init)
pipelines/            # Pipeline definitions — each pipeline has a dag.py
sam/
  template.yaml       # ALL AWS resources (SAM template) — single source of truth
  samconfig.toml      # Deploy config (gitignored)
  samconfig.toml.example
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
  deploy.sh           # Upload built UI to S3 + invalidate CloudFront
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

**SFN edit workflow:** edit `sam/sfn_templates/*/sfn.tpl.json` → `sam build && sam deploy`.
`sam build` inlines `.tpl.json` into `DefinitionString` automatically.

### Lambda packaging: `polyris` SDK comes from `requirements.txt`

**Current state (post repo-split, ADR #102):** the console Lambda gets the
`polyris` SDK core as a normal package dependency, declared in
`sam/lambdas/console_api/requirements.txt`:

```
polyris @ git+https://<PUBLIC_REPO_URL>@vX.Y.Z
```

`routes/backfill.py` imports `polyris.partitions` / `polyris.granularity`;
`sam build` installs them from the dependency above into the package. For local
development, `pip install -e ../polyris` (or from the public repo checkout)
shadows the pinned tag with the live core.

**History — the old symlink is gone.** Before the split, the core sat at the
repo root and was vendored via a committed git symlink
`sam/lambdas/console_api/polyris -> ../../../polyris`. That was always tech debt
(broke on `unzip`/GUI extractors, Windows, etc.). ADR #102 removed it: the core
is a separate repo/package now, so the symlink approach no longer applies (there
is no repo-root `polyris/` in the same layout to point at). Regression tests were
updated accordingly:
- `test_lambda_packages_polyris_via_requirements` — `requirements.txt` must
  declare `polyris`, and the old symlink must NOT be reintroduced.
- `test_lambda_local_makefile_not_reintroduced` /
  `test_template_has_no_buildmethod_makefile_for_console_api` — the abandoned
  Makefile build pattern must not come back.

**Things Claude must NOT propose:**
- ❌ Re-adding the `console_api/polyris` symlink. It assumed a repo-root layout
  that no longer exists after the split, and shadows the package dependency.
- ❌ Adding `sam/lambdas/console_api/Makefile` with `BuildMethod: makefile` and
  `cp ../../../polyris`. Failed live smoke 2026-05-22 (SAM CustomMakeBuilder runs
  from a scratch dir; relative paths can't reach repo root).
- ❌ Vendor copy via a top-level `make sam-build` wrapper, pre-build hooks, or
  `samconfig.toml` tricks. Plain `sam build` must just work from
  `requirements.txt`.

**SaaS build (paid console):** the private `polyris-ee` repo's `ee/` is overlaid
on top of the public free backend at build time, and the same
`requirements.txt`-based core install applies. See ADR #102 "How the two backends
combine at build time".

### DynamoDB GSI changes — one op per update

When `sam deploy` fails with:

```
Cannot perform more than one GSI creation or deletion in a single update
```

This is an **AWS hard limit**, not a polyris bug. CloudFormation can
only do **one GSI add or delete per UpdateTable call**. Renaming a GSI
(= delete old + create new) violates this.

**Don't propose code changes to "fix" this.** The fix is operational:

1. If old GSI has no readers (grep the codebase) — delete it manually
   via `aws dynamodb update-table --global-secondary-index-updates ...`
   then re-run `sam deploy`.
2. Or two-phase: temporarily add old GSI back to template, deploy, then
   remove it, deploy again.

Procedure: delete the old GSI manually via `aws dynamodb update-table
--global-secondary-index-updates`, wait for completion, then re-run `sam deploy`.

Avoid renaming GSIs in future template edits. Add-new-first, migrate
reads, remove-old-later as separate PRs.

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
| `api_tokens_repo` | `api-tokens` | `token_id` | — | `hash-index`, `owner-index` (PATs, ADR #65) |

**CRITICAL — two separate subscription tables:**
- `dep_subscriptions_repo` / `DependencySubscriptionsTable` — task-to-task deps within pipeline
- `asset_subscriptions_repo` / `AssetSubscriptionsTable` — cross-pipeline asset triggers

Never mix these up. `query_subscriptions` Lambda reads `DependencySubscriptionsTable`.
`registration` SFN writes to `DependencySubscriptionsTable`. `check_assets` uses `AssetSubscriptionsTable`.

**Key fields in `pipeline-tokens` (executions_repo):**
- `execution_name` — unique task execution ID: `{task_name}-{date}-{pipeline_execution_short}`
- `pipeline_execution` — pipeline run ID: `{pipeline_name}-run-{date}-{hex8}`
- `pipeline_execution_short` — last 20 chars of `pipeline_execution`, strips `.` and `:`
- `pipeline_name` — pipeline identifier
- `status` — see TaskStatus constants
- `wrapper_execution_arn` — ARN of dependency_wrapper SFN execution for this task
- `task_execution_arn` — ARN of run_task SFN execution for this task
- `orchestration_token` — `.waitForTaskToken` token for signaling deps ready
- `wait_token` — token for notify_dependents to signal ready

**dependency_key format:** `{upstream_task_name}-{pipeline_execution_short}`
Used to find subscribers in `dep-subscriptions` table.

### Zustand + React effect closures — URL sync gotcha (v0.78.7, ADR #63)

When syncing Zustand store state to URL via effects in the same hook
that initializes the store from URL, **effects run in declaration order
within a single commit, but Zustand state updates don't propagate
mid-commit**. The pattern that fails:

```ts
// FAILS: mount-once sets store.date(URL_value); push effect uses STALE store.date
const initialized = useRef(false);

useEffect(() => {
    if (!initialized.current) {
        initialized.current = true;
        store.setDate(urlState.date || today);  // schedules update
    }
}, []);

useEffect(() => {
    updateUrl({ date: store.date !== today ? store.date : undefined });  // reads STALE store.date
}, [store.selectedPipeline?.name]);
```

The push effect's closure was captured at render time. `store.date` in
the closure is the value at that render, NOT the value after
mount-once's setDate call. When mount-once schedules `store.setDate(Y)`,
the push effect still sees the old `today`. It calls `updateUrl({date:
undefined})`, stripping the date from the URL via `pushState`.

After commit ends, React re-renders with the new store.date. Other
effects with date in deps re-fire and re-add the date. The URL flickers,
but anything reading URL synchronously during the gap (e.g. a child
component mounting via React lazy or parsing pathname for routing
decisions) sees a stripped URL.

**The fix is `useState`, not `useRef`**, so the gate transition triggers
a re-render that re-reads fresh store state:

```ts
const [isInitialized, setIsInitialized] = useState(false);

useEffect(() => {
    if (isInitialized) return;
    store.setDate(urlState.date || today);
    setIsInitialized(true);  // triggers re-render
}, []);

useEffect(() => {
    if (!isInitialized) return;  // skip first commit
    updateUrl({ date: store.date !== today ? store.date : undefined });
}, [store.selectedPipeline?.name, isInitialized]);
```

On the second commit (after `setIsInitialized(true)`), the push effect
re-fires with the fresh `store.date = Y` from a new render's closure.

**Symptom to recognize**: user reports "click X → ends up at default
state Y" where X passes deep state via URL params. URL bar may show
correct params briefly before flickering to default. Bug pinned by
test `useStoreInit.test.ts` "preserves URL date param when store has
stale date on mount".

---

## Step Functions

### 16 State Machines

| Name | Type | Purpose |
|------|------|---------|
| `polyris-dependency-wrapper` | STANDARD | One per task — waits for deps, runs task, signals downstream |
| `polyris-dep-run-task-helper` | STANDARD | Executes the actual task SFN/Lambda/Glue/etc |
| `polyris-failure-handler` | STANDARD | Handles failures, notifies, updates DynamoDB |
| `polyris-registration-helper` | STANDARD | Registers pipeline on deploy |
| `polyris-pause-waiter` | STANDARD | Holds execution during pipeline pause |
| `polyris-notify-dependents` | EXPRESS | Finds and signals downstream tasks when upstream completes |
| `polyris-restart-task-helper` | EXPRESS | Stops wrapper + restarts task |
| `polyris-restart-wrapper` | EXPRESS | Starts new dependency_wrapper for restart |
| `polyris-slack-interactive` | EXPRESS | Sends Slack message with action buttons |
| `polyris-pagerduty-alerter` | EXPRESS | Sends PagerDuty alert |
| `polyris-pagerduty-resolver` | EXPRESS | Resolves PagerDuty incident |
| `polyris-notify-asset-consumers` | EXPRESS | Triggers asset-based pipelines |
| `polyris-register-pipeline` | EXPRESS | Registers pipeline in registry |
| `polyris-test-quick` | STANDARD | Demo/test task (fast) |
| `polyris-test-success` | STANDARD | Demo/test task (always succeeds) |
| `polyris-test-failure` | STANDARD | Demo/test task (always fails) |

### Calling SFNs from SFN

```
Standard SFN  → startExecution.sync:2     ✅
Express SFN   → aws-sdk:sfn:startSyncExecution  ✅
Express SFN (fire-and-forget) → states:startExecution  ✅
Express SFN   → startExecution.sync:2     ❌ WRONG — fails silently
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

Routes self-register via a **registration registry** (ADR #97), not a literal
dict. Each route module exposes `register(router)` and adds its own routes;
`main.py` holds an explicit `ROUTE_MODULES` list, calls `register` on each, and
exposes the assembled `ROUTES` table `(METHOD, '/api/path') → (handler_fn,
'param_key')` (tests import `from main import ROUTES`).

**Open-core surface (ADR #98, #110):** OSS route modules register unconditionally; if
the proprietary `ee` package is importable, `main.py` appends `ee.MODULES`. So the
surface is **27 free routes** in an OSS-stripped build, **63** in the full build.
Free = authoring + basic read + running + **live-run intervention** (task actions
skip/fail/success/stop/restart, execution stop/pause/resume/extend); Team (in
`ee/team/`) = pipeline-level lifecycle (pause/restart), backfill, assets console,
alert integrations (Slack/PagerDuty), PATs, per-pipeline observability
(logs/metrics), and config mutation (task-config PUT, decision-timeout PUT).

Path params go via query string (e.g. `/api/tokens?id=…`), not REST path
segments — the API is a single `/{proxy+}` integration, so adding a route needs
**no** `template.yaml` change.

**Auth gate (ADR #65, #66):** `auth.authenticate()` then `auth.authorize()` run
at the top of `handler()` before dispatch. `authenticate` accepts a Cognito JWT
(offline JWKS verify) or a PAT (`plrs_…`) → `401` on failure; `authorize` checks
the token's **scope** (`read` ⊂ `write` ⊂ `admin`, derived from the HTTP method
+ a small `ADMIN_ROUTES` set) → `403` if too low. Gated by `AUTH_ENABLED` (env,
default `false`). Public (no token): `/api/health*`, `/api/metrics`, and
`/api/action/*` (token-less Slack button callbacks). Cognito users + legacy
PATs (no scope) = `admin`. PAT store = `api-tokens` table / `api_tokens_repo`.

When adding an endpoint:
1. **Decide the tier (ADR #98, #110).** Free (authoring + basic read + running +
   live-run intervention: task actions, execution control) → `routes/<mod>.py`.
   Team (pipeline-level lifecycle, backfill, assets, alert integrations, PATs,
   observability, config mutation) → `ee/team/<mod>.py`. Shared
   helpers stay in the OSS `routes/` module and `ee/` imports them — **never the
   reverse** (OSS must never import `ee`).
2. Add the handler to the chosen module and register it in that module's
   `register(router)`.
3. New module? Add it to `main.py`'s `ROUTE_MODULES` (free) or `ee/__init__.py`'s
   `MODULES` (Team). An existing module needs no `main.py` change.
4. Free handlers are also exported from `routes/__init__.py` (the OSS barrel);
   Team handlers are not (the barrel is OSS-only).
5. Tests follow tier: free → `tests/…` / `console_api/tests/`; Team →
   `console_api/ee/team/tests/`. Update the route-count guard — the free-subset
   assert in `tests/sdk/test_templates.py`, the full-63 assert in
   `ee/team/tests/test_route_table_ee.py`.
   (no `template.yaml` change needed — the `/{proxy+}` integration covers it)

---

## Backend Patterns

**Always use DAL repos:**
```python
from dal import executions_repo, pipelines_repo
# Never: boto3.resource('dynamodb').Table('...')
# Never: repo.table.get_item(...)  — use repo.get(key)
```

**Error handling:**
```python
from botocore.exceptions import ClientError, BotoCoreError
try:
    result = executions_repo.get(key)
except (ClientError, BotoCoreError) as e:
    log.error("context", "message", error=str(e))
    return cors_response(500, {'error': str(e)})
```

**Re-raise permission errors in Lambdas** — never return `{}` or `[]` silently on AccessDeniedException.

**Stop pipeline hierarchy** (always stop in this order):
1. Pipeline SFN execution (reconstruct ARN: `sfn_arn.replace(':stateMachine:', ':execution:') + ':' + pipeline_execution`)
2. All `wrapper_execution_arn` from task items (deduplicated)
3. All `task_execution_arn` from task items
4. Update DynamoDB → `stopped`/`aborted`

---

## Error Visibility (ADR #38)

**Requirement:** every error must be visible to the operator.

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
Violating this rule → `_notify_warn_` will appear in All Tasks / pipeline status / runs.

**SFN templates:** `Catch` blocks must preserve `$states.errorOutput` in Output.

## UI Patterns

- React Query only for data fetching (`useQuery` in `hooks/queries/`)
- Add `queryKey` to `lib/queryClient.tsx` for new queries
- Components never import `api` directly — use hooks
- Styling: shadcn/ui + Tailwind CSS only
- Icons: Lucide React via `icons.tsx`
- Runtime config: `window.CONFIG` (set by `/config.js`) is source of truth, `NEXT_PUBLIC_*` is build fallback only — `getConfig()` is window-first, never env-first; use `??` for booleans (ADR #94)

### Responsive Layout (ADR #40)

UI must work on desktop (≥1024px), tablet (≤1024px), and mobile (≤768px / ≤480px).

**Layout rules:**
- Use **flex/grid chains** (`flex: 1; min-height: 0`) for height inheritance — never `calc(100vh - Npx)`
- Heavy components (ReactFlow, Gantt, charts) inherit height from parent: `height: 100%` + parent flex chain + baseline `min-height` for empty states
- Tables wrap in `.table-full` (already has `overflow-x: auto` mobile rule)
- Tabs that may overflow: `overflow-x: auto; flex-wrap: nowrap; scrollbar-width: thin`

**Mobile breakpoints (defined in `_mobile.css`):**
- `≤1024px` — tablet: compact sidebars, hide breadcrumbs
- `≤768px` — mobile: hamburger menu, sidebar overlay, full-width modals (`max-width: 95vw`)
- `≤480px` — small mobile: smallest font sizes, 90vw sidebar

**Before merging UI changes — checklist:**
1. No `calc(100vh - Npx)` or `calc(100vw - Npx)` magic numbers (parent-relative `calc(100% + Npx)` is fine)
2. New full-screen views (`*View.tsx`) must verify mobile rules in `_mobile.css` cover them
3. New modals — either use `BaseModal` (mobile rules inherited) or add own `@media` queries
4. Tap targets ≥36px height
5. No fixed `width: Npx` on top-level containers — use `min-width` + `max-width: 95vw` pattern


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

# Core coverage floor (pure-logic core; ratchet, target 90%)
make test-cov
```

**Test rules:**
- pytest-mock (`mocker` fixture) everywhere — no `unittest.mock` import at all,
  including `MagicMock` as a bare object factory (use `mocker.MagicMock`). The rule
  exists for `patch`: hand-managed context managers leak when a test fails (ADR #26)
- Backend tests in `tests/backend/`, SDK tests in `tests/sdk/`
- Pure-logic core stays above the coverage floor (`fail_under` in `pyproject.toml`);
  raise the floor when you raise coverage (Principle #22). AWS/CLI modules are
  `omit`-ed and covered by e2e/smoke instead.

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

Optional but recommended: `docs/tools/AGENTIC_WORKFLOW.md` documents the
`/shape` → `/build` → `/break` loop, the Stop hook that runs the gates before a
turn can end, the destructive-command guard, and the two tests that keep this
document and `.claude/CONTEXT.md` from drifting. If you change a checkable claim here,
update `tests/sdk/test_claude_md_claims.py` in the same change.

---

## Archive Delivery

Two repos → two archives, each holding only what that repo owns. CE (this repo)
is the full free product; EE is the paid `ee/` overlay only (the free scaffold is
gitignored there and pulled in at build time — never committed). Always full
archives, never incremental; strip caches first.

```bash
# CE (public) — the whole free product
cd /home/claude/work && zip -qry -y /mnt/user-data/outputs/polyris-public.zip polyris
# EE (private) — paid ee/ overlay only (see polyris-ee/.gitignore + check-no-leak.sh)
cd /home/claude/work && zip -qry -y /mnt/user-data/outputs/polyris-ee-private.zip polyris-ee
```

Releases are cut from git tags, not these archives: a CE tag `vX.Y.Z` builds the
PyPI library + the self-hosted bundle; an EE tag `platform-vX.Y.Z` overlays `ee/`
onto CE@`CE_REF` and deploys the SaaS. See `polyris-ee/docs/development/DEV_AND_RELEASE.md`.

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
