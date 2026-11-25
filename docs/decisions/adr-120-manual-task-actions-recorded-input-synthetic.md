# ADR #120 — Manual task actions: recorded input, synthetic output markers, asset events on Mark Successful, safe direct Restart from `waiting_decision`

**Context.** Skip / Mark Successful / Mark Failed / Stop all resolve a task's orchestration
token directly from `console_api`, bypassing every state in the `run_task` wrapper a normal
completion goes through. Investigation (full detail in
`docs/design/SPIKE_TASK_ACTIONS_DATA_LIFECYCLE.md`) found this had three consequences
beyond the originally-reported "Input tab is empty for a skipped task": a downstream task
calling `xcom.pull()` on a manually-resolved task crashes with `PullError`; the manual
action's immediate `notify_dependents_via_sfn` call can unblock downstream trigger rules
before a subsequent Restart's own outcome is known; and asset events for a task's outlets
are never emitted, so push-triggered downstream pipelines silently never fire. Separately,
a request to make Restart work directly from `waiting_decision` (instead of requiring Stop
first) surfaced a field-name mismatch that made `restart_task`'s "stop the old wrapper"
step a silent no-op for every restart, consequential specifically for this new case since
`waiting_decision`'s wrapper is still genuinely alive.

**Decisions, six pieces:**
1. **`task_input` written on start, not just success** — new `Save_Task_Input` state
   (`run_task/sfn.tpl.json`), positioned after upstream/variables resolve, before
   dispatch. `updateItem`, not `putItem` like `Save_Canonical_Output` — a same-day
   re-run's input write must never clobber a prior run's real `result` at the same
   `output#pipeline#task#date` key.
2. **Synthetic output marker on every manual action** — `{"_manually_resolved": true,
   "_resolution": ..., "_reason": ...}` written to the same canonical key `xcom.pull()`
   reads, conditioned on `result` not already existing. `xcom.pull()`'s own contract
   (raise loud on genuinely missing output) is unchanged — this fills the specific gap
   where "missing" actually means "resolved without running," not "something is wrong."
3. **Asset events emitted for Mark Successful only** — the one action that explicitly
   claims real work happened ("verified via logs/S3"); Skip/Fail/Stop correctly continue
   not to. Looks up outlets from the registry's `dag_metadata` (ADR #119's unified nodes)
   since outlets are a deploy-time property never stored per-execution. Scoped to the
   push-model SFN notification (already fully wired in the SAM template); the pull-based
   subscriber Lambda was not wired — a real infrastructure change (new IAM permission +
   env var), left as a separate decision.
4. **Fixed `restart_task`'s `Stop_Old_Wrapper` field-name mismatch** — it read
   `wrapper_execution_arn`, a field the wrapper never wrote (it writes the same value as
   `run_task_helper_arn`). Harmless for a genuinely terminal restart (nothing left running
   to stop); consequential for restarting directly from `waiting_decision`, where the
   wrapper is still alive. (A first fix attempt used `helper_arn` — also wrong, since
   `:helper_arn` in the wrapper's own `UpdateExpression` is only the value-placeholder
   name, not the field; caught before shipping.)
5. **Attempt-keyed `ConditionExpression` on every status-writing state** (`Save_Success`,
   `Save_Failed`, `Save_Error_Waiting`) — (4) only makes killing a stale wrapper *likely*;
   this makes a stale write from a surviving ghost structurally impossible to apply,
   regardless of whether the kill succeeded. Rejected writes route to a new
   `Stale_Attempt_Superseded` (`Succeed`) state — no misleading finished/failed event, no
   stale dependent notification.
6. **`RESTARTABLE_STATUSES` extended to `waiting_decision`** — Restart now works directly,
   without Stop first. `restart_task_helper`'s `Stop_Old_Wrapper` is a hard
   `states:StopExecution` kill, not `send_task_success`/`failure`, so unlike Stop it never
   triggers `notify_dependents_via_sfn`'s immediate downstream notification — using
   Restart as intended (retry, don't tell anyone yet) no longer risks a downstream task
   reacting to a stale abort before the retry's real outcome is known. Found and fixed a
   related drift bug while wiring this up: the fallback (no-`RESTART_HELPER_ARN`) path had
   its own, separately hand-maintained `RESTART_CONDITION` string that was already missing
   `waiting_decision` — now derived from `RESTARTABLE_STATUSES` directly, eliminating this
   class of drift going forward rather than adding one more hardcoded value to it.

**Consequences.**
- Stop remains available alongside Restart on a `waiting_decision` task — they serve
  different intents ("done, tell everyone now" vs. "let me retry, don't tell anyone yet"),
  not one superseding the other.
- Every new safety property (field-name correctness, catch ordering, the
  `ConditionExpression` guard, the synthetic marker's never-overwrite guard, the
  asset-event gating) was mutation-tested: the corresponding fix was temporarily reverted
  and the new test confirmed to fail, not just pass with the fix in place.
- §7c's pull-based subscriber notification and §7a's "should the marker be visually
  badged in the UI" remain open, deliberately out of scope for this pass.
