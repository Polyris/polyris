# Implementation Plan — Intervention-first model + canonical trigger rules

Everything below is scoped so each change lands **fully in code** per CLAUDE.md — ADR
(#6), docs in the same delivery (#9), no duplication (#1), 100% coverage (#22), and the
five-part blast-radius sweep + written completeness report (#23) across **CE and EE**.
Nothing is chat-only.

## Phase 0 — Decisions (BLOCKING, Principle #6)

Per #6, SFN-flow / API-contract / DDB / codegen changes need explicit approval **before**
code, then an ADR in `docs/reference/DESIGN_DECISIONS.md` (+ a numbered `adr-NNN-*.md`,
index updated). Last ADR is #112.

| ADR | decision | status |
|-----|----------|--------|
| **#114** | **Intervention-first failure model.** Task failure (retries exhausted) pauses in `waiting_decision` for a human decision; it does *not* autonomously propagate. This is the product identity; it is *why* failure-reactive trigger rules are constrained. | needs approval |
| **#115** | **Canonical trigger rules + skip semantics.** Canonical set = `all_success`, `one_success`, `all_done`. `skipped` is a distinct terminal that **cascades** for `all_success` (no longer counted as OK in trigger evaluation) but stays *resolved* for run-status derivation. Remaining names (`none_failed`, `none_failed_min_one_success`, `none_skipped`, `all_skipped`, `one_failed`, `all_failed`, `all_done_min_one_success`, `one_done`) are accepted as **compat aliases mapping to canonical behavior**, documented as non-distinct — not advertised as separate. | needs approval |
| **#116** | **Cleanup via scoped de-abort** (`all_done` runs after a resolved failure). Deferred to Phase 2 (needs live-SFN validation). | draft later |

**Also decide (drives #115):** manual skip vs conditional skip. Manual skip cascading can
"un-materialize" a downstream asset chain (verified: a skipped task does *not* reach
`Emit_Asset_Events`, so it materializes nothing). Options: (a) both cascade (simplest);
(b) conditional-skip cascades, manual-skip does not (safer UX, one extra
status distinction). Pick one in #115.

→ **No code starts until #114 + #115 are approved.**

## Phase 1 — Release-blocking, deterministic (I can complete + gate fully here)

Ordering is mandatory: 1.1 → 1.2 → 1.3 (cascade depends on skip semantics; trim depends on
both). Each step ships with both archives + a #23 completeness report.

### 1.1 — Skip-fix: blocked no-op → `skipped` (ADR #115)

The observed bug: a rule that legitimately does not fire is written `upstream_failed`
(red, run `aborted`) instead of `skipped`.

- **Code (CE):**
  - `sam/lambdas/evaluate_deps/index.py` — add a `verdict` (`ready | wait | skip |
    upstream_failed`) to the handler return; `skip` when blocked and the block is not a real
    failure, `upstream_failed` when a failure-averse rule is blocked by a failure.
  - `sam/sfn_templates/helpers/notify_dependents/sfn.tpl.json` — on `verdict = skip`, send a
    new `deps_skip` signal instead of `deps_blocked`.
  - `sam/sfn_templates/helpers/registration/sfn.tpl.json` — same for the immediate-eval path.
  - `sam/sfn_templates/dependency_wrapper/sfn.tpl.json` — handle `deps_skip` → route into the
    existing `Auto_Skip_*` chain (writes `skipped`, notifies dependents, `SendTaskSuccess`).
  - Signal name `deps_skip` added to the constants SSoT (`polyris/constants.py`), codegen
    regenerated (all generated copies + `ui/src/generated/enums.ts`).
- **Tests:** verdict matrix in `test_evaluate_deps.py`; mutation on each verdict branch;
  regenerate + review the SFN JSON snapshots in `tests/snapshots/`; **e2e** (#16): a pipeline
  where a conditional rule does not fire → asserts the task is `skipped` and run is `success`.
- **Docs (#9):** `docs/features/DSL.md` (rule behavior), `docs/operations/TROUBLESHOOTING.md`
  (skipped vs failed).
- **#23 sweep:** consumers of the new `verdict` field and `deps_skip` signal (CE + EE);
  consumers of status `skipped`; every snapshot test; run the EE suite (Makefile method).

### 1.2 — Skip-cascade: `all_success` requires `success == total` (ADR #115)

- **Code (CE):**
  - `sam/lambdas/evaluate_deps/index.py` — `all_success` becomes `pending == 0 and success ==
    total` (drop `ok = success + skipped` for this rule only; `ok` is used *only* by
    `all_success`, verified — surgical).
  - `sam/sfn_templates/helpers/registration/sfn.tpl.json:202` — the **duplicated** JSONata
    `$ok := $c.success + $c.skipped` / `all_success` branch must change identically.
  - **Kill the duplication (#1).** The rule logic exists twice (Python + a 1326-char inline
    JSONata). Add a **parity guard test** that evaluates both implementations over the full
    status-combo matrix and asserts they agree (AST/eval-based, in the spirit of the existing
    guard tests), *or* generate the JSONata from the Python SSoT. Parity test is the minimum;
    without it the two drift silently.
- **Tests:** `all_success` with a skipped upstream → not satisfied → skip-cascade; the
  `trigger-rules-reference` acceptance case (both extractors success → no-op rules `skipped`,
  run `success`); mutation; the parity guard.
- **Docs (#9):** `docs/features/DSL.md` (skip now cascades; `none_failed` to opt out),
  `docs/reference/MIGRATION.md`.
- **#23 sweep:** consumers of `TASK_SUCCESS_STATUSES` — **confirm `derive_execution_status` is
  unaffected** (skipped must stay `_DERIVE_RESOLVED` for run-status); the `ok` logic; EE.
  **EE asset ripple:** cascade → more un-materialized assets → update EE asset docs; note the
  optional EE asset-matrix "skipped" cell as a separate follow-up (not this change).

### 1.3 — Rule trim: 11 → 3 canonical + aliases (ADR #115) — DELIVERED, scope corrected

**Correction made during execution.** This plan originally called for collapsing the 8
alias rules' implementations and removing "~27 obsolete tests." Re-reading ADR #115
decision 4's literal text before touching code — "continue to validate and run" — showed
that was an overreach relative to what was actually decided: 1.1/1.2 already proved the
11 rules are NOT literal code duplicates (each has its own distinct, correct,
verdict/cascade-aware implementation; no Principle #1 violation among them). Collapsing
them would have been a real, risky logic change unsupported by the ADR text, and
removing passing tests for still-existing, still-correct behavior would have been
actively wrong. Delivered as a **positioning/documentation change only** — no rule
removed, no test deleted, no `_check_trigger_rule` dispatch logic touched.

- **Code:** `polyris/constants.py` — `TriggerRuleLiteral` and `TriggerRule` reordered
  (3 canonical first) and commented (`# Compat alias`); the `TriggerRule` docstring's
  overclaim corrected. All 11 values/names unchanged —
  codegen reads `vars()`/the `Literal` members, not comments, confirmed before editing.
  Codegen regenerated (order changed, values didn't) and `--check` + `check_shared_constants`
  both pass.
- **Tests:** none removed — all still test real, unchanged behavior. No SDK validation
  change needed (`polyris/task.py`/`polyris/dag.py` already accept all 11).
- **Docs (#9):** `docs/features/DSL.md` (banner + table split into Canonical/Alias +
  rewritten Examples — the old ones for `all_done`/`one_failed`/`all_failed` were
  actively misleading given the intervention-model caveats), `docs/reference/
  MIGRATION.md` (table completed to all 11 — was missing 4; canonical/alias
  marked), `docs/architecture/ARCHITECTURE.md` (fixed a real drift the 1.1 change had
  introduced: `all_success`'s row, and a status table listing the transient `deps_blocked`/
  `deps_ready` signals as if persisted — added `deps_skip`), `README.md` (replaced the
  "11 trigger rules" headline bullet). `EDITIONS.md` does not exist in this repo — the
  original plan's reference to it was incorrect; no such file to update.
- **#23 sweep:** confirmed no other file duplicates the fixed docstring phrase; confirmed
  EE has no matching overclaim in its own docs; full backend/lambda suites, codegen
  checks, cfn-lint, CE UI (tsc/eslint/vitest/build), and the EE Makefile suite all re-run
  clean after the change.

### Gate for every Phase-1 delivery
Backend `pytest` 100% (`make test-cov`) + codegen `--check`; CE `tsc` + `eslint` + `vitest` +
`npm run build` + `check-oss-build.sh`; **EE suite via the Makefile method** (running it any
other way hides failures); `cfn-lint`. Then the #23 completeness report. Both archives.

## Phase 2 — Post-release (needs live SFN; higher risk)

### 2.1 — Cleanup de-abort (ADR #116)
- `failure_handler` — on a *resolved* failure for the cleanup path, `SendTaskSuccess` instead
  of `SendTaskFailure`; repoint the `Run_All_Tasks` `Parallel` `Catch` so a logical failure no
  longer aborts siblings.
- Run-status stays correct (already DDB-derived, ADR #112 — verified). Confirm
  `reconcile_execution_status` does not mis-mark a de-aborted run.
- **Tests:** unit + **live-SFN e2e — Mike's part** (abort/race behavior is invisible to unit
  tests). Until done, `all_done` is documented as "cleanup on the success path; experimental
  on failure".

### 2.2 — De-dup rule logic (if not done in 1.2)
Generate the JSONata from the Python SSoT, or keep the parity gate. Housekeeping, non-blocking.

## Still open from the release audit (unrelated to trigger rules)
- npm audit: 2 critical / 1 high — triage.
- (docs tier-drift patch + `pipelines/` removal + `examples_temp` restore — already delivered.)

## Definition of done (per change)
ADR written & approved · code (CE + EE) · docs updated same delivery · unit + mutation +
(e2e where a contract/endpoint changes) · full suite CE + EE green · #23 completeness report ·
both archives. No report ⇒ not done.

## What I cannot do
Live-SFN validation (2.1, and the runtime confirmation that the skip-cascade propagates
correctly through the notify/token dance beyond unit level). The simulator proves the logic;
the SFN runtime is yours to smoke on a real stack.
