# Completeness Report — Phase 1.3 (canonical/alias reframing)

Per CLAUDE.md Principle #23. Covers Phase 1.3 only — see
`COMPLETENESS_REPORT_phase_0_and_1_1.md` and `COMPLETENESS_REPORT_phase_1_2.md` for
the earlier phases. Delivered per `docs/reference/PLAN_intervention_and_trigger_rules.md`
(1.3 section, scope corrected during execution), ADR #115 decision 4.

## Scope correction (read this first)

The plan's original 1.3 bullet called for collapsing the 8 alias rules' implementations
and removing "~27 obsolete tests." Before writing any code, ADR #115 decision 4's
literal text was re-read: *"continue to validate and run"* — not "removed" or
"collapsed." Combined with what 1.1/1.2 already proved (the 11 rules are distinct,
correct, already verdict/cascade-aware implementations — not literal duplicates, no
Principle #1 violation among them), collapsing them would have been an unsupported,
risky logic change, and deleting passing tests for still-correct behavior would have
been actively wrong. **Delivered as a positioning/documentation change only.** No rule
removed, no test deleted, no `_check_trigger_rule` dispatch logic touched. The plan
document itself was corrected in place to record this (not silently deviated from).

## Changed

**Code:**
- `polyris/constants.py` — `TriggerRuleLiteral` and `TriggerRule` reordered (3
  canonical first: `all_success`, `one_success`, `all_done`) with `# Compat alias`
  comments on the other 8; `TriggerRule`'s docstring overclaim corrected to describe the intervention-first model and point to
  ADR #114/#115. All 11 values and attribute names unchanged.

**Codegen:**
- Regenerated all 4 copies (`sam/lambdas/_shared`, `console_api`, `evaluate_deps`
  `constants_generated.py`, `ui/src/generated/enums.ts`) — diff confirmed to be only
  reordering + a refreshed source-hash comment, no value/name changes.

**Docs (#9):**
- `docs/features/DSL.md` — removed the "Full parity... 11 trigger rules"
  banner; split the rule table into Canonical (3) and compat-alias (8)
  sections with a "behaves like, in practice" column; consolidated the incremental
  1.1/1.2 ADR notes (which explicitly said they'd be superseded here) into one
  coherent explanation; rewrote the Examples block — the old ones for `all_done`
  (cleanup), `one_failed` (alert), `all_failed` (fallback) were actively misleading
  given the intervention-model caveats now fully understood.
- `docs/reference/MIGRATION.md` — completed the trigger-rules table (was
  missing 4 of 11 — a pre-existing gap, not introduced by this phase), marked
  canonical/alias, consolidated the failure-pause caveat with the existing 1.2
  skip-cascade note.
- `docs/architecture/ARCHITECTURE.md` — fixed a **real drift Phase 1.1 had
  introduced and left unaddressed** (flagged then, closed now): the `all_success`
  table row still said "success/skipped (default)" — no longer true after the 1.2
  cascade; the Task Statuses table listed `deps_blocked`/`deps_ready` as if they were
  persisted statuses (they are transient SFN signal payloads — the persisted status
  is `upstream_failed`/the task literally running); `deps_skip` (new in 1.1) was
  missing entirely. Also fixed the Flow-4-adjacent sequence diagram, whose `evaluate_deps`
  output contract comment (`{is_ready, is_blocked, reason, dep_statuses}`) and
  is_blocked branching (single arrow) were stale versus the actual 1.1 output contract
  (`verdict` field) and its two-way split (`deps_skip` vs `deps_blocked`).
- `README.md` — replaced the "🎯 11 trigger rules" headline feature bullet (the
  clearest end-user-facing overclaim found) with an accurate one; consolidated with
  the adjacent "Skip/Restart tasks" bullet, which the new intervention-first bullet
  described more precisely, to avoid duplicate messaging in a short feature list.
- `docs/reference/adr-115-...md` — status line was already updated in the 1.2 report;
  no further change needed here (decision 4 is now the last one marked implemented).

## A citation mistake, and a mistaken "fix" of it — both caught eventually

While writing `docs/features/DSL.md`'s `one_failed` example, a citation to "ADR #103"
for the automatic Slack/PagerDuty pause-alert was written from memory. A shallow check
at the time — `grep`ing `docs/reference/adr-index.md` for `103` and `find`ing no
standalone `adr-103-*.md` file, both empty — looked like confirmation the ADR didn't
exist, so the citation was replaced with a generic pointer to
`docs/reference/alerting-master-plan.md` instead, and this report originally recorded
that as a caught error.

**That "fix" was itself wrong.** A later, more thorough re-verification pass (prompted
by an explicit request to re-check everything rigorously) searched more broadly —
`docs/reference/DESIGN_DECISIONS.md` and `docs/architecture/ARCHITECTURE.md` — and
found ADR #103 genuinely exists: it's documented **inline** (in
`DESIGN_DECISIONS.md`, not as a standalone file), the same pattern already known from
ADR #113 (and from the #111/#112 index gap found and fixed in the Phase 0/1.1 report).
`ARCHITECTURE.md` states the exact mechanism verbatim: *"PagerDuty fires in line 1
(immediate) so on-call can act during the decision-wait window ... ADR #103 1b."* This
is precisely the claim the original citation made. The citation has been restored in
both `DSL.md` and `MIGRATION.md`.

Two lessons, recorded rather than smoothed over: (1) a citation that fails a shallow
check ("not in the index, no standalone file") is not proven false — this codebase has
at least three ADRs (#103, #111, #112, #113) that are real but under-indexed, so
"absent from the index" and "absent from the codebase" are different claims; (2) a
self-correction needs the same rigor as the original claim — this was only caught by
deliberately re-verifying the delivered archive from scratch, not by re-reading the
same reasoning that produced the mistaken "fix." `adr-index.md` was missing #103 in
addition to the #111/#112 gap already fixed in the Phase 0/1.1 report — added here too
(inline, matching #96/#104/#105/#113's pattern) while the gap was in hand.

## 1. Consumers sweep

- Repo-wide grep for the fixed overclaim phrase across `.py`/`.md`/`.ts`/`.tsx`: only the one instance
  in `polyris/constants.py` existed; no duplicate elsewhere to miss.
- Repo-wide grep for `"11 trigger"` / `"full parity"` across
  `docs/` and `README.md`: found and addressed in `DSL.md`, `README.md`; the one
  remaining hit (`docs/tools/DEVELOPMENT.md`: *"11 trigger rules Python ↔ JSONata
  sync"*) is a factually accurate description of the parity test file's scope (it
  still tests exactly 11 rule names) — left unchanged, not a marketing claim.
- EE: `grep`ed `/home/claude/ee/polyris-ee/docs/` for the same phrases — no matches;
  EE's own docs never carried this claim, nothing to fix there. Full EE suite (backend
  + UI, via the Makefile method) re-run clean regardless, per the standing "full
  suite, never isolated" requirement.
- `TriggerRuleLiteral`/`TriggerRule` consumers (`polyris/task.py`, `polyris/dag.py`,
  the UI's `src/types/index.ts`, `TaskDetailModal/helpers.ts`,
  `DAGGraphFlow.tsx`): none iterate the enum in value-order (no dropdown/ordered
  listing found), so reordering the `Literal`/class body has zero behavioral or UX
  effect — confirmed by reading each consumer, not assumed.

## 2. Pattern sweep

No new duplication introduced. The one pre-existing duplication (Python/JSONata rule
logic) was already closed with a proven parity guard in Phase 1.2; this phase didn't
touch rule logic at all, only comments and prose.

## 3. Producer↔consumer contract

- `polyris/constants.py`'s `TriggerRuleLiteral` (SDK-facing type) and
  `sam/lambdas/evaluate_deps/index.py`'s `rules` dict (runtime dispatch) still cover
  the identical 11 rule names — confirmed no rule was added to one and not the other
  (a real risk this phase carried, since collapsing was explicitly *not* done, so all
  11 remain live on both sides).
- Codegen SSoT → 4 generated copies: verified in sync via `sync_enums --check` and
  `check_shared_constants`, not just regenerated and assumed correct.

## 4. Dead-after-change

- Nothing removed, so nothing to check for orphaned references from removal.
- The reordered `Literal`/class attributes are all still referenced identically by
  every consumer (order-independent usage confirmed in the consumers sweep above).

## 5. Full suite — all green, this session, in this order

- `sam/lambdas/evaluate_deps`: **89 passed** (unchanged from Phase 1.2 — no logic
  touched).
- `sam/lambdas/console_api`: **408 passed** (unchanged).
- Top-level `tests/sdk + tests/backend + tests/integration + tests/e2e`: **1404
  passed**, 62 skipped, **100.0% coverage** on `polyris` (unchanged from Phase 1.2 —
  confirms the comment/reorder-only change didn't silently affect anything).
- `python -m polyris.codegen.sync_enums --check`: clean.
- `python -m polyris.codegen.check_shared_constants`: clean.
- `python -m polyris.codegen.check_sfn_templates`: clean (12/14 canonical values
  referenced, same as before — unaffected by this phase).
- `cfn-lint sam/template.yaml sam/bootstrap.yaml`: clean.
- CE UI: `tsc --noEmit` clean (the reordered `enums.ts` union type-checks
  identically), `eslint src/` clean, `vitest run` **646 passed** (55 files),
  `npm run build` clean.
- **EE full suite via the Makefile method**, re-run clean after Phase 1.3: full
  backend suite green (unchanged counts from prior phases), UI **379 passed** (25
  files).

## Not touched (deliberately — this was the scope correction itself)

- `_check_trigger_rule`'s dispatch table / per-rule satisfaction functions — all 11
  remain, unchanged, exactly as verified correct in Phases 1.1/1.2.
- Any test file — nothing removed or retargeted; every existing trigger-rule test
  still tests real, current behavior.
- `polyris/task.py` / `polyris/dag.py` validation — no change needed; both already
  accept all 11 `TriggerRuleLiteral` values, which remains true.

## Still open (from the original release audit, unrelated to trigger rules)

- npm audit: 2 critical / 1 high — triage still pending, not part of this workstream.

## Not verifiable in this sandbox

- Nothing new for this phase — it was documentation and comment-only; no SFN/runtime
  surface was touched. The live-SFN verification recommended in the Phase 1.1 and 1.2
  reports (deploying and re-running `trigger-rules-reference`/`branching-demo`) still
  stands as the acceptance check before merging all three phases together.
