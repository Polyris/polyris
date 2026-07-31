# Completeness Report — Phase 1.2 (skip-cascade)

Per CLAUDE.md Principle #23. Covers Phase 1.2 only — see
`COMPLETENESS_REPORT_phase_0_and_1_1.md` for Phase 0 + 1.1. Delivered per
`docs/reference/PLAN_intervention_and_trigger_rules.md`, ADR #115 decisions 2 and 5.

## Changed

**Code:**
- `sam/lambdas/evaluate_deps/index.py` — `_check_trigger_rule` gains an optional
  `rule_originated_skip: Optional[List[bool]] = None` parameter; `all_success`'s `ok`
  computation now discounts rule-originated skips (`ok = success + (skipped -
  cascading_skips)`); its blocked-reason message distinguishes a failure-caused block
  from a cascade-caused block. `_handler` fetches origin info only when a dependency is
  actually `skipped` (the common case pays nothing extra) via a new helper,
  `_batch_get_rule_originated_skip`.
- `sam/lambdas/evaluate_deps/dal/__init__.py` — new `TokensRepo.batch_get_skip_origins`
  + `_absorb_skip_origin_response`: a separate, sparse, best-effort (never raises) DDB
  fetch. Deliberately **not** merged into `batch_get_statuses`'s
  `ProjectionExpression`, to leave that method's contract and its 7 existing test
  mocks untouched.
- `sam/sfn_templates/helpers/registration/sfn.tpl.json` — `Get_Task_Dep_Status`'s
  `Output` now extracts `skip_origin` (already present in the unrestricted `getItem`
  response, just not previously projected out); `Eval_Task_Deps`'s JSONata computes
  `$cascading_skips` and an origin-aware `$ok`, mirroring the Python change exactly.

**Tests:**
- `sam/lambdas/evaluate_deps/test_evaluate_deps.py` — 9 new tests: 5 unit-level on
  `_check_trigger_rule`'s `rule_originated_skip` parameter (cascade blocks, explicit
  non-cascade, mixed rule+manual, confirmation no other rule is affected); 3
  handler-level in `TestVerdict` (unknown-origin stays ok, explicit `manual` stays ok,
  explicit `rule` cascades, all-rule-skipped cascades); 5 in a new
  `TestAbsorbSkipOriginResponse` class (pure, dependency-free tests of the sparse
  DDB-response parser — present/absent/missing-name/empty-response/mixed cases). One
  pre-existing 1.1 test (`test_all_success_blocked_by_skip_only_is_still_ok_in_1_1`)
  was renamed and its docstring corrected: it turned out to describe a *permanent*
  behavior (unknown-origin skip never cascades), not a placeholder that 1.2 would
  flip — the actual flip only happens for an explicitly `rule`-tagged skip, which is
  now covered by the new tests alongside it.
- `tests/sdk/test_trigger_rules.py` — both `python_check_trigger_rule` and
  `jsonata_check_trigger_rule` gained a `skip_origins` parameter (default `None`,
  preserving every existing call site); two new tests:
  `test_skip_origin_cascade_sync` (exhaustive: every status combination containing a
  `skipped` entry × every origin combination from `['rule', 'manual', None, '']`,
  14,164 combinations for `all_success`) and `test_skip_origin_cascade_edge_cases` (8
  targeted cases asserted against the expected value, not just cross-checked).

