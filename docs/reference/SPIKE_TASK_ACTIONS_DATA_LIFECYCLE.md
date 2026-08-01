# Spike: Stop→Restart mechanics, event history, XCom, and the manual-action
# escape hatch's real downstream consequences

**Question(s), as asked across this thread.** What happens, status-wise, when you
Stop then Restart a `waiting_decision` task? Is everything tracked in the event
history — and worth checking more broadly, not just those specific events? Does
XCom survive a restart? Why do Skip and Mark Successful lose upstream/default
input — and, refined: the *task's own* output not existing after Skip/Mark
Successful makes sense, but shouldn't the *upstream* input (already produced by
earlier, completed tasks) still be there? Followed by an explicit request for a
critical, QA-style pass over all of this together, checked against project
principles, not just the individual questions as asked.

**Bottom line.** The mechanical questions (§1–3) all have confirmed, benign
answers — restart status transitions, event history, and XCom all behave
correctly. §4–6 confirm a real but *cosmetic* gap: manual actions and genuine
failures leave the Input/Output tab empty, which is fixable (§6, scoped for
today) or at least explainable (§4/§5). §7, from the requested critical pass,
found something more serious: the same root cause (manual actions bypass the
wrapper's normal completion states) has three **functional**, not cosmetic,
consequences — a downstream task can crash calling `xcom.pull()` on a
skipped/marked task (§7a), Stop unblocks downstream trigger rules before a
later Restart can matter (§7b), and asset-triggered downstream pipelines
silently never fire (§7c). None of these surface any warning in the console
today. §7 is the part worth treating as the priority, not §5/§6.

## 1. Stop → Restart status mechanics

`stop_task` (`tasks.py`) branches on `TASK_WAITING_STATUSES`: a `waiting_decision`
task becomes **`aborted`**, not `stopped` — `stopped` is reserved for interrupting
a `running` task. Both are in `RESTARTABLE_STATUSES`, so either path makes
Restart available afterward; the label just differs; this is worth knowing
before assuming "stopped" specifically.

**A real side-effect worth knowing, not fully chased down here:** for `aborted`
specifically, `stop_task` calls `notify_dependents_via_sfn` immediately — anything
downstream of the stopped task is told *now* that this task is done (as a
failure), before you've even clicked Restart. **Confirmed and generalized
later in this document (§7b/§8): this isn't specific to `aborted`/`stop_task` —
the same immediate notification happens for all four manual actions,** via
the shared `_execute_task_action` helper they all go through.

`restart_task` (`restart_task/sfn.tpl.json`): `Get_Task_Config` → `Check_Task_Exists`
→ `Stop_Old_Wrapper` (best-effort, catches all errors — the old wrapper may
already be stopped) → `Delete_Old_Record` (deletes *only* the `execution_name`-
keyed status record) → `Start_New_Wrapper` (new SFN execution named
`restart-{millis}-{task_name}`, carrying `execution_name_override` so the new
wrapper writes back under the *same* `execution_name` the console/history
already know about).

## 2. Event history across restarts — an attempt counter already exists

`Start_New_Wrapper`'s input includes `"attempt": (existing attempt ?? 1) + 1` —
restart already increments a real attempt counter (matching the "attempt 1"
seen in the History tab's first event in the screenshot this thread started
from). Task events live in a **separate** table (`task_events_repo`, keyed by
`task_run_id`), not on the record `Delete_Old_Record` deletes. `task_run_id`
defaults to the *original* `execution_name` and isn't regenerated per attempt,
so events across every attempt share the same key and should all surface
together in the History tab, attempt-numbered. **Confirmed by reading the
code; not exercised against a live restart in this spike** — worth an actual
Stop→Restart test to visually confirm "attempt 2" shows up correctly, since
reading the template correctly is not the same as watching it work.

## 3. XCom survives restart

`xcom.pull()` reads the canonical output store keyed `output#{pipeline}#{task}#{date}`
— a *different* key entirely from `execution_name` (what `Delete_Old_Record`
deletes). Restart doesn't touch this key at all; the old output stays readable
until the restarted attempt actually produces new output and overwrites it.
Nothing to fix here — this already behaves the way you'd want.

## 4. Skip / Mark Successful / Fail / Stop all skip `task_input` recording — confirmed systemic, not Skip-specific

`Save_Canonical_Output` — the *only* state that writes `task_input` (upstream +
variables) alongside `result` — lives exclusively in the `run_task` wrapper's own
SFN template, reached only via the task's normal successful-completion path.
None of the four manual actions in `tasks.py` (`skip_task`, `mark_success`,
`fail_task`, `stop_task`) ever invoke it — all four resolve the orchestration
token directly via a synthetic `send_task_success`/`send_task_failure` call from
the console_api Lambda, bypassing the wrapper's internal states entirely. This
is why *every* manually-resolved task — not just skipped ones — has no recorded
`task_input`/`result`: the write is tied to the wrapper actually running end to
end, and manual actions are specifically the escape hatch for *not* doing that.

For Mark Successful this is arguably the most legitimate case for the gap:
the feature's own doc line is "task stuck but work completed (verified via
logs/S3)" — the real work may well exist somewhere (the underlying Glue job,
S3 output), the console just has no way to know that, because the person
clicking Mark Successful verified it externally, not through the wrapper
reporting back.

