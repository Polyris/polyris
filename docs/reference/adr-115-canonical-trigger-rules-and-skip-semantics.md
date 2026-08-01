# ADR #115 — Canonical trigger rules and skip semantics

> **Status:** All decisions implemented. Decisions 1–3 and 5 (skip-fix, skip-cascade,
> skip-origin) shipped in steps 1.1/1.2. Decision 4 (canonical vs. alias framing)
> shipped in step 1.3 — **scope corrected during execution**: delivered as a
> positioning/documentation change only, not the rule-collapse/test-removal
> originally sketched below (see the corrected Consequences section and
> `docs/reference/COMPLETENESS_REPORT_phase_1_3.md` for why). Narrows the *advertised*
> `trigger_rule` vocabulary to what the engine can actually execute under ADR #114's
> intervention-first failure model, fixes the blocked-rule terminal status, and makes
> `skipped` a real, distinct status instead of a synonym for `success` in rule evaluation.

## Context

polyris's SDK exposes 11 `trigger_rule`s. Investigating two observed runs
(`branching-demo`, `trigger-rules-reference`) where a rule that should not have fired
showed up **red** with the run marked `aborted` — with no task actually failing — led to a
full audit (`docs/reference/SPIKE_TRIGGER_RULES.md`). Three independent defects were
found:

1. **Blocked terminal is always `upstream_failed`.** `evaluate_deps` (`sam/lambdas/
   evaluate_deps/index.py`) treats *any* blocked rule (all deps terminal, rule not
   satisfied) as `upstream_failed`. That is correct when the block was caused by a real
   upstream failure on a rule that requires success (`all_success`) — but wrong when the
   rule's trigger condition simply never occurred (`one_failed` with no failures: there is
   nothing to react to, and marking it `upstream_failed` is a false alarm that also flips
   the whole run to `aborted`).

2. **`skipped` counts as `ok` for `all_success` only.** `ok = success + skipped` (used
   *only* by the `all_success` branch, per line-level check) means a skipped upstream does
   not block `all_success` — collapsing it onto `none_failed`'s behavior. This is a
   "keep the pipeline moving" shortcut, not intentional skip
   semantics; it removes the one distinction the two rules have.

3. **Failure-reactive rules are structurally unreachable** under ADR #114 (`one_failed`,
   `all_failed`, `all_done`, `all_done_min_one_success` all require a genuine terminal
   failure to reach a sibling branch; the `Parallel`'s abort-on-failure cancels siblings
   first).

