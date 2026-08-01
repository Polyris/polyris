# ADR #122 — Registration fast-path resolves `deps_skip`/`deps_blocked`; `evaluate_deps` gates on `assets_ready` for wait_for coordination

> **Status:** ACCEPTED AND IMPLEMENTED.

## Context

Two closely-related hangs share one root cause: `dependency_wrapper`'s
`Wait_For_Dependencies` is a single `waitForTaskToken` state, but there are
**two** independent signal sources — `notify_dependents` (fires when the last
task_dep completes) and `notify_asset_subscribers` (fires when a `wait_for`
asset arrives) — and both share the same `wait_token`.

### Hang 1 — registration observes already-terminal task_deps with an unsatisfiable rule

Registration writes dep subscriptions, evaluates task_deps once, and either
`Signal_Ready_Immediately` (both branches ready) or `Wait_For_Signal` (end of
registration; wait held in `dependency_wrapper`). This split had no path for
"task_deps are all terminal AND the rule can never be satisfied on those
statuses" — the case a targeted restart of a downstream task, or a
`task_subset` backfill, actually produces. `notify_dependents` **already
fired** for each terminal upstream before this subscription existed and will
not fire again, so nothing signals `Wait_For_Signal`. The task hangs at
`waiting` until `Wait_For_Dependencies`'s `orchestration_timeout` (~24h)
elapses, then `Handle_Failure` marks it `upstream_failed` for the wrong
reason (`DependencyTimeout`).

### Hang 2 — `wait_for` + task_deps: whichever signal fires first wins

A task with both `dependencies=[A,B]` and `wait_for=[asset]` shares one
`wait_token` between the two signal paths. If the asset arrives before the
last task_dep completes, `notify_asset_subscribers` sends
`{signal: 'asset_ready'}`; `dependency_wrapper`'s `Check_Deps_Signal` doesn't
match `deps_skip`/`deps_blocked` and defaults to `Emit_Deps_Ready` — the task
runs against pending upstreams and its first `xcom.pull()` on the still-
`waiting` dep raises `PullError`. Symmetric hazard on the other side (task_deps
finish before the asset arrives): `notify_dependents` signals `deps_ready` and
the task runs before its declared asset is available.

## Decision

### Registration fast-path (Hang 1)

`Eval_Task_Deps`'s JSONata now computes a `$verdict` (`ready` | `skip` |
`blocked` | `wait`) alongside `$ready`, mirroring the Python
`_check_trigger_rule` + `FAILURE_AVERSE_RULES` split in `evaluate_deps`.
`Route_Combined_Check` gains a second branch:

```
verdict = 'ready' AND asset_deps_ready  → Signal_Ready_Immediately
verdict IN ['skip', 'blocked']          → Signal_Deps_Not_Ready       (new)
default                                 → Wait_For_Signal
```

`Signal_Deps_Not_Ready` sends
`{signal: 'deps_' & verdict, reason, immediate: true}` on the `wait_token`.
`dependency_wrapper` already handles `deps_skip` → `Emit_Deps_Skip` and
`deps_blocked` → `Emit_Deps_Blocked` — no changes to that side.
`Update_Status_Not_Ready` then writes `skipped` or `upstream_failed` to the
task record.

**No asset gate on the skip/blocked branch.** A task whose task_deps cannot be
satisfied can never succeed regardless of `wait_for`, so gating on
`asset_deps_ready` here would either hang the task waiting for an asset it
will never consume, or run it against blocked deps once the asset arrives.
The asset subscription becomes orphaned and is reaped by TTL.

### `assets_ready` coordination flag (Hang 2)

`pipeline-tokens` gains an `assets_ready` boolean field, set by:

1. **`check_assets`** — when it returns `ready=True` (either immediately or via
   the race-condition recheck) it marks `assets_ready=true` on the subscriber's
   record. Without this a task with `wait_for` + still-pending task_deps hangs:
   `evaluate_deps` (called later when task_deps complete via
   `notify_dependents`) sees `wait_for` + `assets_ready` absent and returns
   `verdict='wait'`, but no async asset arrival will ever trigger
   `notify_asset_subscribers` to flip the flag (there is no subscription).