**Docs (#9):**
- `docs/features/DSL.md` — new paragraph on the skip-cascade, appended to the existing
  1.1 note.
- `docs/reference/AIRFLOW_MIGRATION.md` — new note under the Trigger Rules table.
- `docs/operations/TROUBLESHOOTING.md` — new entry: "An `all_success` task shows
  'skipped' even though its upstream ran fine."
- `docs/reference/adr-115-...md` — status line updated (decisions 1–3, 5 implemented;
  decision 4 pending); decision 5's schema-change note corrected to describe the
  actual mechanism (separate DAL fetch, not a shared projection); the de-duplication
  consequence marked done, naming the actual guard tests and the mutation proof.

## 1. Consumers sweep

- `_check_trigger_rule`'s signature changed again (third parameter added). Repo-wide
  grep confirms the only call sites are `evaluate_deps/index.py` itself and
  `evaluate_deps/test_evaluate_deps.py` (already updated) — no other file calls it.
  Default `None` means every pre-1.2 caller (all 89 existing tests using two positional
  args) is unaffected.
- `batch_get_statuses`'s contract and its 7 existing test mocks: untouched — confirmed
  by re-running the full `test_evaluate_deps.py` and `tests/backend/` suites
  unchanged.
- `skip_origin` (written in 1.1 by `notify_dependents` and `tasks.py`) now has its
  first real consumers: `evaluate_deps/index.py`'s new helper and
  `registration/sfn.tpl.json`'s `Get_Task_Dep_Status`. Attribute name and value
  contract (`'rule'` / `'manual'` / absent) verified consistent across all four sites
  (2 writers from 1.1, 2 readers from 1.2) by direct grep, not assumed.
- EE: repo-wide grep for `rule_originated_skip`, `batch_get_skip_origins`,
  `cascading_skips` in `/home/claude/ee/polyris-ee` returns nothing — zero EE
  consumers to sweep, and the full EE suite (backend + UI) was re-run green after
  these changes regardless.

## 2. Pattern sweep

The duplication flagged as deferred in the Phase 0/1.1 report is now closed: the
`all_success` cascade logic is implemented identically in Python and JSONata, and the
parity guard (`test_skip_origin_cascade_sync`) is the mechanism that keeps them in
sync — not just written, but proven to actually catch drift (see mutation below). The
pre-existing third copy (`tests/sdk/test_trigger_rules.py`'s own
`python_check_trigger_rule`/`jsonata_check_trigger_rule` re-implementations, used only
for this parity assertion) was extended in parallel with the real implementations,
which is an inherent property of a hand-maintained parity test — it is the accepted
guard mechanism per ADR #115, not a fourth untracked duplication.

## 3. Producer↔consumer contract

- `skip_origin` values: writers (1.1) write exactly `'rule'` or `'manual'`; readers
  (1.2) check `== 'rule'` (cascades) and treat everything else — `'manual'`, absent,
  `None`, `''` — as non-cascading. Verified both in the DAL's sparse-inclusion test
  (`TestAbsorbSkipOriginResponse`) and in the cascade edge-case tests
  (`test_all_success_manual_skip_does_not_cascade`,
  `test_all_success_unknown_origin_skip_is_still_ok`).
- Python `_check_trigger_rule` ↔ JSONata `Eval_Task_Deps`: identical formula
  (`ok = success + (skipped - cascading_skips)`), proven equivalent over 14,164
  combinations, not just eyeballed.
- `derive_execution_status` (ADR #112): unaffected by design — `skipped` remains in
  `_DERIVE_RESOLVED` regardless of *why* a task resolved skipped, so a cascaded skip
  still yields a `success` run when nothing genuinely failed. Not re-derived or
  special-cased for the cascade; confirmed by the full top-level suite staying at
  100% coverage with no changes needed in `constants_generated.py`.

## 4. Dead-after-change

- `rule_originated_skip=None` default path is exercised by all 89 pre-1.2 tests — not
  dead.
- `_batch_get_rule_originated_skip`'s early-return (`if not skipped_exec_names: return
  [False] * len(dependencies)`) is exercised whenever no dependency is skipped — not
  dead, covered implicitly by every non-skip test in the suite.
- No orphaned states in the registration SFN template: reachability re-verified after
  the `Get_Task_Dep_Status`/`Eval_Task_Deps` edits (5 states in the branch, 0
  unreachable, 0 dangling `Next` targets).

## 5. Full suite — all green, this session, in this order

- `sam/lambdas/evaluate_deps`: **89 passed** (80 from Phase 1.1 + 9 new).
- `sam/lambdas/console_api`: **408 passed** (unchanged).
- `sam/lambdas/notify` **16**, `check_assets` **19**, `notify_asset_subscribers`
  **12** — all unchanged.
- `tests/sdk/test_trigger_rules.py`: **4 passed** (2 pre-existing + 2 new), covering
  9,009 + 14,164 = 23,173 status/origin combinations across both parity tests.
- Top-level `tests/sdk + tests/backend + tests/integration + tests/e2e`: **1404
  passed**, 62 skipped, **100.0% coverage** on `polyris` (was 1402 before the 2 new
  parity-test functions).
- `cfn-lint sam/template.yaml sam/bootstrap.yaml`: clean.
- CE UI: `tsc --noEmit` clean, `eslint src/` clean, `vitest run` **646 passed**,
  `npm run build` clean.
- **EE full suite via the Makefile method**, re-run clean after Phase 1.2: backend +
  UI, **379 UI tests passed** (25 files), full backend suite green (unchanged counts
  from Phase 1.1's run).
- **Mutation tests, run and reverted (not left in the tree):**
  1. Reverted `all_success`'s `ok` formula to the pre-cascade
     `success + skipped` in the real `evaluate_deps/index.py` — caught by 4 tests
     (`test_all_success_rule_originated_skip_blocks`,
     `test_all_success_mixed_rule_and_manual_skip`,
     `test_all_success_rule_originated_skip_cascades`,
     `test_all_success_all_rule_skipped_cascades_to_skip`).
  2. Broke `_batch_get_rule_originated_skip` to always return `False` (ignoring
     fetched origins) in the real `evaluate_deps/index.py` — caught by 2
     `TestVerdict` tests.
  3. Mutated the **test file's own JSONata-mirror function**
     (`jsonata_check_trigger_rule`, dropping the cascade term) to prove the parity
     guard actually detects a real divergence between the two runtimes, not just
     passes by construction — caught 185 mismatches across
     `test_skip_origin_cascade_sync` and `test_skip_origin_cascade_edge_cases`. This
     is the direct verification that the de-duplication guard required by ADR #115
     works, not just exists.

## Not touched (deliberately, per the approved phased plan)

- The 11→3 canonical rule trim, SDK validation (`polyris/task.py`/`polyris/dag.py`),
  and the full "11 rules" → "3 canonical + aliases" doc/EDITIONS reframing — step 1.3.
- Cleanup de-abort (`all_done` on the failure path) — ADR #116, Phase 2.
- The pre-execution `skip_tasks` list mechanism (`Auto_Skip_Register` in
  `dependency_wrapper/sfn.tpl.json`, used by backfill to skip already-completed
  dates) — deliberately **not** given a `skip_origin` value. It keeps writing only
  `skip_reason='auto_skipped'` (a pre-existing, different field). Absence of
  `skip_origin` on those rows means they are treated as non-cascading (the safe
  default), which preserves today's backfill behavior without deciding — because it
  wasn't asked and touching backfill semantics without a specific request would be
  scope creep beyond the trigger-rule bug this plan addresses. Flagged here as a
  considered decision, not a gap discovered later.

## Not verifiable in this sandbox

- Live-SFN runtime confirmation that `Get_Task_Dep_Status`'s extended `Output` and
  `Eval_Task_Deps`'s extended JSONata actually evaluate as intended inside a real Step
  Functions execution (structural JSON validity and reachability were verified
  programmatically; the JSONata expression syntax itself was hand-extended following
  the exact style of the surrounding, already-deployed expression). As with Phase
  1.1's `Signal_Skip`/`Emit_Deps_Skip` path, a real-stack smoke test — deploying and
  re-running the pipelines that originally showed the bug — is the recommended
  acceptance check before merging both phases together.
