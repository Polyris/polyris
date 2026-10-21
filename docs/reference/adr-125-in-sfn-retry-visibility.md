# ADR #125 — In-SFN retry visibility (`retry_attempt` field + `TASK_RETRY_STARTED` event)

> **Status:** ACCEPTED — extends ADR #107's in-place retry loop with the user-facing
> surface it was missing. Adds one DDB field on `pipeline-tokens`, one new
> `task-events` event type, and a two-chip UI contract that separates manual
> restarts from automatic retries.

## Context

ADR #107 wired per-task retries into the `run_task` helper SFN: a `Check_Should_Retry`
Choice → `Wait_Before_Retry` → `Increment_Retry` loop that re-dispatches a failed
task up to `task_config.retries` times. The loop works. But the only artifact a
user sees in the Console is the end state: either the task eventually succeeds
(looking as if nothing failed), or it exhausts retries and lands in
`waiting_decision` with the final error. The in-between attempts are invisible:

- `Update_Status_Running` writes the DDB `attempt` field **once** at task start.
  The retry loop bumps only a JSONata variable `$retry_attempt` — a value that
  never leaves SFN memory.
- `Emit_Task_Started` is reached once, before dispatch; the retry loop skips it.
- `WRAPPER_STARTED` sits in `dependency_wrapper`, one hop above `run_task`, and
  fires at most once per wrapper execution.

So the user cannot tell whether a task auto-retried twice before failing, or
whether an operator manually restarted it twice — both show `attempt=1` on the
row with no retry events in history.

Two kinds of "attempt counter" matter and must stay separable:

1. **Wrapper generation** (manual restart). `restart_task` starts a **fresh**
   `dependency_wrapper` execution with `attempt = prev + 1`. Every DDB write in
   `run_task` guards against ghost writes from the superseded wrapper via
   `ConditionExpression: attempt = :expectedAttempt`. This is load-bearing — it
   is what makes "Restart" safe during a race.
2. **In-SFN retry index**. The automatic retry loop runs inside **one** wrapper
   execution; the deterministic execution `Name` must stay stable (ADR #107
   discusses why).

Reusing the existing `attempt` field for both conflates them: a `attempt=2`
badge no longer tells the operator "a human restarted this" vs "the system
retried on its own", and the stale-attempt guard loses its single meaning. A
separate counter preserves both semantics.

## Decision

Add one DDB attribute and one event type:

- **`retry_attempt` (int, new)** on the `pipeline-tokens` row. Reset to `0` by
  `Update_Status_Running` at every wrapper start; bumped by a new
  `Save_Retry_Attempt` state after `Increment_Retry`. Guarded by the same
  `attempt = :expectedAttempt` ConditionExpression as the surrounding DDB writes,
  so a ghost retry from a superseded wrapper is rejected and routed to the
  existing `Stale_Attempt_Superseded` Succeed state.
- **`TASK_RETRY_STARTED`** on the `task-events` table. New entry on
  `TaskEventType` (`polyris/constants.py`), emitted by a new
  `Emit_Task_Retry_Started` state after `Save_Retry_Attempt` and before the loop
  rejoins `Check_Task_Type`. Event `event_time` sort key uses
  `$now() & '#15-' & $string($retry_attempt)` so it is unique per retry attempt
  and lexicographically sorts after the preceding attempt's `#10` TASK_STARTED
  and the hypothetical `#20` TASK_FINISHED of the failed attempt.

`max_retries` on the row (resolved by `resolve_max_retries` in `utils.py`) stays
the single source of truth for the denominator in the UI badge. **No new
`retries_max` snapshot field** — the existing `max_retries` already handles the
EE live-edit case (`ee/team/tasks.py` writes it directly) and `task_config.retries`
registration case.

### Fields the row carries after this change

| Field | Writer | Semantics |
|---|---|---|
| `attempt` | `dependency_wrapper` (init), `restart_task` (bump) | Wrapper generation — manual restart counter |
| `retry_attempt` | `Update_Status_Running` (reset=0), `Save_Retry_Attempt` (bump) | In-SFN retry index within the current wrapper |
| `max_retries` | registration (snapshot of `task_config.retries`), EE `update_task_config` (override) | Denominator; `resolve_max_retries` picks the live value |

### `Wait_Before_Retry` surface

The retry loop can sleep for up to one hour (default `retry_delay=5min` with
exponential backoff). The UI already renders a `CountdownTimer` driven by
`wait_delay_until_ms` and `wait_delay_started_ms` on the row (used by the pause
flow). The retry wait reuses the same fields: `Wait_Before_Retry` is preceded by
a DDB write that stores both values, and the UI picks them up with zero new
code.

### Event-type contract

`TASK_STARTED` means "the first dispatch of this task on this wrapper." The
`attempt` field distinguishes manual-restart generations.
`TASK_RETRY_STARTED` means "the automatic retry loop re-dispatched this task."
The `retry_attempt` field says which iteration (1-indexed).

Analytics that count "task starts per day" use `TASK_STARTED` only — retries do
**not** inflate that count. Analytics that count every dispatch sum the two.
This is the explicit reason to pay the cost of a new event type: reusing
`TASK_STARTED` for both would silently break every historical "task start" count
without warning.

## Why not reuse `attempt`

Three concerns, in increasing severity:

