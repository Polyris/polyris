# Completeness Report — ADR #114/#115/#116 + Phase 1.1 (skip-fix)

Per CLAUDE.md Principle #23. Covers everything delivered this session: three ADRs and
the full Phase 1.1 implementation (`docs/reference/PLAN_intervention_and_trigger_rules.md`).

## Changed

**Decisions (ADR, #6):**
- `docs/reference/adr-114-intervention-first-failure-model.md` (new)
- `docs/reference/adr-115-canonical-trigger-rules-and-skip-semantics.md` (new)
- `docs/reference/adr-116-cleanup-scoped-deabort.md` (new)
- `docs/reference/adr-index.md` — added rows for #111, #112 (found missing from the
  index despite the files existing — pre-existing drift, fixed as a byproduct), #114,
  #115, #116.

**Code (Phase 1.1 — blocked-rule terminal is `skip` when no real failure caused it):**
- `sam/lambdas/evaluate_deps/index.py` — `FAILURE_AVERSE_RULES` constant;
  `_check_trigger_rule` now returns `(satisfied, reason, effective_rule)` (was a
  2-tuple); `_handler` computes `verdict` (`ready|wait|skip|upstream_failed`) and adds
  it to the output contract.
- `sam/sfn_templates/helpers/notify_dependents/sfn.tpl.json` — `Route_Evaluation` gains
  a `verdict='skip'` branch (checked before the generic `is_blocked` branch); new
  `Signal_Skip` + `Update_Status_Skip` states (write `status=skipped`,
  `skip_origin=rule`, signal `deps_skip`).
- `sam/sfn_templates/dependency_wrapper/sfn.tpl.json` — `Check_Deps_Signal` gains a
  `deps_skip` branch; new `Emit_Deps_Skip` state records the event then **reuses** the
  existing `Auto_Skip_Notify_Dependents → Auto_Skip_Callback → Auto_Skip_Send_Success`
  chain (no duplicated logic) — ends in `Done`, not `Fail_State`, so this task's branch
  does not abort the `Run_All_Tasks` `Parallel`.
- `sam/lambdas/console_api/routes/tasks.py` — `_execute_task_action` gains an optional
  `skip_origin` parameter (additive to the `SET` clause only when provided); `skip_task`
  passes `skip_origin='manual'`. `fail_task`/`mark_success` unaffected (byte-identical
  `UpdateExpression` when `skip_origin` is not passed).

**Tests:**
- `sam/lambdas/evaluate_deps/test_evaluate_deps.py` — 37 call sites updated for the
  3-tuple return; new `TestVerdict` class, 20 tests covering all four verdicts across
  every rule category (failure-averse blocked-by-failure vs blocked-by-skip-only,
  never-failure-averse rules, `all_done`/`one_done` structurally-never-blocked, unknown
  rule fallback classification, the two originally-observed bug scenarios).
- `tests/backend/test_task_action_idempotency.py` — 3 new tests: `skip_task` writes
  `skip_origin='manual'`; `fail_task`/`mark_success` do not write it at all.
- Two independent mutation tests run and reverted (not left in the tree): (1) reverting
  the verdict branch to the old always-`upstream_failed` behavior — caught by 8 of the
  new `TestVerdict` tests; (2) checking the raw `trigger_rule` instead of
  `effective_rule` — caught by the unknown-rule test. (3) removing
  `skip_origin='manual'` from `skip_task` — caught by the new idempotency test.

**Docs (#9, scoped to what 1.1 actually changed — the full "11 rules → 3 canonical"
reframing is explicitly deferred to step 1.3, per the approved phased plan, so as not to
put docs ahead of code):**
- `docs/features/DSL.md` — note on the blocked-rule terminal split (skip vs
  upstream_failed) and the current structural limitation on failure-reactive rules.
- `docs/operations/TROUBLESHOOTING.md` — new entry: "Task shows 'skipped' with no
  failure anywhere in the pipeline."
- `docs/reference/adr-115-...md` — corrected in place after implementation: the
  original text claimed `deps_skip` would be added to the constants SSoT/codegen; it
  does not need to be (verified `deps_ready`/`deps_blocked` are pure SFN-side literals,
  absent from `polyris/constants.py`). ADR corrected to match what was actually built.

## 1. Consumers sweep

- `_check_trigger_rule`: only consumer besides its own test file is
  `tests/sdk/test_trigger_rules.py`, which does **not** import it — it hand-maintains
  its own `python_check_trigger_rule`/`jsonata_check_trigger_rule` reimplementations for
  parity checking (a **third** copy of the rule logic — flagged below, not touched).
  `grep -rn "_check_trigger_rule(" --include=*.py .` outside `evaluate_deps/` returns
  only that file. No signature-change fallout elsewhere.
- `is_blocked` / `eval_result` / `deps_satisfied`: consumed only by
  `notify_dependents/sfn.tpl.json`, `evaluate_deps/index.py`, and its test —
  confirmed by repo-wide grep, **zero EE consumers**.
- New `verdict` field: no exact-shape dict assertions anywhere in the test suite that
  would break from an added key (checked).
- `deps_ready`/`deps_blocked`/`deps_skip` signal literals: confirmed absent from
  `polyris/constants.py` and every `constants_generated.py` copy — pure SFN literals,
  no codegen consumer to sweep.