## 5. The refined ask — upstream input should still be recoverable, and it's technically feasible

The task's own `task_input` being absent after Skip/Mark Successful is correct
and expected (nothing to fix — the task never ran, there's nothing to have
recorded). The distinct point: the *upstream* tasks this task depends on
already ran, already produced real output, sitting under their **own** canonical
keys (`output#{pipeline}#{upstream_task}#{date}`) — independent of whether
*this* task ever ran. `get_task_output`'s current implementation only reads
`task_input` — the frozen snapshot this specific task wrote for itself — with
no fallback to reconstructing it from dependencies when that snapshot is
missing.

This is closable: the task record already carries its own `dependencies` list
(confirmed present — `tasks.py`'s task-detail response includes
`'dependencies': item.get('dependencies', [])`). A fallback path in
`get_task_output` could, when `task_input` is missing, look up each dependency's
own `output#{pipeline}#{dependency}#{date}` key and reconstruct an equivalent
"here's what upstream would have given you" view — the same data
`xcom.pull()` would have assembled had the task actually run and called it.

**Not yet designed here:**
- Whether the reconstructed view should be visually distinguished from a
  real, wrapper-recorded `task_input` (e.g., "reconstructed from dependencies,
  not a live recording" vs. today's implicit assumption that whatever's shown
  came from an actual run) — probably yes, to avoid it reading as more
  authoritative than it is.
- Whether `variables` (the other half of `task_input`, alongside `upstream`)
  has an equivalent reconstruction path, or whether only the `upstream` half
  is realistically recoverable this way (`variables` come from the DAG
  definition/registration, not a per-task record, so likely reconstructable
  too, but not checked here).
- What happens when a *dependency* is itself missing its own output (e.g., the
  dependency was *also* skipped) — presumably the reconstruction degrades
  gracefully per-dependency rather than failing the whole view, but this
  needs its own decision, not an assumption.

## 6. Confirmed live, broader than §4/§5 — `task_input` is written ONLY on the success path, never on failure

**⚠️ TODO — planned fix for today, higher priority than §5's reconstruction idea.**

Confirmed against real running executions (`sales-etl`, 2026-07-24): a task
that is currently `running` correctly shows "No input recorded" (nothing to
show yet — expected). But a task that has **already actually run and failed**
(`transform`, `waiting_decision` after a real failure) *also* shows "No input
recorded" — even though it genuinely executed with a real input, unlike
Skip/Mark Successful where the task never ran at all.

Root cause, traced precisely: `Save_Canonical_Output` (`run_task/sfn.tpl.json`)
is reached *only* via `Save_Success`'s `Next` — there is no equivalent state on
the failure path writing `task_input` before a task fails. Combined with §4's
finding (manual actions bypass it too), this means **`task_input` is currently
recorded in exactly one situation: the task ran to completion and succeeded
through the normal wrapper path.** Every other outcome — still running,
genuinely failed, manually skipped/marked/stopped — shows an empty Input tab.

This is a bigger problem than §4/§5 individually suggested: the Input tab is
specifically most useful for debugging *why a task failed*, and that's
precisely the one additional case (beyond Skip/Mark Successful) where it's
empty today, despite the task having had a real input worth showing.