1. The stale-attempt guard `attempt = :expectedAttempt` is the only thing
   preventing a superseded wrapper from corrupting the newer wrapper's state
   during Restart. A single field with two writers (restart bumps it; retry
   bumps it) means the comparator value loses one of its two meanings every
   time either side writes.
2. The UI badge collapses to one axis: "Attempt N" cannot distinguish a user
   action from an automatic one. Operators debugging flaky tasks need to know
   whether the system retried on its own.
3. EE's Slack notifier currently says "manual restart" based on `attempt > 1`. A
   dual-use field forces every downstream consumer to disambiguate on a context
   it may not have.

## Why a new event type instead of reusing `TASK_STARTED` with `retry_attempt > 0`

Reusing `TASK_STARTED` is cheaper (one less parity surface), but every analytics
pipeline counting "task starts" from the event table would silently start
over-counting on retries. The event table is also read by third parties through
the public API; a semantic flip without a type signal is a breaking change in a
field they can't guard against.

## Consequences

- **Backward-compatible.** Rows and events without the new field read as `0` via
  the backend's `safe_int(..., 0)` default. Pre-change `TASK_STARTED` rows render
  as `retry_attempt=0`. Old UI pointed at new backend ignores the new field.
  New UI pointed at old backend sees `undefined`, falls through to the "no
  retries happened" branch.
- **Snapshot churn.** `run_task/sfn.tpl.json` gains three states (`Save_Wait_Delay`
  before `Wait_Before_Retry`, `Save_Retry_Attempt`, `Emit_Task_Retry_Started`)
  and bumps three Items (`Emit_Task_Started`, both `Emit_Task_Finished_*`) to
  carry `attempt` + `retry_attempt`. The pipeline-level ASL snapshots in
  `tests/sdk/test_asl_snapshots*` are unaffected: `run_task` is a shared helper,
  not inlined by `generators.py`. The structural tests in
  `tests/sdk/test_run_task_template.py` do change — the existing
  `Increment_Retry.Next == 'Check_Task_Type'` assertion flips to point at
  `Save_Retry_Attempt`.
- **Event sort uniqueness.** With `retry_delay=0`, two retries can land in the
  same millisecond. Encoding `retry_attempt` into the `event_time` suffix
  (`#15-<n>`) makes the `(task_run_id, event_time)` composite PK unique per
  retry; without this, DDB `PutItem` silently overwrites the earlier row.
  **Known edge case:** DynamoDB sorts `event_time` lexicographically, so
  `#15-10` sorts before `#15-9` when `retry_attempt >= 10`. Uniqueness is
  unaffected; only the timeline display order would be wrong for ten or more
  consecutive in-SFN retries within one wrapper — a configuration far outside
  normal use.
- **Row is a convenience; events are the source of truth.** On DDB throttle after
  the standard retry block, `Save_Retry_Attempt` falls through to the event
  emit. The row-side counter can lag the event sequence by one retry; the UI
  reads the row for the badge and the event stream for the timeline, so the two
  recover on the next successful write. Documented so a future consumer does
  not treat the row as authoritative.
- **Legacy `retry_attempts` (plural) field removed.** `get_task_config` returned
  a dead `retry_attempts` field no SFN state wrote — removing it alongside the
  new `retry_attempt` prevents a two-field trap (Principle #1).

## Verification

Structural tests in `tests/sdk/test_run_task_template.py`:

- `Save_Retry_Attempt` carries `ConditionExpression: attempt = :expectedAttempt`,
  catches `ConditionalCheckFailedException` → `Stale_Attempt_Superseded`.
- `Emit_Task_Retry_Started` writes `event_type = "TASK_RETRY_STARTED"` with both
  `attempt` and `retry_attempt`, `event_time` suffix unique per `retry_attempt`.
- Every `task-events` emitter (`Emit_Task_Started`, `Emit_Task_Retry_Started`,
  `Emit_Task_Finished_Success`, failure counterpart) carries both `attempt` and
  `retry_attempt` — a single structural test that would have caught the
  original "`Emit_Task_Started` missing `attempt`" bug.
- Two concurrent same-date wrappers do not corrupt each other's `retry_attempt`
  (per-execution_name row isolation; execution name includes
  `pipeline_execution_short`).

Parity test in `tests/sdk/test_task_event_type_parity.py` pins the
`TASK_RETRY_STARTED` literal across `polyris/constants.py`,
`run_task/sfn.tpl.json`, backend `EventType`, and the UI renderer.

Backend tests in `tests/backend/test_api_routes.py` cover `_format_task_row`
surfacing `retry_attempt`, the history endpoint serializing the new event type,
`batch_get_run_meta` back-filling the field, and `pipelines_list` carrying it
on the per-task dict.

UI tests cover the four `(attempt, retry_attempt)` badge combinations in
`TaskDetailModal.test.tsx` and the chip rendering in `AllTasksView.test.tsx`.
`aria-label` is pinned so screen readers hear the full phrase.

Smoke test in `tests/e2e/test_retries.py` (gated by `@pytest.mark.smoke`) runs a
flaky Lambda fixture that fails until `attempt >= desired`, asserts the
History tab shows one `TASK_STARTED` + N `TASK_RETRY_STARTED` events, and the
final row has `retry_attempt = N`.
