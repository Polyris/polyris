# ADR #114 — Intervention-first failure model

> **Status:** ACCEPTED. Codifies existing behavior as an explicit product decision and
> establishes the constraint that ADR #115 (canonical trigger rules) is built against.
> No code changes — this ADR formalizes what `run_task`'s wrapper already does.

## Context

A task that exhausts its retries today pauses unconditionally in `waiting_decision`
(`sam/sfn_templates/helpers/run_task/sfn.tpl.json`) via `Wait_For_Decision`, up to the
global decision timeout, and does not resume until a human takes a task action (`retry`,
`mark_success`, `skip`, `fail` — `sam/lambdas/console_api/routes/tasks.py`). There is no
SDK parameter to opt a task out of this pause (`polyris/task.py` has no `on_failure`); the
pause is not configurable, it is the model.

This was not previously written down as a decision — it was implicit in `run_task`'s
control flow. Investigating why several `trigger_rule`s (`one_failed`, `all_failed`,
`all_done`, `all_done_min_one_success`) cannot fire (see
`docs/reference/SPIKE_TRIGGER_RULES.md`) surfaced the reason: those rules require a
**genuine terminal failure** to reach a sibling branch, but a resolved failure calls
`SendTaskFailure`, which `Run_All_Tasks`'s `Parallel` catches (`Catch: States.ALL ->
Pipeline_Failed`), cancelling every sibling branch before a failure-reactive rule can
evaluate. This is not a bug to be quietly patched — it is the direct consequence of the
pause-first model already in place, and the two are in tension: a model that always stops
for a human cannot simultaneously support rules whose entire purpose is to react
autonomously to a failure the human hasn't seen yet.

## Decision

**The failure model stays intervention-first.** A task failure pauses for a human
decision; it does not autonomously propagate through the DAG. This is treated as the
product's identity, not a gap:

- Competing orchestrators (Dagster) fail and stop; recovering requires clearing
  and re-running outside the failed execution. Polyris pausing **inside** the run with
  `retry` / `mark_success` / `skip` / `fail` actions (ADR #110 moved this to the free
  tier) is a real differentiator, not a lesser version of autonomous propagation.
- Because failure does not autonomously propagate, failure-reactive `trigger_rule`s
  cannot be supported as advertised today. ADR #115 narrows the canonical rule set
  accordingly instead of leaving a claim ("11 trigger rules") the engine
  cannot honor.
- The one rule whose loss has real product value — `all_done` as an unconditional
  cleanup/teardown step — gets a narrow, scoped exception (ADR #116), not a general
  reversal of this decision.

## Consequences

- Docs/positioning: describe polyris as intervention-first serverless orchestration, not
  as "trigger rules." See ADR #115 for the resulting rule set and
  the `README.md` / `docs/features/DSL.md` updates it drove.
- A full reversal of this decision (autonomous failure propagation, i.e. removing the
  `Parallel` abort-on-failure entirely) is a strategic pivot, not a fix, and is
  explicitly out of scope here. If ever revisited, it needs its own ADR and cannot be
  smuggled in as a trigger-rule bugfix.
- `all_done` is the one narrow, scoped exception — see ADR #116 — and does not reopen
  this decision for the other failure-reactive rules.

## Supersedes

None. This ADR documents existing behavior as a deliberate decision; no prior ADR
addressed the failure-pause model directly.
