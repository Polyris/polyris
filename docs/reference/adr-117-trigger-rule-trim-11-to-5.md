# ADR #117 — Trigger rules trimmed from 11 to 5

> **Status:** ACCEPTED AND IMPLEMENTED.

## Context

ADR #115 kept all 11 rule names accepted (3 "canonical" + 8 "compat aliases,
mapped to canonical behavior, not advertised as distinct") for migration
compatibility. That framing was revisited: an alias a user can still write, that
silently behaves like a *different* rule they didn't ask for (or never fires at all),
is not a compatibility feature — it's a trap. A rule named `one_failed` that can never
be satisfied is worse than not offering it, because the name itself teaches the wrong
mental model (autonomous-propagation semantics) right up until the moment a
real failure occurs in production and it silently never fires.

Re-deriving the reachable-state analysis from ADR #115/`SPIKE_TRIGGER_RULES.md`, but
against the **specific, common case an operator actually produces**: a paused task
(ADR #114) resolved via the UI's **manual** Skip or Mark-Success buttons (not a
rule-originated cascade). This is the dominant real-world path — a human clears a
pause and the pipeline continues — as opposed to a rule-originated skip cascade, which
ADR #115's original derivation weighted differently. Running `_check_trigger_rule`
(the real Python implementation, not a re-derivation on paper) against every rule for
all three reachable 2-dependency combinations in this scenario —
`(success, success)`, `(success, skip-manual)`, `(skip-manual, skip-manual)` — confirms
a resolved failure via Fail still unconditionally cancels the whole pipeline's
`Parallel` (`Catch: States.ALL`) before any downstream `trigger_rule` evaluates, exactly
as ADR #115/#116 already established; so those three combinations are the *entire*
reachable space for a rule evaluation to ever run against real dependency statuses.

