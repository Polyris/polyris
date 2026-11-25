# ADR #85 — Fix skip-task JSONata (`$contains` on array) + evaluate template JSONata in CI (v0.80.1)

#### Symptom

A pipeline-target backfill (`cascade: auto`) over 2 dates appeared stuck:
the Backfills detail showed RUNNING, 0/2 processed, 0 children; yet the
child pipeline executions had run and showed one task FAILED with the rest
SKIPPED. The failing task's run-task-helper execution ended with
`States.QueryEvaluationError` in state `Check_Should_Skip_Task`:

> T0410: Argument 1 of function "and" does not match function signature

#### Root cause

The task-subset skip feature (ADR #51) passes a `skip_tasks` **array** into
each task. `Check_Should_Skip_Task` in `sam/sfn_templates/helpers/run_task`
decided whether to skip with:

```
$exists($states.input.skip_tasks) and $count($states.input.skip_tasks) > 0
  and $contains($states.input.skip_tasks, $states.input.task_name)
```

`$contains(str, pattern)` is a JSONata **string** function. Passing the
`skip_tasks` array as argument 1 throws a signature error — but only when the
array is non-empty (an empty array short-circuits via `$count > 0`). So tasks
that received an empty `skip_tasks` ran fine, while any task given a non-empty
skip list threw, failed, and cascaded `upstream_failed`/skipped to its
descendants. This is why a previous simple backfill succeeded but this one
(with a populated skip list) did not. It was a latent template bug, unrelated
to the v0.80.0 constants work, which never touched the orchestrator.

Confirmed against a JSONata evaluator: the old expression throws on every
non-empty `skip_tasks`; the sibling `dependency_wrapper` template already did
the same check correctly with the array-membership operator.

#### Fix

Use the `in` operator (matching `dependency_wrapper`):

```
$exists($states.input.skip_tasks) and ($states.input.task_name in $states.input.skip_tasks)
```

Verified with `jsonata-python`: empty → False, match (single or multi) →
True, no-match → False, absent → False, never throws.

#### Prevention — evaluate template JSONata in CI

The deeper problem: SFN-template JSONata had **no CI validation**, so a
malformed expression shipped and only failed at runtime. Added
`tests/sdk/test_sfn_skip_task_jsonata.py`, which extracts every skip-task
Choice condition across the templates and evaluates it (via `jsonata-python`,
a new dev dependency) against representative inputs — asserting it never
throws and returns the correct membership boolean — plus a static check that
`$contains()` is never applied to the `skip_tasks` array. Verified the guard
goes red on the old expression and green on the fix.

This is a first, targeted step (skip-task conditions use only standard
JSONata, so a standard evaluator matches ASL's dialect). Broadening JSONata
evaluation to all Choice conditions — many of which use ASL-only context like
`$states.context` — is logged in BACKLOG as a larger follow-up.

#### Operational note

In-flight executions started under the old template are not retroactively
fixed. After deploying v0.80.1, cancel the stuck backfill and start a new one.

#### Follow-up — defensive `$string()` wraps + audit harness as maintenance tool

A post-fix exhaustive audit (618 expressions × 35 input variants =
21,630 evaluations) found two places where `$length()` was called on a
field whose stringness was guaranteed only by an upstream invariant
(`task_output` set via `$string(...)` by the 7 `Run_Task_*` handlers;
`error` set via `$string(...)` by Catch outputs). Safe in production, but
fragile: if a future change drops the upstream `$string()`, both sites
would throw at runtime — exactly the same failure class as the original
bug.

Hardened defensively by wrapping the `$length()` calls (and the
value-passthrough branches) with `$string()`:

- `Save_Success` and `Save_Canonical_Output`: introduced
  ``$to := $string($states.input.task_output)`` and reused everywhere in
  the expression — the truncation check, the truncated-size encoding, and
  the value stored in DynamoDB all go through the same string.
- `Interactive_Slack` Cause: introduced ``$causeStr := $string($cause)``
  and reused.

`$string()` is idempotent on strings, so production behavior is unchanged
when the invariant holds; if a future change ever breaks the invariant,
the expressions degrade gracefully (storing the JSON-encoded form) instead
of throwing `States.QueryEvaluationError`. The full audit harness re-run
post-hardening reports 0 type/signature throws.

Also committed the audit harness as `scripts/audit_jsonata.py` and a
`make audit-jsonata` target — a maintenance tool to run before releases or
after non-trivial template edits. Not added to CI (input synthesis isn't a
perfect mirror of ASL runtime, so output is human-read; the focused
skip-task guard in `tests/sdk/` remains the CI gate).
