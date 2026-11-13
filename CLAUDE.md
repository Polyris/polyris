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

When adding or removing a value: (1) edit `polyris/constants.py`, (2) register in `sync_enums.py` if new class, (3) run `python -m polyris.codegen.sync_enums` — mandatory even for removals (CI `make check-generate-enums` will fail if skipped), (4) import the enum everywhere — never bare strings, (5) add a parity test (#28).

**28. Constants coupled across languages/artifacts need a parity test**
Any string written in one artifact and read verbatim in another must have a parity test asserting the same value on both sides. The type system can't enforce string parity across JSON templates or Python↔TypeScript.

Examples: `_PUSH_MARKER_FIELD` written by `xcom.push()`, read by SFN template — parity in `test_xcom_coupled_constants_parity.py`. Marker fields `_manually_resolved`/`_resolution`/`_reason`/`_operator` — same test. `SCHEDULE_PRESETS` ↔ `infer_cron_cadence` — `TestSchedulePresetsParity` in `test_granularity.py`. Generated enums covered by `codegen --check`.

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

This applies to docstring examples too — a docstring that shows `task_definition='my-task-def:3'` misleads users into using the pinned form.

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
- it surfaces in the console as a warning (bell) or as the engine-health banner.

It is always logged with `error=str(e)`. See the [Error Visibility section](#error-visibility-adr-38) for the `_notify_warn_` pattern.

Production code and tests cite "Principle #38" for error visibility. Never renumber this entry.

**39. When a new field replaces an old one in a multi-reader contract, keep writing the old field until every reader is migrated**
If code A introduces a field that supersedes an old one while code B still falls back to the old field, A must write both. Stopping the old write silently breaks B's fallback, often only in one timing window.

Correct: write both until every reader is updated and deployed. If every reader is replaced in the same deploy, nothing is left to fall back.
Anti-pattern: write only the new field because "the old one is deprecated" while B still reads it.

Before removing the write of an old field, `grep -rn` for every reader that could fall back to it.

**40. Delete a wake-up pointer only after the signal it serves was accepted — never before**
A subscription, or any pointer that says "wake this waiter", may be deleted only after the signal it exists for was accepted. If you delete first and then fail to signal, nothing will ever re-check the waiter.

In the reconciler (ADR #130 §3), a run's pointers are deleted only after an accepted signal. Leftover pointers are harmless no-ops that expire by TTL.

**41. Wake-up hints may be lost; wake-up decisions may not**
- **Hint paths** only say "check this waiter again": the asset-events stream, `notify_dependents`, registration, resume. They may fail or drop a hint, because a periodic re-check from facts (the sweeper, ADR #130 §8) recovers it.
- **Decision and action paths** never fail silently: the reconciler, the wrapper's status write, and every write of a fact. They retry, fail the affected task with the real cause, or surface a console warning (#38). A fact is state, not a hint: nothing re-checks a fact that was never written, so Mark Successful turns a failed fact write into a `manual` warning (B-M2).

Anti-pattern: a wake-up path whose loss nothing re-checks, for example a fire-and-forget invoke with no periodic re-check behind it.

**42. A second bug of the same class in one mechanism stops the patching — question the model first**
When a mechanism produces a second bug of the same class (e.g. "task silently hangs until timeout"), do not patch it a third time. Stop and write down, before any code:
- what the bug class is;
- why the current model keeps producing it;
- what model would make it impossible.

Then decide with the maintainer: patch now and schedule the model change, or change the model now. Record the decision in an ADR.

**Why:** a flag-based AND gate drafted on an unmerged branch (superseded by ADR #130) produced four separate silent-hang bugs, each fixed locally. The root cause was the model: readiness was accumulated from events instead of evaluated from facts. Each local fix looked like an isolated oversight; together they were a signal.

**How to apply:** keep a running count per mechanism during a branch. "Same class" means the same user-visible failure, not the same line of code. This is the escalation step of Principle #5. It does not forbid shipping the patch, but the model question must be asked and answered.

**43. The receiver of a signal owns the status transition that follows it**
When component A signals component B (e.g. `SendTaskSuccess` to a waiting wrapper), B writes the resulting status, not A. If A writes it, two things break:
- a crash between the signal and the write loses the status;
- a consumed token cannot be told apart from an expired one.

Under ADR #130 the wrapper writes `deps_ready`, `skipped` or `upstream_failed` itself, and the reconciler only signals.

**44. A guard for an optional overlay import catches only that module's absence**
`try: import ee` must catch only a `ModuleNotFoundError` whose `name` is the overlay module. If it catches every `ImportError`, a broken import inside the overlay silently removes every route the overlay provides.

**45. No custom CloudWatch metrics, alarms, SNS topics or DLQs in the platform template**
Code computes engine health on demand and shows it in the console (ADR #130 §9). Failures follow #38 and #41. Custom metrics, alarms, topics and queues cost every user money and need someone to watch them.

A template test enforces this rule. Adding any of them needs an explicit maintainer decision recorded in an ADR.

**46. A task token handed over through a write is findable by the sweeper, and a failed write fails the token**
When a state stores its `waitForTaskToken` for another component to send (`pause_waiter` saves `pause_token` for resume):
- the same write gives the row an attention entry (`paused#`), so the sweeper releases the task if the sender missed it. A sender that finds tokens through an index or one scan misses a token written while it ran (B-P3);
- a failed write is caught and fails the token with the cause. Nothing else will ever hold that token, so without the catch the task waits for its heartbeat and then runs anyway (B-P4);
- adoption and any other backfill of attention entries route a row by the same rule as its writer: a row holding a pause token goes to `paused#`, not `waiting#`.

**47. A task row has one owner at a time, named by its generation**
Every write made for an owner (the wrapper, registration, `run_task`, the pause wait, the failure handler) carries `ownership.FENCE` with the owner's generation. Every write that ends an ownership (an operator's decision, a restart, the reconciler closing a zombie) raises it with `ownership.RAISE`, in the same update that records the decision. A fenced write ends its writer quietly: no status, no notification, no work. Six bugs came from writers that checked something else or nothing (B-O1…B-O5, B-W1); see ADR #130 §2.1. `tests/sdk/test_task_row_writers.py` lists every writer of the tokens table, and a new one fails it until it has a role there.

A retry after a lost answer must accept the row it already moved: a claim accepts its own target status under its own generation (B-W1). Test such a write by applying it twice.

**48. A value an ADR defines clause by clause is one pure function with a test per clause**
The enabling time (ADR #130 §9) has four clauses: latest required dependency, earliest success for `one_*`, earliest ready member of an OR group, never before the resume. Computed inline in the reconciler, three of them were missing, and the sweeper reported false misses after every resume over budget. Put such a definition in `polyris/coordination/` (here `trigger_rules.enabled_at`, `Evaluation.enabled_at`) and test each clause.

**49. A helper template reads an input an older caller may omit through `$exists`**
During an upgrade, wrappers started on the old definitions keep calling the new helpers until they finish. `$string()` of a missing field fails the state, and with it the task: a pre-upgrade wrapper registering again failed on `starter_input`. Default such a field to a value its readers already treat as absent (`''`, `0`), and pin it with a test that runs the helper on the old caller's input (`PRE_UPGRADE_WRAPPER_INPUT` in `tests/sdk/test_registration_template.py`).

**50. A time that marks a beginning is written once**
When the writer runs again, as registration does on every run, write the field with `if_not_exists`. Otherwise it moves forward and silently narrows whatever it gates: `subscribed_at` decides which facts a push consumer reacts to, and a moving one would hide the facts a lost stream record left for the catch-up (B-U2).

**51. A reader of shared rows skips a row it cannot parse, and says so**
One malformed row must not fail every decision that reads the table. Catch the parse error per row, count the row for nothing, and write a warning. `FactStore.read` does this for asset facts (B-F2).

**52. A time budget is checked per item, and a periodic job's steps fail independently**
A budget checked only between batches lets one large item run the function into its timeout: AssetFanout checked it between records, so a record with many waiters was retried for 24 hours and blocked its shard, and the catch-up checked it between assets, so an asset with many facts never finished. Check the deadline before each item, give up the rest as lost hints where a re-check exists (#41), and keep a cursor inside the item's group (`catchup_within`). A periodic job runs each step in its own `try`: a failing step must not skip the later steps or the self-check, and the health report names it (`sweep.STEPS`, `failed_steps`).

**53. A Lambda's AWS calls fail inside the Lambda's timeout**
botocore's defaults (ten DynamoDB attempts, a 60 s read timeout) outlast a 30 s function, and a function that times out cannot report its own transient error, so the caller sees `Sandbox.Timedout`. Build clients with a short retry and timeout config (`env._config` in the coordination Lambdas), and let the caller's `Retry`/`Catch` treat the platform's own failures (`Sandbox.Timedout`, `Lambda.Unknown`) and any HTTP 5xx as transient.

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
  lambdas/
    console_api/      # REST API (28 free routes; the ee/ overlay adds the Team routes), DAL repos, route handlers
    coordination/     # reconcile, asset-fanout, sweeper (ADR #130): decide readiness, signal wrappers, start push consumers, re-check
    query_subscriptions/  # Finds downstream subscribers for a completed task
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

# Contributors: from a checkout
cd sam && sam build && sam deploy --profile <profile>
cd ui && npm ci && npm run build && ./deploy.sh <stack> <region> --profile <profile>

# Pipeline
cd pipelines/my-pipeline && polyris-deploy --stage dev --profile <profile>
```

**Release contract (ADR #131).** Every `v*` tag must publish `polyris-platform-vX.Y.Z.zip` (`.github/workflows/release.yml` → `scripts/build_platform_release.py`). CI owns the GitHub Release: draft → attach assets → publish, never the other way round — the repo keeps *Immutable releases* on, so a release published before its assets can never get them; `polyris-up` downloads the one matching `polyris.__version__`. The archive layout lives in `polyris/platform.py` (`Manifest`), imported by both sides — change it there, and bump `MANIFEST_FORMAT` if an older `polyris-up` can't read the new layout. A new `AWS::Serverless::Function` or `AWS::Serverless::StateMachine` with a local `CodeUri` / `DefinitionUri` is picked up automatically. Any other `AWS::Serverless::*` local path (`CodeUri`, `ContentUri`, `DefinitionUri` — e.g. a layer) fails `rewrite_template`, and with it the PR's *Platform archive* CI job, until `scripts/build_platform_release.py` packages it.

**Neither `polyris-up` nor `polyris-deploy` may shell out to the `aws` / `sam` CLIs** — deploys go through `polyris/cfn_changeset.py` (boto3 change sets). The user-side promise is "Python and AWS credentials".

**Pre-deploy resource check:** `polyris-deploy` runs two advisory checks before generating the CloudFormation template:

1. **Existence check** — verifies all external AWS resources (SFN ARNs, Lambda names, Glue jobs, ECS clusters/task definitions, Batch queues/job definitions, Athena workgroups) exist. Aborts deploy if any resource is missing.

2. **Override check** — advisory only, never aborts deploy. For Glue, ECS, and Batch tasks, compares declared task parameters (worker_type, container_overrides, batch_parameters, etc.) against the AWS resource's current defaults. Reports when declared values differ (potential silent runtime overrides), when Glue arguments are in `NonOverridableArguments` (silently ignored at runtime), and when declared values match defaults (can be omitted for cleaner DAGs). Receives `ResourceCheckResult.failed_refs` from the existence check to skip resources already confirmed broken or IAM-inaccessible; throttle/transient errors are excluded from `failed_refs` because the resource may still exist.

Both checks: run on `--dry-run` too; skipped on `--destroy` (no pipeline resources to verify); skip resources in a different IAM role (`task.role != "same"`); convert access-denied to a warning and proceed. Full confidence requires `states:DescribeStateMachine`, `lambda:GetFunction`, `glue:GetJob`, `ecs:DescribeClusters`, `ecs:DescribeTaskDefinition`, `batch:DescribeJobQueues`, `batch:DescribeJobDefinitions`, `athena:GetWorkGroup` on the deploy role.

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
| `executions_repo` | `pipeline-tokens` | `execution_name` | — | `pipeline-execution-index`, `date-pipeline-index`, `pipeline-date-index`, `backfill-id-index`, `attention-index` (sparse, ADR #130) |
| `subscriptions_repo` (query_subscriptions `qs_dal`) | `dep-subscriptions` | `dependency_key` | `subscriber` | `subscriber-index` |
| `pipelines_repo` | `pipeline-registry` | `pipeline_name` | — | — |
| `asset_events_repo` | `asset-events` | `asset_name` | `event_time` | `date-index`, `asset-partition-index` (sparse, ADR #130) |
| `task_events_repo` | `task-events` | `task_run_id` | `event_time` | `run-index`, `execution-name-index` |
| `asset_subscriptions_repo` | `asset-subscriptions` | `asset_name` | `pipeline_name` | — |
| `api_tokens_repo` | `api-tokens` | `token_id` | — | `hash-index`, `owner-index` |

**Two separate subscription tables, split by semantics (not by consumer):**
- `DependencySubscriptionsTable` — composite key `dependency_key` + `subscriber`. Per-run pointers (ADR #130 §5): a task dependency (`dependency_key = "{upstream}-{pipeline_execution_short}"`) or a `wait_for` asset (`dependency_key = "asset:{name}"`), with `subscriber` = the waiting task's `execution_name` and no token. Writer: the registration SFN. Readers: `query_subscriptions` (for `notify_dependents`) and `AssetFanout`, both consistent. The reconciler deletes a task's pointers after its signal was accepted (Principle #40).
- `AssetSubscriptionsTable` — composite key `asset_name` + `pipeline_name`. Push consumers only (`schedule=[asset]`): one row per (asset, consumer pipeline). Writer: the pipeline's own registration chain (`Register_Asset_Subscriptions`, at deploy and on every run). Reader: `AssetFanout`, which evaluates the consumer's schedule from its registry entry (ADR #130 §6.1).

The two tables exist because push-trigger subscriptions are per-(asset, pipeline) with no per-run identity, while dep/wait_for subscriptions are per-(upstream, subscriber-task-execution). Don't move a subscription between tables based on who reads it — move it based on which schema fits the write.

**Key fields in `pipeline-tokens` (executions_repo):**
- `execution_name` — `{task_name}-{date}-{pipeline_execution_short}`
- `pipeline_execution` — `{pipeline_name}-run-{date}-{hex8}`
- `pipeline_execution_short` — last 20 chars of `pipeline_execution`, strips `.` and `:`
- `status` — see TaskStatus constants
- `wrapper_execution_arn` — ARN of dependency_wrapper SFN execution
- `task_execution_arn` — ARN of run_task SFN execution
- `orchestration_token` — `.waitForTaskToken` for signaling deps ready
- `wait_token` — token for notify_dependents to signal ready
- `generation` — the owner's generation (ADR #130 §2.1): writes made for an owner are fenced by it; decisions and restarts raise it

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

### 13 State Machines (10 orchestration + 3 test)

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
| `polyris-register-pipeline` | EXPRESS | Deployed but orphaned — not called by any current code path; superseded by the inline `Register_Pipeline` state in each generated pipeline SFN |
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
| `reconcile` | Decide whether a waiting task is ready and signal its wrapper (ADR #130 §3) | `TOKENS_TABLE`, `SUBSCRIPTIONS_TABLE` → `DependencySubscriptionsTable`, `ASSET_EVENTS_TABLE`, `PIPELINES_TABLE` |
| `asset-fanout` | Read the `asset-events` stream (INSERT only) and reconcile every task waiting on the new fact's asset (ADR #130 §6.1) | the same as `reconcile` |
| `sweeper` | Every 5 minutes (EventBridge Scheduler): re-check the waiting tasks that are due, warn about lost hints, abort zombies past their liveness deadline, release tasks a resume missed from their pause wait, retry failed push starts, hourly push catch-up, adopt rows an older version wrote, write the engine self-check to `_engine_health` (ADR #130 §8) | the same as `asset-fanout`, plus `ASSET_FANOUT_FUNCTION` |
| `ui-bootstrap` | Custom resource on `polyris-up` stacks: copy prebuilt UI into the console bucket, prune old files, render `config.js`, invalidate CloudFront | — |

**Structured logging:** Lambda handlers use `polyris/logger.py` (`log.info`/`log.warn`/`log.error`) for JSON lines CloudWatch can parse. CLI tools (`cli.py`, `register.py`, `init.py`, `output.py`) use `print()` — correct, they are the event stream consumer. Known gap: `ui_bootstrap` still uses bare `print()` — migrate opportunistically.

---

## Task Status Constants (`polyris/constants.py`)

```python
TERMINAL_STATUSES = {SUCCESS, SUCCEEDED, FAILED, SKIPPED, ABORTED, UPSTREAM_FAILED}
# NOTE: STOPPED is NOT terminal — task can be restarted
SUCCESS_STATUSES = {SUCCESS, SUCCEEDED, SKIPPED}
FAILURE_STATUSES = {FAILED, UPSTREAM_FAILED, ABORTED}
WAITING_STATUSES = {WAITING, DEPS_READY, WAITING_DELAY, WAITING_PAUSED, WAITING_DECISION}
RECONCILABLE_STATUSES = {WAITING, WAITING_PAUSED}  # the only rows the reconciler acts on (ADR #130)
```

After changing `polyris/constants.py`: run `python -m polyris.codegen.sync_enums` and `python -m polyris.codegen.sync_coordination` (Principle #27).

---

## API Routes (console_api/main.py)

Routes self-register via `ROUTE_MODULES` list (ADR #97). Each module exposes `register(router)`; `main.py` exposes the assembled `ROUTES` table `(METHOD, '/api/path') → (handler_fn, 'param_key')`.

**Surface (ADR #98, #110):** 28 free routes in the OSS build; the full build adds the Team routes. Free = authoring + basic read + running + live-run intervention (task actions skip/fail/success/stop/restart, execution stop/pause/resume/extend). Team = pipeline-level lifecycle, backfill, assets console, alert integrations, PATs, observability, config mutation.

Path params go via query string (`/api/tokens?id=…`) — single `/{proxy+}` integration means no `template.yaml` change for new routes.

**Auth (ADR #65, #66):** `authenticate()` → `authorize()` at top of `handler()`. Accepts Cognito JWT or PAT (`plrs_…`). Scopes: `read` ⊂ `write` ⊂ `admin`. `AUTH_ENABLED` env (default `false`). Public paths: `/api/health*`, `/api/metrics`, `/api/action/*`.

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

**A new AWS call in `console_api` needs its IAM action on `ConsoleApiRole` in the same change.** Step Functions calls are pinned by `tests/sdk/test_console_api_sfn_iam_parity.py`; a missing `states:ListStateMachines` left `/api/health` at 503 on every stack until the ADR #131 live smoke caught it.

**Stop pipeline hierarchy** (always stop in this order):
1. Pipeline SFN execution (reconstruct ARN: `sfn_arn.replace(':stateMachine:', ':execution:') + ':' + pipeline_execution`)
2. All `wrapper_execution_arn` from task items (deduplicated)
3. All `task_execution_arn` from task items
4. Update DynamoDB → `stopped`/`aborted`

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
- Never swallow `AccessDeniedException` — always `raise`
- Every `except Exception` must log `error=str(e)` with function context
- If an error blocks downstream tasks → write `_notify_warn_` record to `pipeline-tokens`
- A catch on a wake-up path (subscription write/query, `send_task_success`, a fail-open fallback) logs at `log.error`, never `log.warn` — a swallowed WARN is what hid the historical `wait_for` subscription-table schema mismatch for months (DAL wrote `dependency_key`/`subscriber`, env var pointed at `AssetSubscriptionsTable` which keyed on `asset_name`/`pipeline_name`); the ValidationException was caught and logged as warning

**`_notify_warn_` pattern** (infrastructure errors → Notifications bell in UI, ADR #130 §9). Build every warning row with `records.warning_item` — never by hand:
```python
from coordination_core.records import warning_item

executions_repo.put(warning_item(
    kind="notify",                      # a WarningKind; add a member there for a new kind
    execution_name=execution_name, task_name=task_name, pipeline_name=pipeline_name,
    pipeline_execution=pipeline_execution, task_date=date,
    error=f'Context: {error}', now=datetime.now(timezone.utc),
))
```
The row carries no `date`: the bell reads it from `attention-index` (`warn#{h}`) by write time, so a warning about an old partition still shows. A row with a `date` and no `attention` reaches the bell only while that date is in its window — the B-N1 bug. SFN templates and generated ASL write the same shape; a contract test compares each with `warning_item`.

**Special prefixes in `pipeline-tokens`** — internal records:
- `_pause_{pipeline_execution}` — pause state
- `_notify_warn_{kind}_{execution_name}` — infrastructure warning (`records.warning_item`)

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
- SDK tests are hermetic: `tests/sdk/conftest.py` clears `AWS_*` / `POLYRIS_*` variables and points the AWS config files at an empty place before every test. A test that needs a profile, region or stage sets it with `monkeypatch`. Never rely on the developer's shell or `~/.aws` — the v1.1.4 release gate failed on a leftover `AWS_PROFILE`.
- Backend tests in `tests/backend/`, SDK tests in `tests/sdk/`
- **Lambda suites are hermetic too.** `make test-lambdas` runs them with no AWS profile, region or credentials (`HERMETIC_AWS`), as CI does. A suite whose module builds a boto3 client at import sets `AWS_DEFAULT_REGION` (and fake credentials for moto) before importing it; `bucket_cleanup` passed locally and failed in CI on `NoRegionError`.
- **Every Lambda test suite runs in CI.** The `lambdas` job in `ci.yml` lists each suite as its own step, and `make test-lambdas` runs the same suites locally. A Lambda with `tests/` or `test_*.py` needs both, or its tests silently never run; `tests/sdk/test_ci_runs_every_suite.py` fails until it has them, and also until every `testpaths` directory in `pyproject.toml` runs from a make target that `ci.yml` calls and `make test` includes.
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