**Planned fix:** write `task_input` (upstream + variables) as soon as the task
*actually starts* — i.e., move this write earlier in the wrapper's flow, no
longer conditioned on reaching the success path at all. This makes input
available for running, failed, and waiting tasks alike, and as a side effect
also closes part of §4's gap: Skip/Mark Successful/Stop applied to a task that
*had* already started (e.g., stopping a genuinely `running` task, not just a
`waiting_decision` one) would then correctly show the input it started with,
even though the task's own *output* still correctly stays empty (nothing to
show — the task never finished). Does not on its own address `waiting_decision`
tasks that never started at all, or resolve whether `variables`/`upstream`
reconstruction (§5) is still worth building for that remaining case.

## 7. Full QA sweep — the manual-action escape hatch has three severe, confirmed downstream consequences beyond the UI

Systematic pass over every action × status combination, checking what a real
downstream task (not just the console UI) actually experiences. Three genuine,
functional (not cosmetic) gaps confirmed — all stemming from the same root
cause: **Skip / Mark Successful / Mark Failed / Stop all resolve the
orchestration token directly from `console_api`, bypassing every state in the
`run_task` wrapper that a normal completion goes through** — not just
`Save_Canonical_Output` (§4/§6), but also asset-event emission and (per §1)
dependent-notification timing.

### 7a. `xcom.pull()` raises, it doesn't degrade — a downstream task can crash because an upstream task was skipped

`polyris/xcom.py`'s `pull()` does a direct `dynamodb:GetItem` on
`output#{pipeline}#{task_name}#{date}` and **raises `PullError`** — not
`None`, not an empty dict — when nothing is stored: `"no output stored for
task '{task_name}' ... did it return anything?"`. Since Skip/Mark
Successful/Fail/Stop never reach `Save_Canonical_Output`, none of them ever
populate that key. Concretely: task C depends on task B; task B fails, gets
Skipped or Marked Successful; task C's own code calls `xcom.pull("B")` because
it genuinely needs B's data — task C now **crashes** with `PullError`, purely
as a consequence of a manual decision made on B, through no fault of C's own
logic. This is a real pipeline-breaking outcome a user could hit today with
no warning anywhere in the console that it's coming.

### 7b. `aborted` unblocks downstream *before* a subsequent Restart can matter

`evaluate_deps`'s trigger-rule engine imports `TASK_TERMINAL_STATUSES` (which
includes `aborted`) directly from `constants_generated` — meaning the instant
a `waiting_decision` task is Stopped (→ `aborted`, confirmed in §1),
`all_done`/`one_done`/similar downstream trigger rules see it as *finished*
and can fire immediately. This is the same timing concern flagged as an open
question in §1, now confirmed rather than merely suspected: if the downstream
task then runs and calls `xcom.pull()` on the aborted task, it hits §7a's
crash — and clicking Restart *afterward*, even if it succeeds this time, is
too late; the downstream task already ran (and likely already failed) against
the aborted state. Stop→Restart is not a safe "pause the whole subtree and
resume later" operation today if anything downstream depends on this task's
actual output.

### 7c. Manual actions also skip asset-event emission — push-triggered downstream pipelines silently never fire

`Emit_Asset_Events` (`run_task/sfn.tpl.json`) — the state that calls
`notify_asset_consumers`/`notify_asset_subscribers` for a task's `outlets` (the
push-model mechanism from earlier in this project) — appears **zero times**
in `tasks.py`. Marking a task with `outlets=[some_asset]` as Skipped, Mark
Successful, Mark Failed, or Stopped means the asset event for `some_asset`
is never emitted at all — any pipeline scheduled on that asset
(`schedule=[some_asset]`, push model) silently never triggers, with nothing
in the console suggesting why. This is the same class of gap as 7a/7b, just
for the asset-trigger path instead of `xcom.pull()`/trigger_rule.

### What this adds up to, as a real user/QA would experience it

The manual-action buttons (Skip, Mark Successful, Mark Failed, Stop) are
framed in the UI as "resolve this one task and let the pipeline continue" —
and for *simple* pipelines (no real inter-task data dependency, no asset
outlets, no downstream trigger_rule sensitivity) that framing holds up fine.
But for anything using `xcom.pull()`, `outlets`, or a non-default
`trigger_rule` downstream of the resolved task — which is a large fraction of
realistic pipelines, not an edge case — a manual action can silently produce
one of: a crashed downstream task (7a), a downstream task that ran against
stale/wrong data because it fired before a later Restart corrected things
(7b), or a push-triggered pipeline that never runs at all (7c). None of these
surface any warning in the console at the moment the manual action is taken.