- `skip_origin`: written in exactly two places (`notify_dependents` rule-path =
  `'rule'`, `tasks.py` manual-path = `'manual'`), same attribute name in both,
  confirmed by grep. Zero read-consumers yet (expected — that's step 1.2).

## 2. Pattern sweep

Found: the trigger-rule satisfaction logic is duplicated in **three** places —
`evaluate_deps/index.py` (Python, the SSoT), `registration/sfn.tpl.json`'s inline
JSONata (`Eval_Task_Deps`, the immediate-registration fast path), and
`tests/sdk/test_trigger_rules.py`'s two hand-maintained Python reimplementations used
only to assert the first two stay in sync. **Not migrated in this delivery** — Phase
1.1 does not change rule-satisfaction semantics (only what happens in the
already-blocked case), so all three implementations remain correct and in sync as-is
(confirmed: `test_trigger_rules_sync` passes unchanged). The skip-cascade change (step
1.2, `all_success` requiring `success == total`) is exactly the point where this
duplication must be resolved with a real parity guard — flagged in ADR #115 and the
plan doc, intentionally deferred, not silently left for later.

## 3. Producer↔consumer contract

- `evaluate_deps` → `notify_dependents`: `verdict` field added to the Lambda's output;
  `Route_Evaluation`'s new Choice branch reads
  `$states.input.eval_result.verdict = 'skip'`. Verified by JSON structural validation
  (no dangling `Next` targets, full reachability from `StartAt` in both the top-level
  and the nested `ItemProcessor` state machine).
- `notify_dependents` → `dependency_wrapper`: the `deps_skip` string is identical on
  both sides (`Signal_Skip`'s `Output.signal` and `Check_Deps_Signal`'s new Choice
  condition) — verified by direct string comparison, not assumed.
- `tasks.py` (manual skip) ↔ `notify_dependents` (rule skip): both write to the same
  DDB attribute name (`skip_origin`) with the documented value contract (`'manual'` /
  `'rule'`) — verified by grep, no typo/casing drift.
- Regenerated nothing: this delivery introduced no new SSoT constant, so
  `polyris.codegen.sync_enums --check` has nothing new to drift on (verified no codegen
  copies reference `deps_skip`).

## 4. Dead-after-change

- `is_blocked` remains meaningful and used (still gates the Choice's generic branch,
  now checked after the more specific `verdict='skip'` branch) — not dead.
- No unreachable SFN states: verified programmatically (reachability walk from
  `StartAt`) on both edited templates before and after the edit.
- No unused imports or orphaned branches introduced in `index.py` or `tasks.py`
  (`py_compile` clean, full test suites green).

## 5. Full suite — all green, this session, in this order

- `sam/lambdas/evaluate_deps`: **77 passed** (57 pre-existing + 20 new `TestVerdict`).
- `sam/lambdas/console_api`: **408 passed** (unchanged count; re-run after the
  `tasks.py` edit).
- `sam/lambdas/notify`: **16 passed**. `check_assets`: **19 passed**.
  `notify_asset_subscribers`: **12 passed**.
- Top-level `tests/sdk + tests/backend + tests/integration + tests/e2e`: **1402
  passed**, 62 skipped, **100.0% coverage** on `polyris` (was 1399 before the 3 new
  `test_task_action_idempotency.py` tests).
- `tests/sdk/test_asl_snapshots.py` + `test_asl_snapshots_steps.py` (59 tests): green,
  unaffected (they compile `polyris/generators.py` output, which references the
  edited helper templates only by ARN, never inlines them).
- `tests/sdk/test_trigger_rules.py` (the pre-existing Python/JSONata parity guard):
  green, unaffected (rule-satisfaction logic unchanged in 1.1).
- CE UI: `tsc --noEmit` clean, `eslint src/` clean, `vitest run` **646 passed** (55
  files), `npm run build` clean (static export, 8 routes).
- `cfn-lint sam/template.yaml sam/bootstrap.yaml`: clean.
- **EE full suite via the Makefile method** (`make test-ee CE=<this checkout>`, run
  twice — once before, once after the `tasks.py` edit, both clean): backend 70 + 294 +
  26 = 390 passed; UI (`vitest run src/ee`) 379 passed (25 files); merged-tree
  `tsc --noEmit` clean.
- Two independent mutation tests per the changed logic, run and reverted (not left in
  the tree) — see Tests above.

## Not touched (deliberately, per the approved phased plan)

- Rule-satisfaction logic itself (`_check_trigger_rule`'s per-rule functions, the
  JSONata in `registration/sfn.tpl.json`) — unchanged; step 1.2.
- The `all_success` skip-cascade (`ok = success + skipped`) and the parity-guard
  requirement it creates — step 1.2.
- The 11→3 canonical rule trim, SDK validation, and the full doc/EDITIONS reframing —
  step 1.3.
- Cleanup de-abort (`all_done` on the failure path) — ADR #116, Phase 2, requires
  live-SFN validation not available in this sandbox.
- `docs/reference/AIRFLOW_MIGRATION.md`, `docs/architecture/ARCHITECTURE.md`,
  `README.md`, `EDITIONS.md` — scoped to step 1.3 (the "11 rules" framing change),
  not touched here to avoid docs running ahead of code.

## Not verifiable in this sandbox

- Live-SFN runtime behavior of the new `Signal_Skip`/`Emit_Deps_Skip` path (does the
  actual AWS Step Functions execution route and complete exactly as the JSON structure
  implies). Structural validation (JSON validity, dangling-target check, full
  reachability) was done programmatically; the SFN JSON was hand-authored to mirror
  the existing, already-deployed `Signal_Blocked`/`Emit_Deps_Blocked` pattern
  line-for-line in shape. A real-stack smoke test (deploy + run the
  `trigger-rules-reference` / `branching-demo` pipelines that originally showed the
  bug) is the recommended acceptance check before merging.