A truth-table simulator, cross-validated against the two observed runs (both reproduced
exactly) and driven through the real `derive_execution_status`, shows that on the *only*
states reachable under ADR #114 (`{success, skipped}` — a real failure never reaches a
sibling), the 11 named rules collapse to **3 distinct behaviors**:
`all_success` ≡ `none_skipped`; `one_success` ≡ `none_failed_min_one_success`;
`none_failed` is vacuous (its only distinguishing condition — blocking on failure — never
occurs, once failure can't reach it); `all_skipped` is a narrow but genuine fourth case.
Full derivation in `docs/reference/SPIKE_TRIGGER_RULES.md`.

## Decision

1. **Canonical rule set: `all_success` (default), `one_success`, `all_done`.**
   `all_done` is the exception carved out by ADR #116 (cleanup/teardown) — until that
   ships, it is documented as "runs on the success path only."

2. **`skipped` becomes a real, distinct terminal in rule evaluation — no longer
   `ok` for `all_success`.** `all_success` now requires `pending == 0 and success ==
   total`. `run_status` derivation is untouched: `skipped` stays in `_DERIVE_RESOLVED`
   (ADR #112) so a skip does not fail the run.

3. **Blocked-rule terminal is split by cause.** `evaluate_deps` returns an explicit
   `verdict`: `ready | wait | skip | upstream_failed`. `upstream_failed` only when a
   rule that requires success (`all_success`, and legacy aliases that behave the same)
   is blocked by an actual failure; `skip` otherwise (the condition simply didn't occur —
   not an error). The poller (`notify_dependents`) and the immediate-eval path
   (`registration`) send a new `deps_skip` signal on `verdict = skip`; the wrapper routes
   it into the existing `Auto_Skip_*` chain (writes `skipped`, notifies dependents,
   `SendTaskSuccess`) instead of `Handle_Failure`.

4. **Legacy rule names remain accepted, mapped to canonical behavior, not
   advertised as distinct.** `none_failed`, `none_failed_min_one_success`,
   `none_skipped`, `all_skipped`, `one_failed`, `all_failed`,
   `all_done_min_one_success`, `one_done` continue to validate and run — each maps onto
   one of the canonical behaviors (documented mapping table in `docs/features/DSL.md`) —
   for migration compatibility. Docs stop presenting them as 11 independent
   behaviors.

5. **Skip has an origin, and only rule-originated skip cascades.** A downstream
   `all_success` task should not silently no-op an entire success chain (and, in the EE
   asset console, an entire downstream asset-materialization chain — verified: the
   `Auto_Skip_*` path never reaches `Emit_Asset_Events`) just because an operator
   manually skipped one upstream task to unblock a pause. Manual skip is a human decision
   to tolerate one specific gap, not a signal that everything downstream should also be
   skipped. Therefore:
   - `skip_task` (manual, `sam/lambdas/console_api/routes/tasks.py`) writes
     `skip_origin = 'manual'`.
   - The wrapper's `Auto_Skip_*` path (rule-triggered, decision 3 above) writes
     `skip_origin = 'rule'`.
   - `all_success`'s blocked-check treats `skip_origin = 'rule'` as blocking (cascades)
     and `skip_origin = 'manual'` as `ok` (does not cascade — same behavior a manual
     skip has today, preserved intentionally).
   - **Schema change (flagged per Principle #6):** `skip_origin` is a new optional
     string attribute on the task/token DDB item. No index changes; no migration needed
     (absence is treated as `'manual'` for pre-existing rows, matching current
     behavior). **Implemented as a separate, lightweight fetch**
     (`TokensRepo.batch_get_skip_origins`), not merged into the existing
     `batch_get_statuses`' `ProjectionExpression` — this keeps that method's contract
     and its existing test mocks untouched, at the cost of a second (small, sparse,
     best-effort, non-raising) DDB round-trip, and only when a dependency is actually
     `skipped` (the common case pays nothing extra). The registration fast-path
     (`Eval_Task_Deps`'s JSONata) gets `skip_origin` for free in the same
     `dynamodb:getItem` call it already makes per dependency (no projection was
     restricting that call), so no second round-trip is needed there.

## Consequences

- **De-duplication (Principle #1) — done.** Rule logic exists twice: Python
  (`evaluate_deps/index.py`) and inline JSONata (`sam/sfn_templates/helpers/
  registration/sfn.tpl.json`, the `$ok := $c.success + $c.skipped` / rule-dispatch
  block, now `$cascading_skips`/origin-aware `$ok`). Both changed identically for
  decisions 2–3, 5. `tests/sdk/test_trigger_rules.py`'s pre-existing parity guard
  (`test_trigger_rules_sync`) was extended with `test_skip_origin_cascade_sync`
  (14,164 status×origin combinations for `all_success`) and
  `test_skip_origin_cascade_edge_cases` — the guard against the two runtimes drifting
  apart. Verified by deliberately mutating the JSONata-mirror function in the test file
  (dropping the cascade term) and confirming both new tests fail (185 mismatches),
  then reverting — the guard actually catches drift, not just passes by construction.
- **Codegen.** Corrected during implementation: `deps_ready`/`deps_blocked` (and now
  `deps_skip`) are pure SFN-side signal literals — they are not part of the `TaskStatus`
  SSoT and have no codegen copies (verified: absent from `polyris/constants.py`). No
  codegen change was needed for the signal name itself. The *resulting* DDB status
  (`skipped`) already exists in `TaskStatus` and required no schema addition. The
  `skip_origin` attribute (decision 5) is the only new DDB field, and it is
  free-text-optional, not enum-generated.
- **Tests — plan corrected during execution.** This originally said "~27 now-redundant
  per-rule tests are removed, replaced by a canonical-behavior + alias-mapping matrix."
  That was written before steps 1.1/1.2 proved the 11 rules are genuinely distinct,
  correct implementations (no Principle #1 duplication among them — only the
  Python/JSONata *pair* was duplicated, and that's the parity guard above, not this).
  Step 1.3 shipped as decision 4's literal text specifies — "continue to validate and
  run" — with **zero tests removed**: every existing per-rule test still tests real,
  unchanged behavior. See `docs/reference/COMPLETENESS_REPORT_phase_1_3.md`.
- **Docs.** `docs/features/DSL.md` (canonical/alias table + skip-origin behavior),
  `docs/reference/MIGRATION.md`, `docs/operations/TROUBLESHOOTING.md`,
  `docs/architecture/ARCHITECTURE.md`, `docs/architecture/BACKEND.md`, `README.md` —
  repositioned from "11 trigger rules" to "intervention-first orchestration; 3
  canonical rules, legacy names accepted as aliases." (`EDITIONS.md` does not exist in
  this repo — an earlier draft of this ADR referenced it in error.)
- **EE.** Rule-originated skip-cascade can leave a downstream asset chain
  un-materialized; manual skip does not. EE asset docs get a note; the asset matrix's
  visual treatment of `skipped` vs. `missing` is a separate, non-blocking EE follow-up.

## Supersedes

None directly — no prior ADR defined the 11-rule vocabulary or its skip semantics; this
ADR is the first explicit decision on the topic. Flagged as a pre-existing documentation
gap, not a formal supersession.