2. **`notify_asset_subscribers`** — before signaling, it atomically
   `UpdateItem` sets `assets_ready=true` and reads the subscriber's record
   back (`ReturnValues=ALL_NEW`), then invokes `evaluate_deps` (via boto3
   `lambda.invoke`) with the record's `dependencies`, `trigger_rule`,
   `wait_for`, and `assets_ready=True`. It signals only if evaluate_deps
   returns `is_ready=True`. Otherwise the subscription is still deleted
   (asset has arrived, its job is done) and the notify_dependents path will
   signal later when task_deps complete — evaluate_deps will then see the
   flag set on the record and route to `Signal_Ready`.

`evaluate_deps`'s handler accepts `wait_for` and `assets_ready` from the
caller, and treats the outcome as `is_ready = deps_satisfied AND
assets_satisfied AND NOT is_paused`, where `assets_satisfied = (wait_for
empty) OR assets_ready`. `notify_dependents`'s Evaluate_Dependencies Payload
lifts `wait_for` + `assets_ready` from the subscriber's fetched record.

### Alias parity between Python and JSONata

`_check_trigger_rule` in `evaluate_deps` gains aliases for the three ADR #117
removed rule names (`none_failed`, `one_done`, `none_failed_min_one_success`)
so a pre-trim pipeline stops silently falling back to `all_success` semantics
at runtime. The registration `Eval_Task_Deps` JSONata gains the same aliases
in its `$ready` computation and includes `none_failed_min_one_success` in
`is_failure_averse` (it aliases to `one_success`, which is failure-averse) —
without this the registration fast-path (`verdict`) would diverge from the
notify_dependents path (`is_ready`) on the same statuses.

## Consequences

- **Zero-cost happy path.** Hang 1's new branches only fire in the edge case;
  Hang 2's new DDB write is only issued when `notify_asset_subscribers` or
  `check_assets` runs (i.e. only for tasks that actually have `wait_for`).
- **Fail-open on coordination errors.** `notify_asset_subscribers`'s
  `_coordinate_ready_check` falls back to `True` (signal anyway) when the
  subscriber's record is missing, `EVALUATE_DEPS_LAMBDA` is unset, or the
  Lambda invoke fails. Trade-off: on a transient invoke failure the task may
  run early against pending deps and raise `PullError` — visible in the UI
  and restartable — versus the alternative of hanging until orchestration
  timeout, which is invisible operationally.
- **Restart safety.** `Save_Task_Record` uses PutItem, which replaces the
  record; a stale `assets_ready=true` from a previous run cannot leak into a
  new attempt.
- **Test-isolation caveat.** Each Lambda package has its own `index.py` at a
  matching path, so a combined `pytest` run over multiple Lambda suites hits
  `sys.modules['index']` cache collisions. Not caused by this ADR, but this
  ADR touches three Lambda packages and so exercises the collision most
  visibly. Suites remain fully green when run per-Lambda.
- **Pre-existing IAM gap in `check_assets` fixed as a side-effect.**
  `CheckAssetsPolicy` lacked write permissions on `AssetSubscriptionsTable`
  (`subscriptions_repo.put/delete` was silently failing under best-effort
  catches). Added while wiring `PipelineTokensTable.UpdateItem` for the
  `assets_ready` flag.

## Alternatives considered

- **Zero new states via polymorphic `Signal_Ready_Immediately`** — reuse the
  existing state with a dynamic payload. Fewer lines, but the state name
  becomes misleading and each verdict-driven path is harder to trace in the
  CloudWatch step visualisation. Rejected in favour of the two-state approach.
- **Two separate wait tokens (task_deps + assets)** — would require
  `dependency_wrapper` to await both in a Parallel. Significant SFN refactor
  with no proportional benefit — the coordination flag achieves the same
  invariant with far less surface.
- **Have `evaluate_deps` self-fetch the subscriber's record from DDB** — kept
  the plumbing "explicit payload" instead. The read is trivially cheap for
  the caller (`notify_dependents` already fetches the record; the new call
  from `notify_asset_subscribers` fetches it once via
  `mark_assets_ready_and_get`'s `ALL_NEW`).