**7a and 7c are not revisited further in this document** — the thread that
followed (§8–10) was specifically about the Restart feature, so only 7b got a
deeper pass. That's a reflection of what was actually investigated, not a
signal that 7a/7c are lower-priority or resolved — see the final summary at
the end of this document for their current, still-open status.

**Blast radius confirmed to include EE, not just the console:** `polyris-ee`'s
Slack decision buttons (`ee/team/slack.py`) and Backfill orchestration
(`ee/team/backfill.py`) call the same CE-provided `skip_task`/`mark_success`/
`stop_task`/`restart_task` handlers in `tasks.py` — §7a/7b/7c apply identically
to a Slack "Mark Successful" click or a Backfill-driven skip, not just a
console button.

## 8. §7b, specifically for the Restart feature this thread started with

Traced precisely: `notify_dependents_via_sfn` is called from inside
`_execute_task_action` — the **shared** helper `skip_task`, `fail_task`, and
`mark_success` (not just `stop_task`) all go through. So §7b's premature
downstream-unblock is not something the Stop-for-`waiting_decision` fix
introduced — Skip and Mark Failed already had it, and were already available
on `waiting_decision` before that fix. Nothing new was made *possible* here.

What's specific to Restart, though: Skip and Mark Failed are framed, and used,
as *final* resolutions — "this is done, one way or another" — so notifying
dependents immediately matches the intent. Stop, by contrast, exists
specifically to say "not done — pausing so I can retry" (its own subtitle:
"can restart later"). The system doesn't distinguish that intent from Skip/
Fail's: it notifies dependents immediately either way. Concretely, using the
Restart feature as intended — Stop, fix something, Restart, this time it
succeeds — can still leave a downstream task that already ran (and possibly
already crashed via §7a) against the *aborted* outcome, before the successful
retry ever happened. The retry succeeding doesn't undo what already ran
downstream in response to the abort.

This doesn't mean the Restart fix from earlier was wrong to make — Skip/Fail
already carried this exact risk for `waiting_decision` tasks, so Restart isn't
introducing a new hazard class, only one more door into an existing one. But
it does mean Restart's own framing ("can restart later") is somewhat
optimistic about what "later" protects against, and worth being honest about
if this becomes user-facing guidance rather than just a spike finding.

## 9. A cleaner idea for Restart specifically — and a genuine, pre-existing blocker found while checking it

Mike's own alternative to §8's "add a warning" idea: instead of Stop (which
goes through `_execute_task_action` and unconditionally calls
`notify_dependents_via_sfn`) then Restart, why not let Restart itself directly
handle a `waiting_decision` task — hard-kill the currently-waiting wrapper
execution via `states:StopExecution` (which does **not** go through
`send_task_success`/`send_task_failure` at all, so `notify_dependents_via_sfn`
is never called), then start a fresh attempt. `restart_task_helper`
(`restart_task/sfn.tpl.json`) already does exactly this sequence — `Stop_Old_
Wrapper` → `Delete_Old_Record` → `Start_New_Wrapper` — for terminal statuses.
Extending `RESTARTABLE_STATUSES` (`tasks.py`) to also accept `waiting_decision`
looked like a small, safe change with no need to touch `notify_dependents_via_
sfn`'s timing at all — a genuinely more elegant fix than §8's warning dialog.

**Checked before endorsing it, and found a real, pre-existing blocker,
independent of this specific idea:** `Stop_Old_Wrapper` reads
`$states.input.item.wrapper_execution_arn.S` to know which execution to kill.
Traced every write in `run_task/sfn.tpl.json` — the wrapper's own execution ID
(`$states.context.Execution.Id`) is captured early, correctly, right when
status becomes `running` — but saved under the DynamoDB attribute name
**`helper_arn`**, not `wrapper_execution_arn`. `wrapper_execution_arn` is never
written anywhere. This is a field-name mismatch bug that already exists today,
for the terminal-status restart path this feature already ships — `Stop_Old_
Wrapper`'s `StopExecution` call always receives an empty ARN, always fails,
and is always silently swallowed by its own Catch ("Ignore errors — wrapper
may already be stopped or ARN missing").

**Why this hasn't been noticed:** for a genuinely terminal task (already fully
finished naturally before you click Restart), there's nothing left running to
stop anyway — `Stop_Old_Wrapper` silently failing to find it is harmless by
coincidence, not because it's actually working. For `waiting_decision`
specifically — the case this idea is for — the wrapper is **still genuinely
alive**, sitting in `Wait_For_Decision`'s timer. If `Stop_Old_Wrapper` can't
actually kill it (same bug, now consequential), that ghost execution keeps
running in the background. When its own `decision_timeout_seconds` eventually
elapses, it proceeds to `Save_Failed` and attempts its own callback — writing
`status: failed` to the same `execution_name` the *new*, restarted attempt is
by then already using (`Start_New_Wrapper` reuses `execution_name_override`).
This is precisely the "duplicate status" hazard flagged as a concern before
even proposing this — confirmed real, not hypothetical, and it predates and is
independent of the Restart-from-`waiting_decision` idea, but that idea is the
first place it would actually bite.

