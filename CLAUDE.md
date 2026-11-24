# CLAUDE.md — Polyris Agent Instructions

Read this fully before writing any code. When in doubt, `grep` first.

---
## Repository context

**You are in `polyris` — the public, open-core repo.** It ships the free SDK (`polyris/`), the free console backend (`sam/lambdas/console_api/`), and the free UI (`ui/src/`, everything outside `src/ee/`). The private `polyris-ee` repo depends on it — never the reverse.

- **Never add proprietary/paid code here.** Paid code lives only in `polyris-ee`. `polyris._ee*` is excluded from the wheel; no `ee/` directory in this checkout.
- **The seam is enforced.** Free UI reaches paid surface only through the generated `@/ee-active.generated` stub (empty in OSS). `ui/scripts/check-oss-build.sh` CI gate strips `src/ee/` and asserts the build still typechecks.
- **Self-contained build/test:** `pip install -e ".[dev]"` then `python -m pytest tests/sdk tests/backend tests/integration` and `make test-docs` (its own session: in one session with `tests/backend`, `tests` resolves to console_api's test package); for the UI, `cd ui && npm ci && npm run typecheck && npm test -- --run && npm run build`.

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

**CLAUDE.md itself is a doc — Principle #9 applies to it too.** If a rule in CLAUDE.md quotes a specific docstring or cites a specific function signature, and that docstring or signature changes, update the rule in the same commit. A CLAUDE.md rule that describes code that no longer exists misleads more than silence would.

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
2. Run `pytest -m smoke` against the live deployment; `make e2e-coordination STACK=<stack> PROFILE=<profile>` runs the coordination smoke in one command
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

When adding or removing a value: (1) edit `polyris/constants.py`, (2) register in `sync_enums.py` if new class, (3) run `python -m polyris.codegen.sync_enums` — mandatory even for removals (CI `make check-generate-enums` will fail if skipped), (4) import the enum everywhere — never bare strings, (5) add a parity test (#28).

**28. Constants coupled across languages/artifacts need a parity test**
Any string written in one artifact and read verbatim in another must have a parity test asserting the same value on both sides. The type system can't enforce string parity across JSON templates or Python↔TypeScript.

Examples: `_PUSH_MARKER_FIELD` written by `xcom.push()`, read by SFN template — parity in `test_xcom_coupled_constants_parity.py`. The upstreams whose output a task reads — `run_task`'s `Items` and `xcom._readable` — `test_the_set_run_task_reads_is_the_set_xcom_reads`: an allowlist on both sides, never a list of refusals on one (`_readable` refused only failures and rule skips, and read an older run's output of the date for an upstream that still ran; the review of TR-5). Marker fields `_manually_resolved`/`_resolution`/`_reason`/`_operator` — same test. `SCHEDULE_PRESETS` ↔ `infer_cron_cadence` — `TestSchedulePresetsParity` in `test_granularity.py`. Generated enums covered by `codegen --check`.

**29. adr-index.md count must match its row count**
`docs/reference/adr-index.md` opens with "_N rows indexed (X inline, Y standalone)_". When you add or remove an ADR, update the count in the same commit:
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

**Review "done" criterion — classify before acting:**
A finding is a **blocker** if it either (a) prevents the user from completing their task, or (b) silently produces an incorrect result. Everything else is **polish** — worth fixing, but does not gate the claim.

Before starting another review round, triage the open list: blockers go first, polish is batched. Additional rounds are warranted only while blockers remain. Accumulating polish across infinite rounds is not "being thorough" — it is scope creep masquerading as quality.

**33. Never change `Description` on `AWS::IAM::ManagedPolicy` — CloudFormation replaces the policy and hits a name collision**
CFN treats `Description` as update-requires-replacement. Editing it → delete + create with same `ManagedPolicyName` → `EntityAlreadyExists` → rollback. Hit twice in 1.0.0 delivery.

How to apply: editing an IAM policy → change only the `Statement` block; leave `Description` and `ManagedPolicyName` alone. Before editing any CFN property on a named resource, check the AWS docs for "Update requires: Replacement".

**34. Two writers to the same DDB row must both handle "row already exists" explicitly**
Once a second writer joins a row (Lambda + SFN, or operator action + SFN), `attribute_not_exists` means "silently do nothing if someone got here first" — almost never what you want.

Before adding a second writer: answer per row: "when writer B fires and writer A's data is present — protect, overwrite, or fail loudly?" If "protect if fresh, overwrite if stale" → you need GetItem-then-Update (#31).

**35. ECS TaskDefinition and Batch JobDefinition — reference by family name, never by pinned revision ARN**
Any template change to `Command`, `Image`, etc. creates a new revision (`:2`, `:3`, …). A DAG pinned to `arn:...:task-definition/family:1` keeps calling revision 1 with its stale Command even after the template change ships. Hit in 1.0.0 smoke — `polyris-test-xcom-all-render:2` created but DAG still called `:1`.

In `@task.ecs_task(task_definition=...)` and `@task.batch_job(job_definition=...)`: drop the trailing `:N`. Only pin `:N` when deliberately holding a specific version — document why.

This applies to docstring examples too — a docstring that shows `task_definition='my-task-def:3'` misleads users into using the pinned form. The deploy's existence check looks a Batch reference without `:N` up by name (`resource_check._active_job_defs`): `describe_job_definitions(jobDefinitions=[…])` documents only the forms with a revision.

**36. Functions with 5+ parameters enforce keyword-only arguments**
Any function with five or more parameters must separate positional from optional parameters with `*`, making every optional argument keyword-only at the call site:

```python
# Wrong — 10 positional params; callers silently break when order changes
def deploy_pipeline(dag, stage=None, region=None, dry_run=False, destroy=False, ...): ...

# Right
def deploy_pipeline(dag: DAG, *, stage: Optional[str] = None, region: Optional[str] = None, dry_run: bool = False, ...): ...
```

Keyword-only call sites are self-documenting (`deploy_pipeline(dag, dry_run=True)`) and immune to silent breakage when the signature is reordered. Exception: internal helpers where positional order is obvious by convention (`checker(client, ref)`).

**37. `typing.cast()` on external data requires a runtime guard**
`cast()` is a lie to the type checker — it does nothing at runtime. On internal data whose shape is controlled by our own code, `cast()` is acceptable. On data from external sources (AWS API responses, user input, deserialized JSON), an unverified cast silently lets the wrong type propagate until it crashes far from the cast site.

```python
# Wrong — AWS changes the response shape; crash happens later with no context
events = cast(List[Dict[str, Any]], history["events"])

# Right — fail immediately with actionable context
raw = history.get("events")
if not isinstance(raw, list):
    raise ValueError(f"Unexpected events shape from AWS: {type(raw)}")
events: List[Dict[str, Any]] = raw
```

Rule: `cast()` on an AWS API field, user-supplied dict, or deserialized JSON must be preceded by an `isinstance` check that raises with context.

**38. Error visibility: no infrastructure failure may be silent**
Every infrastructure failure ends in one of three ways (ADR #130 §9):
- it is retried automatically until it succeeds;
- it fails the affected task with the real cause, visible in the console;
- it surfaces in the console as a warning (bell) or in the System status in the header.

It is always logged with `error=str(e)`. See the [Error Visibility section](#error-visibility-adr-38) for the `_notify_warn_` pattern.

Production code and tests cite "Principle #38" for error visibility. Never renumber this entry.

**39. When a new field replaces an old one in a multi-reader contract, keep writing the old field until every reader is migrated**
If code A introduces a field that supersedes an old one while code B still falls back to the old field, A must write both. Stopping the old write silently breaks B's fallback, often only in one timing window.

Correct: write both until every reader is updated and deployed. If every reader is replaced in the same deploy, nothing is left to fall back.
Anti-pattern: write only the new field because "the old one is deprecated" while B still reads it.

Before removing the write of an old field, `grep -rn` for every reader that could fall back to it.

**40–52, 55 (coordination). The coordination code follows the rules in ADR #130, *Rules for the code*, as ADR #137, *ADR #130 after this ADR*, restates them**
They concern task rows, wake-ups, facts and the sweeper. The numbers stay because code and tests cite them; read the rule in both ADRs before changing that code.
- **40.** Delete a row's `asset:{name}` pointers only once the row has left its dependency wait.
- **41.** Wake-up hints may be lost; wake-up decisions, status writes and fact writes may not. The hints are the two streams, with the sweeper's checks of the run rows behind them; the decisions are the dispatcher's writes and starts, the run's answer, and every write of a fact, which shares a transaction with its status.
- **43.** Retired by ADR #137: no token signals a task. The dispatcher writes the transition it decides before it starts `run_task`.
- **46.** The run's token, which `Start_Run` hands over through the run row, is findable by the sweeper: the run row is due at least until its current token is answered, and a failed write fails `Start_Run`.
- **47.** A task row has one owner at a time, named by its generation; `tests/sdk/test_task_row_writers.py` lists every writer.
- **49.** A platform function that a pipeline's state machine calls reads each format it supports and refuses any other, with the cause (`Start_Run`'s arguments). A key added to `run_task`'s input after a release is read through `$exists` for one release: one stack updates the dispatcher and `run_task`, but not at the same moment.
- **50.** A time that marks a beginning is written once (`if_not_exists`).
- **51.** A reader of shared rows skips a row it cannot parse, and says so.
- **52.** A time budget is checked per item, and a periodic job's steps fail independently; every pass, narrow or full, has a budget below its function's timeout.
- **55.** An evaluation as of a time reads only what was recorded by then (`asset_conditions.evaluate(recorded_by=)`).

**42. A second bug of the same class in one mechanism stops the patching — question the model first**
When a mechanism produces a second bug of the same class (e.g. "task silently hangs until timeout"), do not patch it a third time. Stop and write down, before any code:
- what the bug class is;
- why the current model keeps producing it;
- what model would make it impossible.

Then decide with the maintainer: patch now and schedule the model change, or change the model now. Record the decision in an ADR.

**Why:** a flag-based AND gate drafted on an unmerged branch (superseded by ADR #130) produced four separate silent-hang bugs, each fixed locally. The root cause was the model: readiness was accumulated from events instead of evaluated from facts. Each local fix looked like an isolated oversight; together they were a signal.

**How to apply:** keep a running count per mechanism during a branch. "Same class" means the same user-visible failure, not the same line of code. This is the escalation step of Principle #5. It does not forbid shipping the patch, but the model question must be asked and answered.

**44. A guard for an optional overlay import catches only that module's absence**
`try: import ee` must catch only a `ModuleNotFoundError` whose `name` is the overlay module. If it catches every `ImportError`, a broken import inside the overlay silently removes every route the overlay provides.

**45. No custom CloudWatch metrics, alarms, SNS topics or DLQs in the platform template**
Code computes engine health on demand and shows it in the console (ADR #130 §9). Failures follow #38 and #41. Custom metrics, alarms, topics and queues cost every user money and need someone to watch them.

A template test enforces this rule. Adding any of them needs an explicit maintainer decision recorded in an ADR.

**48. A value an ADR defines clause by clause is one pure function with a test per clause**
The enabling time (ADR #130 §9, ADR #138 §2) has five clauses: latest required dependency, earliest success for `one_success`, earliest final failure for `one_failed`, earliest ready member of an OR group, never before the resume. Computed inline in the reconciler, three of them were missing, and the sweeper reported false misses after every resume over budget. Put such a definition in `polyris/coordination/` (here `trigger_rules.enabled_at`, `Evaluation.enabled_at`) and test each clause.

**53. A Lambda's AWS calls fail inside the Lambda's timeout**
botocore's defaults (ten DynamoDB attempts, a 60 s read timeout) outlast a 30 s function, and a function that times out cannot report its own transient error, so the caller sees `Sandbox.Timedout`. A read of an index that is still being built answers `ValidationException` "Cannot read from backfilling global secondary index"; `failures.is_transient` counts it as transient, because an upgrade that adds an index reaches the Lambdas first (the 1.2.0 upgrade rehearsal failed a `wait_for` task on it). Build clients with a short retry and timeout config (`env._config` in the coordination Lambdas), and let the caller's `Retry`/`Catch` treat the platform's own failures (`Sandbox.Timedout`, `Lambda.Unknown`) and any HTTP 5xx as transient.

**54. Every resource AWS can tag carries the billing tags**
The tags are `polyris:namespace`, `polyris:stage` and `polyris:component` (`platform` or `pipeline`), plus `polyris:pipeline` on a pipeline's resources (ADR #132). No other keys. In `sam/template.yaml` the Lambda functions take them from `Globals`, and every other resource spells them; `polyris-deploy` uses `resource_tags.pipeline_tags`. A resource without them is missing from every cost report and IAM condition that selects by them. `tests/sdk/test_resource_tags.py` learns from cfn-lint's resource schemas which types AWS can tag and checks every resource after the SAM transform, so a new resource fails it until it is tagged.

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

Core idea (ADR #137): a run is one pipeline execution whose `Start_Run` writes one row per task. The dispatcher, woken by the tasks table's stream, decides each row from the rows and facts, starts its `run_task`, and answers the run once every row is final; the sweeper checks every run on a schedule, so a lost wake-up only delays a task.

---

## Project Layout

> Canonical full-system layout. See **Repository context** for what this repo physically ships.

```
polyris/              # Python DSL library + CLI (polyris-deploy, polyris-init)
pipelines/            # Pipeline definitions — each pipeline has a dag.py
sam/
  template.yaml       # ALL AWS resources — single source of truth
  sfn_templates/      # SFN definitions as .tpl.json — edit HERE, never in template.yaml
    bulk_backfill/sfn.tpl.json
    helpers/
      run_task/sfn.tpl.json
  lambdas/
    console_api/      # REST API (24 free routes; the ee/ overlay adds the Team routes), DAL repos, route handlers
    coordination/     # the dispatcher (reconcile), start-run, asset-fanout, sweeper (ADR #137): decide each row, start run_task, start push consumers, re-check
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
# Users (ADR #131): prebuilt release of the installed polyris version — Python + AWS credentials only
polyris-up --profile <profile>                 # install / upgrade the platform
polyris-down --profile <profile>               # remove it

# Contributors: from a checkout. sam build packs polyris from the Lambdas' requirements.txt (the last release):
# point both pins at the checkout first, never commit that, undo it before a commit (CONTRIBUTING.md)
sed -i "s|^polyris @ .*|polyris @ file://$PWD|" sam/lambdas/console_api/requirements.txt sam/lambdas/coordination/requirements.txt
cd sam && sam build && sam deploy --profile <profile>
cd ui && npm ci && npm run build && ./deploy.sh <stack> <region> --profile <profile>

# Pipeline
cd pipelines/my-pipeline && polyris-deploy --stage dev --profile <profile>
```

**Release contract (ADR #131).** Every `v*` tag must publish `polyris-platform-vX.Y.Z.zip` (`.github/workflows/release.yml` → `scripts/build_platform_release.py`). CI owns the GitHub Release: draft → attach assets → publish, never the other way round — the repo keeps *Immutable releases* on, so a release published before its assets can never get them; `polyris-up` downloads the one matching `polyris.__version__`. The archive layout lives in `polyris/platform.py` (`Manifest`), imported by both sides — change it there, and bump `MANIFEST_FORMAT` if an older `polyris-up` can't read the new layout. A new `AWS::Serverless::Function` or `AWS::Serverless::StateMachine` with a local `CodeUri` / `DefinitionUri` is picked up automatically. Any other `AWS::Serverless::*` local path (`CodeUri`, `ContentUri`, `DefinitionUri` — e.g. a layer) fails `rewrite_template`, and with it the PR's *Platform archive* CI job, until `scripts/build_platform_release.py` packages it.

**Neither `polyris-up` nor `polyris-deploy` may shell out to the `aws` / `sam` CLIs** — deploys go through `polyris/cfn_changeset.py` (boto3 change sets). The user-side promise is "Python and AWS credentials".

**The move from 1.2 to 1.3 (ADR #137, *Migration and upgrade*).** `polyris-up` checks a platform below 1.3 before it changes anything (`upgrade.check`): older than 1.2.0, 1.2's adoption not settled for 48 hours, a run in flight. It stops what 1.2 left running as the cleanup of the update from 1.2 begins and at the end of that run, and on a later run only while a state machine of 1.2 is left (`upgrade.left_by_1_2`) or the stack still cleans up (`upgrade.stop_leftovers`); every run on 1.3 waits for `run-start-index` and lists the pipelines to redeploy. The rules are pure, in `polyris/platform.py`; `polyris/upgrade.py` is their AWS side. Three things to keep:
- A step that stops executions runs only on a platform that runs 1.3, read from the stack (`platform.runs_tasks_as_rows`), and it stops an execution of a state machine that 1.3 keeps only when it started before the 1.3 update created `pipeline-specs`.
- Every execution under a name in `platform.REMOVED_STANDARD_MACHINES` counts as 1.2's, so no state machine may get such a name back; a new Standard state machine of the engine joins the names in `platform.py` (`tests/sdk/test_platform.py` fails until it does).
- A check that cannot see what it checks refuses: `upgrade.check` refuses a platform none of whose engine state machines it finds.

**A command that deletes a stack with state machines stops their running executions until the stack is gone** (`cfn_changeset.delete_stack` with `executions.stop_running`). CloudFormation deletes a state machine only once its executions have ended, and a run keeps its execution open until every task of it is final, a paused run forever: `polyris-destroy` waited ten minutes and failed with the stack left deleting (review of the branch, B-1). One stop before the delete is not enough: until the state machine is deleting, its schedule can start a run (`test_destroy_stops_a_paused_run_and_one_its_schedule_starts_while_the_stack_is_deleted`).

**Pre-deploy resource check:** `polyris-deploy` runs two advisory checks before generating the CloudFormation template:

1. **Existence check** — verifies all external AWS resources (SFN ARNs, Lambda names, Glue jobs, ECS clusters/task definitions, Batch queues/job definitions, Athena workgroups) exist. Aborts deploy if any resource is missing.

2. **Override check** — advisory only, never aborts deploy. For Glue, ECS, and Batch tasks, compares declared task parameters (worker_type, container_overrides, batch_parameters, etc.) against the AWS resource's current defaults. Reports when declared values differ (potential silent runtime overrides), when Glue arguments are in `NonOverridableArguments` (silently ignored at runtime), and when declared values match defaults (can be omitted for cleaner DAGs). Receives `ResourceCheckResult.failed_refs` from the existence check to skip resources already confirmed broken or IAM-inaccessible; throttle/transient errors are excluded from `failed_refs` because the resource may still exist.

Both checks: run on `--dry-run` too; skipped on `--destroy` (no pipeline resources to verify); skip resources in a different IAM role (`task.role != "same"`); convert access-denied to a warning and proceed. Full confidence requires `states:DescribeStateMachine`, `lambda:GetFunction`, `glue:GetJob`, `ecs:DescribeClusters`, `ecs:DescribeTaskDefinition`, `batch:DescribeJobQueues`, `batch:DescribeJobDefinitions`, `athena:GetWorkGroup` on the deploy role.

**SFN edit workflow:** edit `sam/sfn_templates/*/sfn.tpl.json` → `sam build && sam deploy`. `sam build` inlines `.tpl.json` into `DefinitionString` automatically.

### Lambda packaging: `polyris` SDK comes from `requirements.txt` (ADR #102)

The console Lambda and the coordination Lambdas declare the SDK in `sam/lambdas/console_api/requirements.txt` and `sam/lambdas/coordination/requirements.txt`, and import `polyris.coordination` and `polyris.partitions` from it (ADR #130 §10):
```
polyris @ git+https://<PUBLIC_REPO_URL>@vX.Y.Z
```
Both pins name the repository's version; `scripts/release.sh` moves both (`tests/sdk/test_lambda_sdk_pins.py`). To deploy a checkout from source, point both at it (`polyris @ file:///absolute/path`) and never commit that.

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
| `executions_repo` | `pipeline-tokens` | `execution_name` | — | `pipeline-execution-index`, `date-pipeline-index`, `pipeline-date-index`, `backfill-id-index`, `attention-index` (sparse, ADR #130), `run-start-index` (sparse: run rows, ADR #137 §5) |
| — (`Start_Run` writes; the dispatcher and AssetFanout read) | `dep-subscriptions` | `dependency_key` | `subscriber` | `subscriber-index` |
| `pipelines_repo` | `pipeline-registry` | `pipeline_name` | — | — |
| `specs_repo` (reads only; `polyris-deploy` writes) | `pipeline-specs` | `pk` | `sk` | — |
| `asset_events_repo` | `asset-events` | `asset_name` | `event_time` | `date-index`, `asset-partition-index` (sparse, ADR #130) |
| `task_events_repo` | `task-events` | `task_run_id` | `event_time` | `run-index`, `execution-name-index` |
| `asset_subscriptions_repo` | `asset-subscriptions` | `asset_name` | `pipeline_name` | — |
| `api_tokens_repo` | `api-tokens` | `token_id` | — | `hash-index`, `owner-index` |

**Two separate subscription tables, split by semantics (not by consumer):**
- `DependencySubscriptionsTable` — composite key `dependency_key` + `subscriber`. Per-run pointers (ADR #130 §5, ADR #137 §7): a `wait_for` asset (`dependency_key = "asset:{name}"`), with `subscriber` = the waiting task's `execution_name`, `run` = its run row's key and no token. Writer: `Start_Run` (and the dispatcher, which rewrites them for a Restart). Reader: `AssetFanout`, which groups the pointers by `run` into one narrow pass per run, consistently. The dispatcher deletes a row's pointers once it has left the wait (Principle #40).
- `AssetSubscriptionsTable` — composite key `asset_name` + `pipeline_name`. Push consumers only (`schedule=[asset]`): one row per (asset, consumer pipeline). Writer: the pipeline's own registration chain (`Register_Asset_Subscriptions`, at deploy and on every run). Reader: `AssetFanout`, which evaluates the consumer's schedule from its registry entry (ADR #130 §6.1).

The two tables exist because push-trigger subscriptions are per-(asset, pipeline) with no per-run identity, while `wait_for` pointers are per-(asset, waiting task execution). Don't move a subscription between tables based on who reads it — move it based on which schema fits the write.

**Key fields in `pipeline-tokens` (executions_repo):**
- `execution_name` — `{task_name}-{date}-{pipeline_execution_short}`
- `pipeline_execution` — `{pipeline_name}-run-{date}-{hex8}`
- `pipeline_execution_short` — last 20 chars of `pipeline_execution`, strips `.` and `:`
- `status` — see TaskStatus constants
- `task_execution_arn` — ARN of run_task SFN execution
- `readiness` — what the dispatcher decides on (dependencies, trigger rule, `wait_before`, `wait_for`), copied from the spec by `Start_Run` (ADR #137 §8)
- `generation` — the owner's generation (ADR #130 §2.1): writes made for an owner are fenced by it; decisions and restarts raise it

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

### 5 State Machines (2 orchestration + 3 test)

| Name | Type | Purpose |
|------|------|---------|
| `polyris-dep-run-task-helper` | STANDARD | `run_task`: one attempt of a task's work, its outcome and its facts in one write (ADR #137 §1) |
| `polyris-bulk-backfill` | STANDARD | Orchestrates a range of dates for a pipeline |
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

### The subscriptions table mapping

In `template.yaml`, the coordination functions' `SUBSCRIPTIONS_TABLE` (`ReconcileFunction`, `StartRunFunction`, `AssetFanoutFunction`, `SweeperFunction`) → `DependencySubscriptionsTable` ✅. A wrong table once hid the `wait_for` pointers for months (*Error Visibility*).

---

## Lambda Functions

| Function | Purpose | Key env vars |
|----------|---------|-------------|
| `console-api` | All REST API endpoints | `TOKENS_TABLE`, `REGISTRY_TABLE`, etc. |
| `reconcile` | The dispatcher (ADR #137 §1, §7; handler `dispatcher.handler`): woken by the tasks table's stream, it passes over a run's rows, decides each, starts `run_task` and answers the run | `TOKENS_TABLE`, `SUBSCRIPTIONS_TABLE` → `DependencySubscriptionsTable`, `ASSET_EVENTS_TABLE`, `PIPELINES_TABLE`, `SPECS_TABLE` → `PipelineSpecsTable`, `TASK_EVENTS_TABLE` (its decisions in each task's timeline), `RUN_TASK_ARN` |
| `asset-fanout` | Read the `asset-events` stream (INSERT only), make one narrow pass of the dispatcher over each run with a task waiting on the new fact's asset (ADR #137 §7), and start the pipelines scheduled on it (ADR #130 §6.1) | the same as `reconcile`, plus `ASSET_SUBSCRIPTIONS_TABLE` |
| `sweeper` | Every 5 minutes (EventBridge Scheduler): one full pass of the dispatcher over each due run (ADR #137 §7), whose passes alone judge executions; the engine stops a run whose execution ended before its answer or that never became active; stop rows whose run row is missing; warn about lost hints once per run; retry failed push starts, hourly push catch-up, write the engine self-check to `_engine_health` (ADR #130 §8, ADR #137 §6) | the same as `asset-fanout`, plus `ASSET_FANOUT_FUNCTION` and `DISPATCHER_FUNCTION` |
| `start-run` | A run's first state (`lambda:invoke.waitForTaskToken`, ADR #137 §1): read the run's task specs, write the run row with the run's token, the run's input, one row per task and the `asset:` pointers, then mark the run active | `TOKENS_TABLE`, `SUBSCRIPTIONS_TABLE` → `DependencySubscriptionsTable`, `SPECS_TABLE` → `PipelineSpecsTable` |
| `ui-bootstrap` | Custom resource on `polyris-up` stacks: copy prebuilt UI into the console bucket, prune old files, render `config.js`, invalidate CloudFront | — |

**Structured logging:** Lambda handlers use `polyris/logger.py` (`log.info`/`log.warn`/`log.error`) for JSON lines CloudWatch can parse. CLI tools (`cli.py`, `register.py`, `init.py`, `output.py`) use `print()` — correct, they are the event stream consumer. Known gap: `ui_bootstrap` still uses bare `print()` — migrate opportunistically.

---

## Task Status Constants (`polyris/constants.py`)

```python
TERMINAL_STATUSES = {SUCCESS, SUCCEEDED, FAILED, SKIPPED, ABORTED, UPSTREAM_FAILED}  # public: keeps its members
SETTLED_STATUSES = TERMINAL_STATUSES | {STOPPED}  # the engine's "final": a run ends when every row is in it (ADR #137 §4)
SUCCESS_STATUSES = {SUCCESS, SUCCEEDED, SKIPPED}
FAILURE_STATUSES = {FAILED, UPSTREAM_FAILED, ABORTED}  # public; the engine's failures add STOPPED (transitions.FINAL_FAILURES)
WAITING_STATUSES = {WAITING, DEPS_READY, WAITING_PAUSED, WAITING_DECISION}
RECONCILABLE_STATUSES = {WAITING, WAITING_PAUSED}  # the rows whose readiness the dispatcher decides (ADR #137 §1)
```

After changing `polyris/constants.py`: run `python -m polyris.codegen.sync_enums` (Principle #27). The coordination Lambdas import `polyris.constants` from the SDK itself.

---

## API Routes (console_api/main.py)

Routes self-register via `ROUTE_MODULES` list (ADR #97). Each module exposes `register(router)`; `main.py` exposes the assembled `ROUTES` table `(METHOD, '/api/path') → (handler_fn, 'param_key')`.

**Surface (ADR #98, #110):** 24 free routes in the OSS build; the full build adds the Team routes. Free = authoring + basic read + running + live-run intervention (task actions skip/fail/success/stop/restart, execution stop/pause/resume). Team = pipeline-level lifecycle, backfill, assets console, alert integrations, PATs, observability, config mutation.

Path params go via query string (`/api/tokens?id=…`) — single `/{proxy+}` integration means no `template.yaml` change for new routes.

**Auth (ADR #65, #66):** `authenticate()` → `authorize()` at top of `handler()`. Accepts Cognito JWT or PAT (`plrs_…`). Scopes: `read` ⊂ `write` ⊂ `admin`. `AUTH_ENABLED` env: the template sets it from `EnableCognitoAuth`, on by default; unset, it is off. Public paths: `/api/health*`, `/api/metrics`, `/api/action/*`.

**A public route shows an anonymous caller no error text and no resource name.** Build its body from an allowlist. Until 1.2.0 every check of `/api/health` returned the raw AWS error text, account and role names included, to anyone. It now shows its details only to a verified caller (`auth.optional_principal`, ADR #65 v1.2.0 update). `sam/lambdas/console_api/tests/routes/test_health_detail.py` plants such names in failing checks and asserts that none reaches the anonymous answer. `main.handler`'s catch-all answers a public route's unexpected failure with `INTERNAL_ERROR_MESSAGE`, and the paid overlay's action pages show no error text. `response.html_response` escapes what a page echoes, such as the execution name from the link.

When adding an endpoint:
1. **Decide the tier.** Free → `routes/<mod>.py`. Team → `ee/team/<mod>.py`. OSS must never import `ee`.
2. Add handler to module, register in `register(router)`.
3. New module? Add to `main.py`'s `ROUTE_MODULES` (free) or `ee/__init__.py`'s `MODULES` (Team). Existing module needs no `main.py` change.
4. Free handlers also go in `routes/__init__.py` barrel; Team handlers don't.
5. Tests follow tier. `tests/sdk/test_templates.py` asserts the free subset; `tests/docs/test_resource_inventory.py` checks the free-route count these docs state; the tier split in `ee/team/tests/test_route_table_ee.py` is derived.

---

## Backend Patterns

**DAL repositories for all DynamoDB access in `console_api` (100%).** Always use DAL repos:
```python
from dal import executions_repo, pipelines_repo
# Never: boto3.resource('dynamodb').Table('...')
# Never: repo.table.get_item(...)  — use repo.get(key)
```

**"Latest" and "newest first" sort on a key unique in time.** Sort runs and tasks by date *and* `started_at`, never by the date alone: rows that share a date come back in index order, and the sidebar card showed a failed run of the morning over a successful one of the afternoon (1.2.0 upgrade rehearsal; `test_the_card_shows_the_latest_run_of_a_date_with_several`).

**A list says it is complete only when its read reached the end.** A page that read `limit + 1` rows answered `next: null` when the extra row was a twin of its last one, though older runs existed: the read, not the data, had ended there. The Runs tab's date filter cut a date of more than 2,000 task rows on a pipeline boundary and dropped the runs of every pipeline past it without a word. The reader says whether it stopped early (`feed.run_rows_before`), and a list either reads to the end or answers with a cursor or a flag (review of TR-6d; `test_a_page_whose_last_run_has_a_twin_at_the_end_of_the_read_still_says_older_runs_exist`, `test_a_date_filter_shows_every_run_of_a_date_that_outgrows_a_page`).

**A field that a rewrite gives a new meaning is checked in every reader that derives one from it.** In 1.3 `attempt` counts the task's runs; the console showed `attempt − 1` as the operator's Restarts and missed a Restart of a task that never ran, until the Restart counted itself in `restarts` (live smoke of 1.3; `test_a_restart_counts_itself_from_the_count_it_read_and_a_reset_of_a_dependant_counts_nothing`).

**A key is looked up, never recognized by its form.** A run's date takes the form of its granularity (`2026-11-18`, `2026-11-18T05`, `2026-W47`, `2026-11`), and so does the key of each of its rows. The task routes recognized a row's key by a daily date in it and searched for any other as a task's name, so every action on a task of an hourly, weekly or monthly run answered 404 (review of the branch, E-1; `test_the_key_of_a_row_of_an_hourly_weekly_or_monthly_run_finds_its_row`).

**A ProjectionExpression on a GSI names only what that index projects.** DynamoDB rejects the whole query otherwise (`… does not project [...]`); moto does not, so unit tests pass. Read the index without a projection, or with its keys and `NonKeyAttributes` only, and fetch the rest with `batch_get_run_meta`. `tests/sdk/test_console_index_projections.py` checks every index read against `sam/template.yaml`; `/api/tasks` failed with a 500 on every filter until the 1.2.0 live smoke.

One deliberate exception: `notify/registry.py` has no `dal/` and one table accessor — `registry_table()` is the accessor. A DAL for a single function would be over-engineering.

**GSI INCLUDE projections:** when a field is absent from `NonKeyAttributes`, fetch from base table with `executions_repo.batch_get_run_meta` (projects `triggered_by`, `attempt`, `retry_attempt`, `task_config`; chunks at 100, retries `UnprocessedKeys`). Never loop N individual `GetItem` calls.

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

**A fallback that keeps a derived value says it did.** When a check that confirms a value fails, is skipped or comes too late, and the code keeps the value it had, the answer carries a flag the UI shows. `/api/pipelines` kept a run `running` when Step Functions could not confirm it, and its `stats_incomplete` said nothing, so a dead run looked alive (1.2.1 review, ADR #136). In 1.3 the flag says a start day could not be read (`test_a_start_day_that_cannot_be_read_marks_the_stats_incomplete`). When the fallback would look whole and is not, fail the request instead: a registry it could not read was replaced by a tokens scan without groups or schedules, and then by an empty 200 (`test_a_registry_that_cannot_be_read_fails_the_request`).

**A new AWS call in `console_api` needs its IAM action on `ConsoleApiRole` in the same change.** Step Functions calls are pinned by `tests/sdk/test_console_api_sfn_iam_parity.py`; a missing `states:ListStateMachines` left `/api/health` at 503 on every stack until the ADR #131 live smoke caught it.

**Operator actions write rows; the engine does the rest (ADR #137 §3, §4).** The console sends a transaction that `polyris/coordination/actions.py` builds from its reads (a task's Skip, Fail, Mark Successful, Stop, Restart; a run's Stop, Pause, Resume) and stops, starts and answers nothing itself, apart from the `run_task` of a task it stops; a stop that fails warns in the bell, as the dispatcher's does, since the work may run on (review of the branch, P-2). A Stop of a run is its stop mark: the dispatcher stops the unfinished rows dependants first, each stopped row's `run_task`, then the run's execution, and answers the run. Every AWS call a coordination Lambda makes needs its grant in `sam/template.yaml`; `test_reconcile_role_allows_exactly_the_calls_the_code_makes` walks the dispatcher's paths, so a new path joins that walk (the renewal's pointer write lacked `dynamodb:PutItem` until the walk took a Restart).

**A repeated action is a no-op only while the first one is in progress.** Once the state it left is gone, it acts again or refuses, and its answer says what it did, not what was asked. A Stop of a run whose stop was done, after a Restart ran its task again, answered "stopping" and changed nothing, and a Pause was refused for good (review of TR-6; `test_a_stop_after_a_done_stop_and_a_restart_stops_the_rerun`).

**A backoff holds the whole run it backs off.** A run whose pass failed for good waits for its run row's due time though rows of it are due, since their checks would meet the same failure: the sweeper moved only the run row's due time and checked the run first at every sweep (review of the branch, P-1; `test_a_broken_run_waits_for_its_backoff_not_every_sweep`).

**A step a pass repeats until its request clears records its progress or keeps off the decisions' path.** A Restart's pointer step rewrote the pointers of every waiting row before any decision, on every pass until it cleared, and under a Stop wrote pointers it deleted at once; it writes those of the rows waiting since its request alone, keeps the first request's time, and none under a Stop (review of TR-6; `test_a_restart_writes_the_pointers_of_the_rows_it_put_back_to_wait_and_renews_nothing`).

---

## CloudFormation custom resources (Lambda-backed)

Rules from latent `UiBootstrap` bugs found when ADR #131 first enabled it:
1. **Bundle `cfnresponse.py`.** The runtime only injects `cfnresponse` for inline (`ZipFile`) functions; with `CodeUri` the import fails and the stack waits an hour. Copy `sam/lambdas/bucket_cleanup/cfnresponse.py` (a parity test keeps copies identical).
2. **Stable `PhysicalResourceId`.** Pass it explicitly (derive it from a property such as the bucket name), never default to `context.log_stream_name`. A changed id makes CloudFormation follow every Update with a Delete of the "old" resource — which for `UiBootstrap` wiped the UI it had just installed.
3. **Delete ≠ teardown.** A Delete also fires when an update removes the resource. Destroy data only if the parent stack status is `DELETE_IN_PROGRESS` / `ROLLBACK_IN_PROGRESS`; on any doubt keep it, and always answer SUCCESS on Delete.
4. **`s3.copy_object` ignores `CacheControl` / `ContentType` unless `MetadataDirective="REPLACE"`** — and with REPLACE you must pass `ContentType`, or it becomes `binary/octet-stream`.

---

## Error Visibility (ADR #38)

**Lambda rules:**
- Never swallow `AccessDeniedException` — always `raise`. Exception: a stream consumer (the dispatcher, AssetFanout). Its mapping retries a failed batch without end, so a raise would hold up the shard for 24 hours; it writes a `coordination` warning and leaves the row to its next check instead (ADR #137 §7)
- Every `except Exception` must log `error=str(e)` with function context
- If an error blocks downstream tasks → write `_notify_warn_` record to `pipeline-tokens`
- An error that every attempt meets ends the row with the cause, not only a warning: a `deps_ready` row whose spec text or run input is gone stayed `deps_ready`, started again at each liveness check (review of the branch, P-4; `test_a_start_whose_spec_text_is_gone_fails_its_task_with_the_cause`)
- A catch on a wake-up path (subscription write/query, `send_task_success`, a fail-open fallback) logs at `log.error`, never `log.warn` — a swallowed WARN is what hid the historical `wait_for` subscription-table schema mismatch for months (DAL wrote `dependency_key`/`subscriber`, env var pointed at `AssetSubscriptionsTable` which keyed on `asset_name`/`pipeline_name`); the ValidationException was caught and logged as warning

**`_notify_warn_` pattern** (infrastructure errors → Notifications bell in UI, ADR #130 §9). Build every warning row with `records.warning_item` — never by hand:
```python
from polyris.coordination.records import warning_item

executions_repo.put(warning_item(
    kind="coordination",                # a WarningKind; add a member there for a new kind
    execution_name=execution_name, task_name=task_name, pipeline_name=pipeline_name,
    pipeline_execution=pipeline_execution, task_date=date,
    error=f'Context: {error}', now=datetime.now(timezone.utc),
))
```
The row carries no `date`: the bell reads it from `attention-index` (`warn#{h}`) by write time, so a warning about an old partition still shows. A row with a `date` and no `attention` reaches the bell only while that date is in its window — the B-N1 bug. SFN templates and generated ASL write the same shape; a contract test compares each with `warning_item`.

**Special prefixes in `pipeline-tokens`** — internal records:
- `_run_{pipeline_execution}` — the run row: its token, stop mark, pauses (`pauses`) and requests (ADR #137 §5)
- `_pause_{pipeline_execution}` — a 1.2 run's pause; 1.3 writes none, and those 1.2 left expire
- `_notify_warn_{kind}_{execution_name}` — infrastructure warning (`records.warning_item`)

**CRITICAL:** All console loops iterating `pipeline-tokens` items MUST skip internal rows with `utils.should_skip_token_row(item)` (or `is_internal_record(name)`), never with a prefix check of their own. (The dispatcher reads its run's rows by key, and the sweeper only the due entries of the `waiting` and `decision` kinds, so neither meets an internal row.)
```python
for item in items:
    if should_skip_token_row(item):
        continue
```
Internal rows are `_`-prefixed (the run row `_run_{execution}` and its input `_run_{execution}#input`, `_pause_`, `_notify_warn_`, `_retry_push_`, `_engine_health`, `_sweeper_lease`), the stores `output#` and `input#`, and the run's DAG snapshot `dag_snapshot::{run}`. Violating this → `_notify_warn_` appears in All Tasks / pipeline status / runs. A `_`-only check let the snapshot, which shares the run's `pipeline_execution`, through: a task without a status, and an empty `/api/execution-children` for every run.

**SFN templates:** `Catch` blocks must preserve `$states.errorOutput` in Output. A state after a point of no return has a Catch that reaches the bell: in 1.2, a restart that stopped the old wrapper first and then failed to start the new one left the task dead while the console said "Task restarted". Never record a success apart from what depends on it: `run_task` writes the status, the output and the facts in one transaction, and a success it cannot record waits for an operator's decision with the cause (ADR #137 §1). 1.2 wrote the facts after the success, and a fact it could not record left its consumers waiting with no trace.

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
python3 -m pytest tests/sdk/ tests/backend/ tests/integration/ -q
make test-docs                                     # own session (see Repository context)
cfn-lint sam/template.yaml                         # 0 errors

# Full suite: every pytest dir, the Lambda suites, JSONata and the UI
make test

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
- Every repository method the console calls exists on the repository. The Stop route called `pipelines_repo.get_sfn_arn`, which only a test fake defined; no test called the route, and every Stop answered 500. `sam/lambdas/console_api/tests/test_repository_calls.py` reads the code of the console and of the paid overlay, so a route no test reaches is covered too.
- SDK and backend tests are hermetic: `tests/sdk/conftest.py` clears `AWS_*` / `POLYRIS_*` variables and points the AWS config files at an empty place before every test, and `tests/backend/conftest.py` clears the `AWS_*` ones and sets fake credentials before any test module loads, so no call can reach a real account (a run of `test_api_routes.py` once sent a real DynamoDB Query with the developer's default profile, TR-6d). A test that needs a profile, region or stage sets it with `monkeypatch`. Never rely on the developer's shell or `~/.aws` — the v1.1.4 release gate failed on a leftover `AWS_PROFILE`.
- Backend tests in `tests/backend/`, SDK tests in `tests/sdk/`
- A test client that talks to moto uses the endpoint moto serves. CI installs the newest botocore and moto, which can disagree: botocore 1.44.4 sent DynamoDB Streams calls to `{account}.streams-ddb.{region}`, moto 5.2.5 answered 404 "Not yet implemented", and every coordination suite failed. The Streams clients in `tests/backend/coordination/world.py` and the asset-fanout tests pass `Config(account_id_endpoint_mode="disabled")`.
- Whatever runs this checkout's Python in another process puts the checkout's root on `PYTHONPATH`: a test's subprocess, the Lambda suites of `make test-lambdas` and `scripts/verify-changed.sh`, the smoke's `polyris-deploy`. The SDK may be an editable install of another checkout: the route count in `tests/docs/test_resource_inventory.py` imported that one, and failed the 1.2.1 release gate while it lagged. `python -m pytest` from the root needs nothing: the current directory comes first. CI installs the checkout under test.
- **Lambda suites are hermetic too.** `make test-lambdas` runs them with no AWS profile, region or credentials (`HERMETIC_AWS`), as CI does. A suite whose module builds a boto3 client at import sets `AWS_DEFAULT_REGION` (and fake credentials for moto) before importing it; `bucket_cleanup` passed locally and failed in CI on `NoRegionError`.
- **Every Lambda test suite runs in CI.** The `lambdas` job in `ci.yml` lists each suite as its own step, and `make test-lambdas` runs the same suites locally. A Lambda with `tests/` or `test_*.py` needs both, or its tests silently never run; `tests/sdk/test_ci_runs_every_suite.py` fails until it has them, and also until every `testpaths` directory in `pyproject.toml` runs from a make target that `ci.yml` calls and `make test` includes.
- Coverage floor is a ratchet — raise the floor when you raise coverage (#22)
- **A harness check that retires keeps its guards.** ADR #130's invariants also failed a scenario on any template expression error, caught or not (D107). When the World moved onto 1.3's rows and stopped running them, that check stopped too, silently, while this file still promised it. Before you stop running a check, list everything it checks; each guard the docs still promise moves to what runs now, with a test that it fails a scenario (`test_rows_view.py`).
- **`dal/__init__.py` submodule shadowing:** `from dal.executions_repo import executions_repo` replaces the submodule attribute on the `dal` package with the singleton instance. Tests patching module-level globals on a `dal` submodule must use `importlib.import_module('dal.executions_repo')` to get the actual module object.

---

## SFN Templates — Common Pitfalls

1. **`.tpl.json` is not valid JSON** — `${var}` breaks `json.load()`. CI strips numeric vars with regex.
2. **Express SFN via sync:2 fails silently** — use `startSyncExecution` instead.
3. **DefinitionUri vs inline** — always edit `sfn_templates/`, never inline in `template.yaml`.
4. **pipeline_execution_short** — last 20 chars of `pipeline_execution`, with `.` and `:` stripped. Format: `-{date}-{hex8}`.
5. **JSONata on a field that may be missing.** `$not(x)` of a missing `x` is undefined, and `and` with undefined is false; a comparison with a missing field (`x != true`) is false too. Write the absence out: `$not($exists(x) and x = true)`. The failure handler's `$not($states.input.helper_failed)` was false whenever the field was absent, so a task whose wait timed out never answered its run, which stayed `RUNNING` (F16, 1.2.0 rehearsal; `test_a_dependency_wait_that_times_out_fails_the_task_once`). A field whose expression evaluates to undefined fails the state with `States.QueryEvaluationError`, since JSON cannot represent undefined, and a `Catch` on `States.ALL` hides that: `Failure_Complete` read fields its input no longer carried, and the failure handler failed live while every scenario passed (1.2.0, CR-7). Never let an expression error steer a template: test the field with `$exists` in a `Choice`. The coordination harness fails like AWS on undefined, and a scenario fails on any expression error, caught or not (D107; defect `B-Q1`).
6. **Whoever ends a task answers its run, and a closed token is fine (ADR #135).** In 1.3 the dispatcher answers a run once every row is final, and no template answers one: `run_task` answers none (ADR #137 §4). Never skip the answer because a flag says another state machine gave it. `helper_failed` claimed that `run_task` had answered, and twice it had not: F16, and a pause token that could not be saved. A zombie that the reconciler closed had no answer at all, and the wrapper's skip answer and the console's task actions swallowed a failed one. Retry nothing on the closed-token codes (`DEAD_TOKEN_CODES`), and turn any other error into a warning (`Warn_Run_Left_Running` in a template, the dispatcher's `_answer`). The console answers no run: its actions write rows (ADR #137 §3). The one exception is a superseded owner: a restart handed its token on, and the new owner answers. `tests/sdk/test_run_answers.py` fails when a template answers a run, and harness invariant 10 fails a scenario that leaves a run `RUNNING` after its tasks have finished.
7. **A value a template stores for another to parse is JSON, a string included.** `$string(x)` of a string is the string itself (`ok`, not `"ok"`), which no reader can parse: a task that returned a string blocked its dependant (the review of TR-5). Encode a string as `run_task`'s work states do (`$string([$x])` without its brackets) unless it starts like JSON: that is JSON text (`json.dumps`), which 1.2 stored as it was and read parsed, and encoding it changed what dependants got (the review of TR-5e). Writer and reader decide "starts like JSON" by the same characters (`test_a_result_is_stored_as_json_text_as_1_2_read_it`). A reader of a store an older version wrote takes what that version wrote: `run_task` parses only a text that starts like JSON, and `xcom.pull` and the console return any other text as the string.
8. **A `Retry` never depends on error names nobody has seen.** List what must not be retried (`MaxAttempts: 0`: a failed condition, a cancelled transaction, an expression error, a payload too large), then `States.ALL`. `run_task` retried four named DynamoDB errors for seven seconds, and a claim failed its task for good on a 5xx it did not name (the review of TR-5; `test_every_dynamodb_state_retries_any_error_but_an_answer_for_minutes`). After an error whose write may have applied, read the row before sending the write again.
9. **A catcher never parses what it caught.** Step Functions cuts a cause at 32,768 characters, so `$parse` of a cause can fail the catcher itself, and the state then fails without its retries: match what you need (`Shape_Failure`). A name cut to a service's limit keeps its unique tail (`Run_Task_SFN`'s child execution).

---

## Documentation

After significant changes:
- `CHANGELOG.md` — new version entry
- `docs/reference/DESIGN_DECISIONS.md` — new ADR
- Version must match across: `pyproject.toml`, `polyris/__init__.py`, `ui/package.json`
- `CHANGELOG.md` opens with `## Unreleased`; add your entry under it. `scripts/release.sh` refuses an empty one and renames it to `## vX.Y.Z - <date>` (`scripts/changelog_release.py`). Nothing did before, and releases from 1.0.2 to 1.2.2 shipped their notes under `## Unreleased`.
- A template change that adds or removes a resource updates the counts and inventory tables in `INFRASTRUCTURE.md`, `IAM_PERMISSIONS.md`, `BACKEND.md`, `README.md` and the State Machines table above in the same commit. `tests/docs/test_resource_counts.py` and `tests/docs/test_resource_inventory.py` derive every number and name from `sam/template.yaml` (and the free route count from the console) and fail until the docs agree. Run `make test-docs`.

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
| 23 | DAG lookup | superseded by ADR #137 §8: the run's spec version (a 1.2 run's snapshot until 1.4) → registry → inferred |
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