Results (`✅`=fires, `⛔`=doesn't), verified against the real code:

| Rule | success+success | success+skip | skip+skip |
|---|---|---|---|
| `all_success` | ✅ | ✅ | ✅ |
| `all_done` | ✅ | ✅ | ✅ |
| `one_done` | ✅ | ✅ | ✅ |
| `none_failed` | ✅ | ✅ | ✅ |
| `one_success` | ✅ | ✅ | ⛔ |
| `none_failed_min_one_success` | ✅ | ✅ | ⛔ |
| `all_done_min_one_success` | ✅ | ✅ | ⛔ |
| `all_skipped` | ⛔ | ⛔ | ✅ |
| `none_skipped` | ✅ | ⛔ | ⛔ |
| `all_failed` | ⛔ | ⛔ | ⛔ |
| `one_failed` | ⛔ | ⛔ | ⛔ |

Four groups emerge:
- **Group A** (`all_success`, `all_done`, `one_done`, `none_failed`) — identical in
  every reachable state.
- **Group B** (`one_success`, `none_failed_min_one_success`, `all_done_min_one_success`)
  — identical in every reachable state.
- **Never satisfiable** (`all_failed`, `one_failed`) — their only intended use case is
  reacting to a confirmed failure, which is exactly the state the `Parallel`'s
  `Catch: States.ALL` prevents them from ever reaching. Not "unreliable" — structurally
  impossible until a de-abort mechanism (ADR #116, deferred, unimplemented) ships.
- **Genuinely distinct**: `all_skipped`, `none_skipped`.

(Note this differs from `SPIKE_TRIGGER_RULES.md`'s original grouping, which analyzed a
rule-*originated*, cascading skip specifically — a narrower, rule-triggered scenario,
not the common manual-Skip-button case this ADR is scoped to. Both derivations are
correct for the scenario each one covers; this ADR is about the day-to-day reachable
state space for a human operator, which is dominated by manual resolution.)

## Decision

Trim the accepted `trigger_rule` vocabulary from 11 to 5: `all_success` (default),
`one_success`, `all_done`, `all_skipped`, `none_skipped`. Pick one representative name
from each duplicate group (`all_done` for Group A, `one_success` for Group B) and
**remove**, not just deprecate, the other 9 names (6 duplicates + 2 never-satisfiable).

A task built with one of the 6 removed names now fails `validate_asl_from_dag` with a
specific message: which kept rule it was identical to (for the 4 duplicates), or that
it can never be satisfied (for `all_failed`/`one_failed`) — not evaluate_deps' generic
"unknown rule, defaulting to all_success" fallback, which exists for a genuinely
unrecognized string, not a deliberately-removed one the user should be told about.

`all_done` is kept even though it duplicates Group A today, per ADR #116's standing
carve-out: it is the one rule with independent product value (unconditional
cleanup/teardown) once a de-abort mechanism ships, at which point it will stop
duplicating the others on the failure path specifically.

## Consequences

- **Code (CE):** `polyris/constants.py` (`TriggerRuleLiteral`, `TriggerRule` — the SSoT),
  `polyris/validation.py` (new `trigger_rule` check in `validate_asl_from_dag`, with
  per-removed-rule suggestions), `sam/lambdas/evaluate_deps/index.py`
  (`_check_trigger_rule`'s rule dict, `FAILURE_AVERSE_RULES`),
  `sam/sfn_templates/helpers/registration/sfn.tpl.json` (the duplicated JSONata
  ternary, trimmed identically — parity guard in
  `tests/sdk/test_trigger_rules.py` extended, not bypassed),
  `ui/src/components/TaskDetailModal/helpers.ts`. Codegen regenerated
  (`polyris.codegen.sync_enums`) for all `constants_generated.py` copies and
  `ui/src/generated/enums.ts` — `--check` clean.
- **Dead code removed, not synced.** Two hand-written, unused duplicate `TriggerRule`
  classes (`sam/lambdas/evaluate_deps/constants.py`) were deleted rather than kept in
  sync — confirmed zero callers in CE or EE. A third, `sam/lambdas/_shared/constants.py`,
  has a dedicated parity checker (`polyris/codegen/check_shared_constants.py`, ADR #83)
  proving it *is* consumed, so it was synced to the 5-rule set instead of removed.
- **Examples.** `examples/09_trigger_rules` (and `examples_temp/`'s copy) rewritten for
  the 5-rule set. `examples/08_realistic`'s `notify_on_failure` and
  `examples/05_branching`'s `alert_on_failure` — both built solely to demonstrate
  `one_failed` — removed entirely rather than reassigned to a different rule, since
  keeping them under a different name would misrepresent what they were built to show.
  `examples/03_fan_in_trigger_rule`'s docstring, which listed `one_failed`/`none_failed`
  as valid "other rules," corrected.
- **Tests.** `tests/sdk/test_trigger_rules.py`'s Python/JSONata parity mirrors trimmed
  to 5 rules; new tests confirm each removed name is rejected with the correct
  suggestion, the 5 kept rules are unaffected, and a genuinely-unknown (never-existed)
  name gets the generic message, not a removal suggestion. `tests/sdk/test_smoke.py`'s
  separate `test_trigger_rule_sync` (an independent, cruder JSONata/Python string-search
  check) had its own hardcoded 11-rule list, missed on the first pass through this
  change — trimmed to 5.
- **Docs.** `docs/features/DSL.md`, `docs/reference/MIGRATION.md`,
  `docs/architecture/ARCHITECTURE.md`, `docs/operations/TROUBLESHOOTING.md` updated to
  present 5 rules, not 11-with-caveats.
- **EE.** `polyris/_ee/ai/examples.py`, `polyris/_ee/ai/docs.py` (AI-assistant example/doc
  content referencing trigger rules) and `ee/team/tests/test_backfill.py` updated.

## Supersedes

Supersedes ADR #115 decision 4 (the "8 aliases accepted, documented as non-distinct"
framing) and decision 1's implicit assumption that `all_done` is the only useful
exception — 9 of the 11 original names are now removed outright, not aliased. Decisions
1–3 and 5 of ADR #115 (skip-fix, skip-cascade mechanics, blocked-rule verdict-splitting,
skip-origin) are untouched. Does not reopen ADR #116 (de-abort remains deferred,
unimplemented); `all_done`'s carve-out rationale is unchanged by this trim.