**This must be fixed first, as a prerequisite:** either rename `helper_arn` →
`wrapper_execution_arn` when writing (matching what `Stop_Old_Wrapper` already
expects), or change `Stop_Old_Wrapper` to read `helper_arn` instead. Either is
a small, mechanical fix. Until it's done, §9's "Restart directly from
`waiting_decision`" idea — genuinely the more elegant fix for §7b/§8 — is not
safe to ship, because the one case it's *for* is exactly the case where the
pre-existing bug stops being harmless.

**Not yet verified:** whether `decision_timeout_seconds` is long enough in
practice that this race is rare, or short enough that it's a near-certainty
on any real restart attempt — this depends on how the timeout is configured
per-task/pipeline and wasn't checked here.

**Reducing this to zero, not just "unlikely" — the actual prerequisite plan.**
Fixing the field-name mismatch alone only makes killing the ghost *likely* —
`StopExecution` itself can still fail for unrelated reasons (throttling,
transient API unavailability), and nothing in the wrapper currently guards
against a stale write applying anyway. Confirmed: **zero** states in
`run_task/sfn.tpl.json` use a `ConditionExpression` — every status-writing
`updateItem` (`Save_Success`, `Save_Failed`, `Save_Error_Waiting`, etc.) is
unconditional. A ghost that eventually reaches `Save_Failed` would blindly
overwrite whatever the new, restarted attempt has written by then — including
flipping a since-succeeded task's status back to `failed` — this is data
corruption, not just a confusing History entry.

Two independent layers, not one:
1. **Fix the field-name mismatch** (`helper_arn` → what `Stop_Old_Wrapper`
   reads, or vice versa) — makes ghost-killing work as originally intended.
   Hygiene, reduces how often layer 2 needs to matter.
2. **Add a `ConditionExpression` keyed on `attempt`** to every status-writing
   state in the wrapper, so a write only applies if the record's current
   `attempt` still matches the one this specific execution started with. If a
   restart has since happened (`attempt` incremented), a stale write is
   rejected by DynamoDB itself — regardless of whether layer 1 worked,
   regardless of any future reason a ghost might survive. This is what
   actually gets the risk to zero, structurally, rather than "less likely."

Layer 2 is the one that matters for correctness; layer 1 just makes it rarer
that layer 2 has to do its job. Both are small, mechanical, single-attribute
changes — no new states, no new tables, no change to `xcom.pull()` or
`notify_dependents_via_sfn`'s contract. Recommended as the prerequisite pair
before §9's "Restart directly from `waiting_decision`" ships.

