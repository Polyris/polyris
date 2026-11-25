# ADR #121 — Restart correctness: two-level wrapper stop, `task_config`/`outlets` reconstruction, `restart-` name prefix

**Context.** Live testing of ADR #120's direct-Restart-from-`waiting_decision` surfaced
three further, real gaps — full detail in `docs/design/SPIKE_TASK_ACTIONS_DATA_LIFECYCLE.md`
§9.5–9.6.

**Decisions:**
1. **Two executions must be stopped, in order, not one.** There are two levels: the
   outer `dependency_wrapper` (waits for dependencies, then synchronously calls the inner
   `run_task` via `startExecution.sync:2`) and the inner `run_task` itself. An initial fix
   read `wrapper_execution_arn`, found it unused in `run_task`'s own template, and
   concluded (incompletely — only that one template was checked) it must be a typo for
   `run_task_helper_arn`. It's genuinely written, by `registration_helper`'s
   `Save_Task_Record`, for the outer wrapper. Killing only the inner wrapper directly
   left the outer one alive, synchronously waiting on it — its own `Catch` fired,
   cascading a false `notify_dependents('failed')` to downstream tasks *immediately*,
   before the restarted attempt's own work even began. Confirmed live: a downstream task
   was marked `upstream_failed` within seconds while the restarted task's underlying SFN
   was still genuinely running (and later genuinely succeeded). Fixed: split into
   `Stop_Old_Outer_Wrapper` (external `StopExecution` cannot trigger a wrapper's own
   `Catch`) then `Stop_Old_Inner_Wrapper` (safe once the outer wrapper is already dead).
2. **`task_config`/`outlets` reconstruction.** Both are deploy-time DAG properties, never
   stored on the per-execution record — `Start_New_Wrapper` tried to read
   `item.task_config.S`/`item.outlets.S`, fields that never existed, always silently
   yielding `{}`/`[]`. Fixed at the source: extracted `_build_task_config_and_arn`
   (shared between the original dispatch and the DAG builder, so this can't drift again),
   added `task_config` to the registry's `dag_metadata` (`outlets` was already there per
   ADR #119/#120), and `restart_task`'s Python handler now looks both up via a new shared
   `_lookup_dag_node` (also used by ADR #120's asset-event lookup) and passes them
   explicitly in `restart_task_helper`'s input.
3. **`restart-` name prefix.** Requested directly, to distinguish a restarted attempt's
   underlying business SFN invocation from the original in the AWS console without
   checking duration/timestamps. Only `Run_Task_SFN` needed it — the only `Run_Task_*`
   variant that names a child Step Function execution (the others call AWS services
   directly). Retired the dead `is_restart` flag `Start_New_Wrapper` set (never read
   anywhere) — `attempt > 1` already captures the same thing, more precisely, and is what
   the prefix condition uses.

**Consequences.**
- `task_config` is now visible in `generate_dag_json`'s public output (`polyris-output
  --json`) and the registry, in addition to already being implicitly present in the
  deployed wrapper's own SFN Argument (visible to anyone who can read the deployed
  CloudFormation/SFN definition already). Not currently read or displayed anywhere in the
  UI. Worth keeping in mind if a task's `task_config` is ever used to carry something
  more sensitive than retry/worker settings (e.g. an inline Lambda payload with a
  secret) — that would already be a questionable practice given it's baked into the
  deployed template either way, but this makes it one step easier to notice.
- ASL snapshots regenerated; diffed before accepting to confirm the only change was the
  new `task_config` field appearing on nodes that have one, nothing else.
- Every new behavior (both wrapper levels stopped in order, task_config/outlets
  correctly threaded, the name prefix) was mutation-tested: reverting each fix was
  confirmed to make its corresponding test fail.
- `_build_task_config_and_arn` also gained a direct, isolated unit test suite
  (`tests/sdk/test_task_config_contract.py::TestBuildTaskConfigAndArnDirectly`) —
  previously covered only indirectly through its two callers. Pins the lambda-specific
  `task_arn` resolution (discarded by the existing indirect tests, which only read
  `task_config`), the sfn-stays-empty contract, and retry-key conditionality, in
  isolation from either caller's surrounding construction.
