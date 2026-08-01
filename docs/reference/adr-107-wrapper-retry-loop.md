# ADR #107 — In-place retry loop in the `run_task` wrapper (per-task `retries` / `retry_delay`)

> **Status:** ACCEPTED — implemented (option B). This **does** change the
> wrapper SFN flow (adds a retry stage at the failure chokepoint), which is why
> it is its own ADR. Builds on the parameter-parity contract (ADR #106).

## Context

`task.retries` / `task.retry_delay` were accepted by the decorators but only ever
populated **registry/debug metadata** — they produced no retry behavior at all. A
user setting `retries=3` got nothing; the only retry in the system was a
hardcoded `MaxAttempts: 2` on the Lambda invoke (a transient-error retry,
unrelated to `task.retries`).

Wiring per-task retries hits a wall from two sides:

1. **SFN `Retry` is static ASL.** `MaxAttempts` / `IntervalSeconds` / `BackoffRate`
   must be literal integers; they cannot be JSONata expressions reading
   `task_config.retries`. So the **shared** wrapper's per-state `Retry` cannot be
   per-task.
2. **A `Retry` on the per-task pipeline state is forbidden.** The pipeline
   `Task_<id>` state is `startExecution.waitForTaskToken` with a **deterministic**
   execution `Name` (`_make_execution_name_expr`). A native `Retry` there
   re-issues the same `Name` → `ExecutionAlreadyExists`. The deterministic `Name`
   is load-bearing: idempotency for `notify_dependents`, the UI countdown, and
   DynamoDB registration all key off it. (`generators.py` carries an explicit
   `# NOTE: No Retry here!` to this effect.)

Two designs can deliver per-task retries:

- **(B) In-place loop inside the wrapper.** A manual retry loop around the
  service dispatch, reading `task_config.retries` at runtime.
- **(C) Retry-safe `Name` + pipeline-state `Retry`.** Make the wrapper execution
  `Name` vary by retry count so a native `Retry` on the pipeline state works.

## Decision

**Option B.** Confine retries to the wrapper.

The wrapper already owns failure handling: every `Run_Task_<X>` has an identical
`Catch [States.ALL] → Save_Error_Waiting`, and the failure path runs
status-write + notify + a human-in-the-loop Slack decision before
`sendTaskFailure`. The retry loop slots in at that single chokepoint:

- Redirect all `Run_Task_<X>` `Catch` targets to a new **`Check_Should_Retry`**
  (Choice). Its condition — `$retry_attempt < task_config.retries` (default 0) —
  is JSONata, so it reads per-task config at runtime. On exhausted retries its
  `Default` falls through to `Save_Error_Waiting` (the existing path, unchanged).
- **`Wait_Before_Retry`** (Wait) backs off `task_config.retry_delay` seconds via a
  **dynamic** `Seconds` expression.
- **`Increment_Retry`** (Pass) bumps the attempt counter (`Assign: retry_attempt`,
  a JSONata variable that survives the `Catch`), strips the prior error from the
  data, and loops back to `Check_Task_Type` to re-dispatch the same task.

The counter is initialized once in `Prepare_Task_Input`. The static-`Retry`
limitation is sidestepped because the loop is built from **Choice + dynamic
Wait + a variable**, none of which are static `Retry` fields.

`task_config` carries `retries`/`retry_delay` **only when retries are requested**;
absent, the wrapper defaults to 0, so no-retry tasks keep their existing
`task_config` untouched (including `sfn`'s empty contract) and incur no snapshot
churn. `sfn` tasks with retries opt in uniformly — the chokepoint and the inner
`Run_Task_SFN` (which already uses a `$now()`-suffixed child `Name`) both retry
cleanly.

### Why not (C)

Each pipeline-level retry is a **fresh wrapper execution**, which re-runs the
entire preamble (`Emit_Task_Started`, status, skip/pause/deps/prepare) **and** the
existing failure-decision machinery (the first execution reaches `sendTaskFailure`
before the pipeline `Retry` starts the next) — yielding double DynamoDB entries,
double notifications, and a human decision per attempt. It also forces the
execution `Name` to vary, reopening exactly the idempotency invariant the
codebase deliberately protects. Option B keeps **one** wrapper execution per task
with the deterministic `Name` intact, composing with the failure-decision flow
instead of fighting it.

## Consequences

- **Backward-compatible.** `retries=0` ⇒ `0 < 0` is false ⇒ straight to
  `Save_Error_Waiting`, i.e. today's behavior. Existing snapshots change only for
  tasks that actually set retries.
- **Observability trade-off.** Retries are internal to one execution, so each
  attempt is **not** a separate console execution; DynamoDB status stays
  `running` across attempts. (C's per-attempt executions would have been more
  granular but at the cost above.)
- **Exponential backoff (opt-in).** With `retry_exponential_backoff=True` the wait
  is `min(retry_delay * 2^attempt, max_retry_delay)` — computed in
  `Wait_Before_Retry` as a JSONata expression over the runtime `retry_attempt`
  variable. `max_retry_delay` sets the ceiling (default 3600s when unset). Default
  is **off** (fixed `retry_delay`), so existing retries are unchanged; both flags
  are settable per-task on the decorator or globally via `default_args`. Jitter is
  applied opt-in via `retry_jitter=True`: **equal jitter** — the wait becomes a
  random value in `[base/2, base)` via JSONata `$random()`, spreading retries of many
  tasks so they don't all wake at the same instant. Off by default. (`$random()` is
  standard JSONata; confirm it against live SFN JSONata mode in a smoke run.)

## Verification

Contract/structural tests in `tests/sdk/test_run_task_template.py` (threading;
the decision condition at the 0/1/N boundaries; the dynamic Wait; loop closure
decision→wait→increment→dispatch) plus the strengthened convergence test in
`tests/backend/test_alerting.py` (every `Catch → Check_Should_Retry`, and
`Default → Save_Error_Waiting`). `cfn-lint` clean; 100% core coverage. Real retry
behavior under failure still requires a dev smoke run.