**Checked against existing convention first (Principle #1) — a real pattern
exists, but for a different purpose, and doesn't cover this case.**
`notify_dependents/sfn.tpl.json` already uses a `ConditionExpression` —
`NOT (#s IN (:success, :failed, :skipped, :aborted, :upstream_failed))` — when
marking a *subscriber* task `waiting_paused`, guarding against clobbering a
subscriber that's independently already reached a terminal status in the
meantime. Structurally similar to what's proposed here, but status-based, not
attempt-based — and that distinction matters: a stale ghost write landing
*while* the new attempt is still `running` (not yet terminal) would **not** be
blocked by a status-only check like this one, since `running` isn't in the
excluded set. The new attempt needs protection for its entire lifetime, not
just from the moment it reaches its own terminal state — which is why
attempt-based, not status-based, is the right guard for this specific case,
not a stylistic preference over the existing convention.

## 9.5. A real bug caught through live testing, after shipping — restart must stop TWO executions, not one

**This is a correction to §9 above, found only after Mike tested Restart
live** — the field-name fix and `ConditionExpression` guard described in §9
were both real and necessary, but §9's diagnosis of *which* field
`Stop_Old_Wrapper` should read was incomplete, and shipping it caused a
genuine, live bug.

There are two separate executions involved in running any task: the OUTER
`dependency_wrapper` (waits for dependencies, then synchronously calls the
INNER `run_task` via `startExecution.sync:2`) and the inner `run_task`
itself (the one with `Wait_For_Decision`/`waiting_decision`, which §9's
`ConditionExpression` guards protect). §9 found `Stop_Old_Wrapper` reading
`wrapper_execution_arn`, confirmed that field is never written anywhere in
`run_task`'s own template, and concluded it must be a typo for
`run_task_helper_arn` — reasoning that was correct as far as it went, but
incomplete: it only checked `run_task`'s template. `wrapper_execution_arn`
**is** genuinely written — by `registration_helper`'s `Save_Task_Record`,
for the *outer* dependency_wrapper specifically (`Prepare_Inputs` sets it to
`$states.context.Execution.Id`, evaluated in the outer wrapper's own
context). Both fields are real; they refer to different executions.

Switching `Stop_Old_Wrapper` to kill only the inner `run_task` (leaving the
outer wrapper alive, still synchronously waiting on it) caused a live,
reproducible bug: the outer wrapper's `Run_Task` call sees its child
forcibly aborted — which *is* an internal error from the outer wrapper's own
perspective, so its own `Catch` fires, cascading through `Handle_Failure` →
`failure_handler` → `notify_dependents('failed')`, **immediately**, well
before the new (restarted) attempt's own work even begins.

Mike caught this by testing end to end: clicking Restart marked the
downstream task `upstream_failed` within seconds, while the restarted
task's own underlying business logic was still genuinely running — and
later genuinely succeeded (confirmed via the underlying SFN's own AWS
execution history, ~30s duration, "Succeeded") — a false failure
notification racing ahead of, and contradicting, the real outcome. This is
exactly the "статуси" (status confusion) Mike suspected and asked to have
checked precisely against the code, not guessed at.

**Corrected fix:** split into two states, in a specific order —
`Stop_Old_Outer_Wrapper` (kills the outer wrapper **first**, via
`wrapper_execution_arn` — an *external* `StopExecution` call does not
trigger a wrapper's own internal `Catch` block, which only fires for errors
*from within* an execution, not from being stopped externally, so this
cannot cascade a false notification) then `Stop_Old_Inner_Wrapper` (kills
the inner `run_task` **second**, via `run_task_helper_arn` — safe now that
the outer wrapper, which would otherwise catch this as a failure, is
already dead and cannot react to it). Order matters: reversing it recreates
the exact bug.

Re-verified with a mutation test: reverting `Stop_Old_Outer_Wrapper` back to
`run_task_helper_arn` (the incomplete §9 fix) was confirmed to make the
corresponding test fail, not just pass with the correction in place.

## 9.6. Two follow-ups after live testing confirmed §9.5's fix worked

**task_config/outlets silently lost on restart.** Flagged earlier (§9's
original investigation of `Start_New_Wrapper`) but not fixed at the time.
Both are deploy-time DAG properties, never stored on the per-execution
DynamoDB record — `Start_New_Wrapper` tried to reconstruct them from
`item.task_config.S`/`item.outlets.S`, fields that never existed, always
silently yielding `{}`/`[]`. Fixed properly: `restart_task`'s Python handler
now looks these up from the pipeline registry (via a new shared
`_lookup_dag_node`, also used by §7c's asset-event lookup) and passes them
explicitly in `restart_task_helper`'s input. `task_config` construction was
also extracted into `_build_task_config_and_arn` (shared between the
original dispatch and the DAG builder) and is now stored in the registry's
`dag_metadata` too — closing the gap at the source, not just working around
it. ASL snapshots regenerated; diffed to confirm the only change was the
new `task_config` field appearing, nothing else.

**`restart-` name prefix.** Requested directly: distinguish a restarted
attempt's underlying business SFN invocation from the original in the AWS
console, without checking duration/timestamps. Only `Run_Task_SFN` needed
this — it's the only `Run_Task_*` variant that names a child Step Function
execution; the others (Lambda, Glue, ECS, Athena, EMR, Batch) call AWS
services directly and don't have an equivalent controllable name. Also
retired the dead `is_restart` flag `Start_New_Wrapper` set (never read
anywhere) — `attempt > 1` already captures the same thing, more precisely,
and is now what the prefix condition uses.

## 10. Direct answers to the two questions this thread started with, given §9

**"Will the History tab record that a restart happened?"** Yes, for the *new*
attempt specifically — `Start_New_Wrapper` increments `attempt` correctly
(§2), and the new wrapper's own `Emit_Task_Started`/etc. events log normally
under the same stable `task_run_id`, so "WRAPPER STARTED (attempt 2)" and
onward should appear correctly. **But**, given §9's finding: if the *old*,
never-actually-killed wrapper is still ghost-running, `record_manual_decision`'s
own comment confirms it *also* records a `TASK_FINISHED` event when it
eventually times out — meaning the History tab could show a confusing,
out-of-order event arriving *after* the new attempt's own events, whenever the
old `decision_timeout_seconds` happens to naturally elapse. Not confirmed
against a live restart in this spike (same caveat as §2) — but §9 gives a
concrete reason to expect it, not just a theoretical one.

**"Will XCom work the same as the first attempt?"** Yes, unaffected by any of
§9's findings — confirmed in §3, XCom is keyed by `pipeline#task#date`, entirely
independent of `execution_name`/attempt number, and neither `Stop_Old_Wrapper`'s
bug nor the ghost execution touches that key at all.

## Final status — implemented, following explicit sign-off on this plan

All six pieces below were implemented, tested (including new,
purpose-written tests for each), and mutation-verified — a mutation was
introduced for every new safety property (the field-name fix, the
ConditionExpression guard, the catch-ordering, the RESTARTABLE_STATUSES
extension, the synthetic-marker's never-overwrite guard, the asset-event
gating) and confirmed the corresponding test actually fails without the fix,
not just that it passes with it.

- **§6 (task_input written only on success)** — fixed. New `Save_Task_Input`
  state, `run_task/sfn.tpl.json`.
- **§7a (`xcom.pull()` crashes on a manually-resolved dependency)** — fixed.
  All four manual actions write a synthetic marker to the canonical output
  key; `xcom.pull()`'s own contract (raise on missing) is unchanged.
- **§7c (asset events skipped on manual resolution)** — fixed for the
  push-model SFN notification (already fully wired in the SAM template);
  the pull-based subscriber Lambda notification was deliberately not added
  (would need a new IAM permission/env var — a real infra change, separate
  decision).
- **§9 foundation, part 1 (field-name mismatch) — corrected in §9.5, after a
  live bug caught by testing.** The templates involve TWO separate
  executions, not one: the outer `dependency_wrapper` (`wrapper_execution_arn`)
  and the inner `run_task` (`run_task_helper_arn`). An initial fix switched
  from the former to the latter, reasoning the former was a typo — that
  reasoning was incomplete (it only checked `run_task`'s own template, not
  `registration_helper`'s, where `wrapper_execution_arn` is genuinely
  written for the outer wrapper) — and shipping it caused a real,
  reproducible bug: killing only the inner wrapper left the outer one
  alive, synchronously waiting on it, so its own `Catch` fired and
  cascaded a false "task failed" notification to downstream tasks,
  immediately, before the restarted attempt's own work even began. §9.5
  documents the live catch and the corrected fix: stop the outer wrapper
  first (external `StopExecution` can't cascade its own `Catch`), then the
  inner one second.
- **§9 foundation, part 2 (attempt-keyed `ConditionExpression`)** — added to
  `Save_Success`/`Save_Failed`/`Save_Error_Waiting`, with a new
  `Stale_Attempt_Superseded` state for the rejected-write case. This is what
  makes the "duplicate status" risk structurally zero, not just less likely.
- **§9 final step — `RESTARTABLE_STATUSES` extended to `waiting_decision`**
  — Restart now works directly, without Stop first. Found and fixed a
  related drift bug while wiring this up: the fallback (no-SFN-helper)
  restart path had its own, separately hand-maintained `RESTART_CONDITION`
  string, missing `waiting_decision` — now derived from
  `RESTARTABLE_STATUSES` directly instead.

**Not implemented, by design:** §7c's pull-based subscriber notification
(infra change, separate decision) and any further work on §7a's "should the
reconstruction be visually badged" question (§5) — neither was part of the
plan Mike approved for this pass.
