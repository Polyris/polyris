# ADR #130 — Coordination layer: one reconciler, facts as state, stream-driven hints

> **Status:** ACCEPTED (2026-11-12), revision 8 (2026-11-13: one owner per task row, by generation, §2.1; 2026-11-14: the Lambdas import the coordination package from the SDK, §10; 2026-11-14, after an external review: a push is judged as of its fact, §4, §6.1, Rule 55; a zombie's run is told, §3 step 3, ADR #135; invariant 10). Implemented on `feat/coordination-reconciler` for v1.2.0.
>
> The design went through five review rounds: break, architect, developer, QA, DevOps and reliability reviews. The implementation went through two more rounds of break, architect and reliability reviews. Every finding is resolved below or listed under *Out of scope*.
>
> **Maintainer decisions:**
>
> | Decision | Choice |
> |---|---|
> | D8 | Partition-aware `wait_for`, rule B: exact date for hourly/daily assets; current or previous week/month for weekly/monthly |
> | D11 | Stream-driven asset hints |
> | D12 | Push triggers on the same evaluator and the same rule: a run follows the cadence of its finest input |
> | D13 | Forward-fix only |
> | D15 | Maintenance-window deploy |
> | D16 | Attention index |
> | D20 | Version v1.2.0 |
> | D21 | No custom CloudWatch metrics |
> | D22 | Failure handling in code, in three levels, with no alarms |
> | D23 | Partition index on `asset-events` |
> | D39 | No drain-check script: the upgrade's drain is checked once, in the console's Tasks view |
> | D40 | One owner per task row, named by its generation (§2.1) |
> | D41 | No retry of the dependency wait on `States.Timeout`: `orchestration_timeout` is the real maximum |
>
> **Supersedes:** ADR #122 (registration fast-path verdicts, `assets_ready` flag); ADR #128 (direct-invoke wake-up from `mark_success`).
>
> **Amends:** ADR #57. Push consumers start with deterministic names. `cascade='none'` now *records* the asset fact but triggers no push consumer (§1).
>
> **Unchanged:** trigger-rule semantics (ADR #115, #117). They move into one implementation.
>
> **Related:** an AND-gate design numbered #129 was drafted on an unmerged branch and never reached `main`. See *Rejected alternatives*.

## Context

A task waits in `dependency_wrapper` → `Wait_For_Dependencies` on one task token until its task dependencies and `wait_for` assets are satisfied. Asset-triggered pipelines (`schedule=[assets]`) start when their assets materialise.

Every hang and wrong wake in this layer since ADR #122 traces to five causes.

1. **Readiness is decided in six places, each with partial information.** Those places are:
   - the registration JSONata;
   - `check_assets`;
   - `notify_asset_subscribers`;
   - `evaluate_deps`;
   - the routing in `notify_dependents`;
   - `resume_execution`.

   They already disagree: the registration JSONata counts `success` but not `succeeded`; one path calls a verdict `blocked`, another `upstream_failed`; pause is ignored for tasks without dependencies.
2. **Readiness is accumulated from events, not evaluated from facts.** Pull keeps an `assets_ready` flag; push keeps a per-day counter and lock. An event that is never credited (lost, landed before its subscription, or arrived during a pause) leaves the state wrong for good.
3. **Every writer of a fact must notify every consumer.** `mark_success` left `wait_for` waiters asleep until ADR #128 added a second call.
4. **There is no safety net, so primary paths fail open:** they wake a task when unsure.
5. **Reads on the decision path are eventually consistent.**

**Defects fixed by this ADR:**

| Id | Defect |
|---|---|
| B-P1 | Asset arrives during a pause → subscription deleted, status stays `waiting` → resume ignores it → hang |
| B-P2 | Task deps finish during a pause, assets missing → resume runs the task without its assets |
| B-P3 | A task saves its pause token while resume runs (resume finds the run's rows through an index), or its release fails → resume misses it → it waits for its 12-hour pause heartbeat |
| B-P4 | `pause_waiter` cannot save the pause token → nothing can release the task → after its 12-hour heartbeat it runs, paused or not |
| B-O1 | An operator resolves a waiting task, but the console cannot stop its wrapper → when the wrapper's dependency wait times out it registers again, the unconditional write puts the row back to `waiting`, and the task runs |
| B-O2 | A restart replaces a wrapper the console could not stop → the old wrapper registers again and takes the row back, and the task runs twice |
| B-O3 | An operator decides a task whose pre-upgrade wrapper waits → registration wakes that wrapper with `deps_ready`, and having no claim it runs the task at once |
| B-O4 | An operator decides a task whose wrapper is past its wait and cannot be stopped → `run_task` writes `running` over the decision and runs the task |
| B-O5 | An operator decides a task held by a pause → its pause wait, retried at the heartbeat, writes `waiting_paused` back, and the task can run |
| B-W1 | A wrapper's claim is applied but its answer is lost (a 5xx, a timeout) → the retry fails its own condition, the wrapper ends as superseded, and the task never runs |
| B-U2 | A consumer is deployed after facts were recorded → the hourly catch-up starts it for those older facts |
| B-U3 | A subscription outlives what made it (an asset dropped from the schedule, a destroyed pipeline) → facts keep starting the pipeline, or write a warning every hour |
| B-R2 | A restart keeps the old finished status until the new wrapper registers → a dependant restarted meanwhile runs on the old success, or is blocked again by the old failure |
| B-F2 | One fact that no reader can place on a partition → every task waiting on its asset fails its decision |
| B-M2 | Mark Successful cannot record a fact → it logs and reports plain success; the tasks waiting on the asset hang with no trace in the console |
| B-R1 | `restart_task` passes neither `wait_for` nor the other starter fields → the restarted task is not gated on assets |
| B-N1 | Warning rows are dated by partition and grouped per run → warnings for older dates never reach the bell; one warning hides another |
| B-S1 | `Auto_Skip_Register` swallows a failed status write and still notifies dependants |
| B-G2 | The reconciler times out or crashes during registration → the task fails although its row is saved for the sweeper |
| B-G1 | A failed registration SFN never signals the wrapper → silent hang until `orchestration_timeout` |
| B-X1 | Run-name collisions: a run's tasks inherit the last 8 characters of its execution name. Asset-AND runs end in the constant `-asset-and`, and backfill runs share `-bf-{id8}` across tiers. Two consumers with the same task id on the same date then collide (`ExecutionAlreadyExists`) |
| B-M1 | `mark_success` dates its asset fact from the request (or today), not from the partition of the run being marked |
| B-A1 | `wait_for=[a, b]` wakes on the first event |
| B-U1 | Push AND loses the day when `Trigger_AND` fails after the counter completes |
| B-K1 | Subscription keys collide across runs and pipelines |
| B-T1 | A second signal overwrites `running` |
| B-A2 | Mixed `[a, b \| c]` fires on first arrival |
| B-A3 | Early AND members are not re-checked for freshness |
| B-C1 | A backfill passes on today's event |
| B-F1 | A consumer-notification failure fails the producer task |

## Decision

### 1. Facts are the only state

Readiness is evaluated on demand from three facts:
- **task rows** (`pipeline-tokens`: `status`, `wait_token`, `started_at`);
- **asset events** (`asset-events`);
- **the pause flag** (`_pause_{pipeline_execution}`).

No readiness flag, set, counter or lock is written.

**Asset fact rows.** Writers:
- the `run_task` template;
- one helper, `asset_events_repo.write_fact` (console DAL), used by `mark_success` and by every other fact writer outside `run_task`.

A fact is keyed by `(asset_name, event_time)`. Both writers put it with `attribute_not_exists(event_time)` and retry a collision with a later time, so two facts of one asset in the same millisecond never overwrite each other: an overwrite would lose a fact and, as a `MODIFY`, never reach the INSERT-only stream mapping.

Each row records:

| Field | Meaning |
|---|---|
| `event_time` | write time, `YYYY-MM-DDTHH:MM:SS.sssZ`; readers accept the legacy `+00:00` form |
| `partition_key` | the run's partition, raw (`YYYY-MM-DD`, `YYYY-MM-DDTHH`, `YYYY-Www`, `YYYY-MM`) |
| `partition_date` | normalised date of the partition: the date itself, the date of an hour, the Monday that starts an ISO week, the first day of a month. Start-of-period dates make a backfill keyed `YYYY-MM` or `YYYY-Www` line up with a scheduled run keyed by its first day |
| `fact_version` | `2`. Set by every new writer. Facts written by older code never have it |
| `granularity` | the outlet's declared granularity (ADR #50), copied from the outlet metadata the writer already has |
| `cascade` | `auto`, `none` or `all` |
| `source_dag` | the producing pipeline |

The partition of a fact is the **partition of the run that produced it**:
- for `run_task`, the run's `date`;
- for `mark_success`, the marked task row's `date`. This fixes B-M1.

So is its cascade: `none` or `all` when a backfill started the run with `_suppress_asset_event` or `cascade_all`, `auto` otherwise. `run_task` reads the flags from its input; Mark Successful reads them from the row's `starter_input` (`records.cascade_of`; a row from before 1.2 has none and gets `auto`).

The write is conditional (`attribute_not_exists(event_time)`, retried with a fresh timestamp), so a fact is never overwritten.

`cascade='none'` (ADR #57) now records the fact but starts no push consumer. Pull waiters for that partition are woken, because the data exists. `tests/sdk/test_run_task_facts.py` checks that `run_task`'s JSONata writes the fact the helper writes, and `tests/sdk/test_asset_fact_writers_wiring.py` that both writers reach the same table and keep facts equally long.

### 2. Status ownership

`RECONCILABLE_STATUSES = {waiting, waiting_paused}` is a generated constant. Each transition has exactly one owner:

| Transition | Owner |
|---|---|
| → `waiting` (row created) | registration `Save_Task_Record` |
| `waiting` → `waiting_paused` (dependencies satisfied, pipeline paused) | reconciler (§3) |
| `waiting`/`waiting_paused` → `deps_ready`, `skipped` (+ `skip_origin='rule'`, `skip_reason`, `finished_at`), `upstream_failed` | **the wrapper**, after it receives the signal (§6.4) |
| → `failed` (task failures, including coordination errors) | wrapper → failure handler, which never overwrites `upstream_failed` |
| `waiting` or `waiting_paused` → `aborted` (zombie: wrapper no longer running), or `waiting` → `skipped` (a pre-upgrade wrapper consumed a skip signal) | reconciler (§3, step 3) |
| `waiting_delay`, `waiting_decision`, `running`, terminal statuses, `pause_token` rows | wrapper / `run_task` / `pause_waiter` / console actions, as today |

The party that **receives** a wake writes the status that follows it. A signal and its status therefore cannot be split by a crash, and the reconciler never has to tell a token it consumed from one that expired.

### 2.1 One owner per task row: the generation (D40)

The table above says who may write each transition. It does not say for which owner. A wrapper keeps running after an operator decided its task, after a restart replaced it, and across the upgrade that changed its helpers, and not every writer checked that it still owned the row. Six defects came from this one gap: B-O1, B-W1, and four found in the second review round (B-O2…B-O5). Principle #42 applies, so the gap is closed in the model, not site by site.

**The rule.** A task row carries `generation`, a number.
- **An owner holds one generation.** The wrapper takes it from its input: `1` for a new run, the value the restart helper hands over for a restart. It passes it to everything it starts: registration, `run_task`, the pause wait and the failure handler.
- **Every write an owner makes is fenced** with `(generation = :g OR attribute_not_exists(generation))`. A row without the attribute was written before the upgrade (*Old rows* below).
- **Ending an ownership raises it** with `SET generation = if_not_exists(generation, :zero) + :one`, in the same write that records the decision. Writers that do this: the operator's decisions (skip, fail, success, stop, stopping a run), the restart helper, and the reconciler's closes (a zombie's abort, a pre-upgrade wrapper's skip). The restart helper keeps the row and hands the new value to the next wrapper; it no longer deletes the row. In the same write it sets the status to `stopped`, which dependants count as pending, so the old finished status never serves a dependant until the new wrapper registers (B-R2); and it changes only a row that exists.
- **A fenced write ends its writer quietly:** no status, no notification, no work.
  - The wrapper ends as `Superseded`.
  - `run_task` ends at `Stale_Attempt_Superseded`.
  - The pause wait fails its token with `Superseded`.
  - The failure handler skips its notifications when it can read the row's generation. If that read fails, its fenced write still changes nothing, but it notifies and fails the wrapper's token as before; the read's retries make this rare.
  - Registration answers the wrapper with `Superseded`.

**Every writer of a task row**, and the guard it uses:

| Writer | Kind | Guard |
|---|---|---|
| Registration `Save_Task_Record`; wrapper `Auto_Skip_Register`, `Claim_*`, `Update_Status_Waiting_Delay`; every `run_task` write of the row; `Save_Pause_Token`; failure handler `Update_Status_Failed` | acts for an owner | the fence (`run_task` also keeps its `attempt` check, ADR #125's guard against a restart's ghost; it goes with the other upgrade code in 1.3) |
| Console skip, fail, success, stop and run stop; the restart helper; reconciler `close_aborted`, `close_skipped` | ends an ownership | raises the generation |
| Reconciler `park`, `mark_due`, `defer`, `clear_attention`, pause checks, adoption | decides from what it read | `wait_token`, `started_at`, status, `pause_token`: values that change with every owner |
| Config edits, and the output, input, warning and pause rows | writes no status | none |

A contract test lists every write to a task row in the templates and the code, and fails on one that fits no row of this table.

**Claims** need no wrapper ARN any more: `(generation = :g OR attribute_not_exists(generation)) AND #s IN (:waiting, :paused, :status)`. A row in the target status under the owner's own generation can only be that owner's earlier claim. So a retry after a lost answer passes (B-W1) without `claimed_by`.

**Registration** writes the row when it is new, or when it holds the owner's generation (or none, from before the upgrade) and either another wrapper wrote it (a restart) or it still waits: `attribute_not_exists(execution_name) OR ((generation = :g OR attribute_not_exists(generation)) AND (wrapper_execution_arn <> :self OR #s IN (:waiting, :paused)))`. The same wrapper's save, retried after a lost answer, so never resets a task that started meanwhile.

**One timeout (D41).** The wait for dependencies is no longer retried on `States.Timeout`, so `orchestration_timeout` is the real maximum and a wrapper registers once. The liveness deadline becomes one timeout plus an hour. Rows without a generation keep three timeouts, because their pre-upgrade wrappers still retry.

**Old rows.** Rows and wrappers from before the upgrade have no generation; an owner input without one counts as `0`. `attribute_not_exists(generation)` lets them work as before until an ownership change gives the row a generation. From then on their writes are fenced. A pre-upgrade wrapper answered with `Superseded` fails, and the failure handler it starts finds the row raised and stays quiet.

### 3. The reconciler

`reconcile` is the only code that decides whether a waiting task is ready, and the only code that sends its dependency signal. It is a Lambda handler and is also importable in-process (the sweeper uses it that way).

**Entry points:**

| Payload | Called by |
|---|---|
| `{execution_name, source, known_events?}` | registration, `notify_dependents`, AssetFanout |
| `{pipeline_execution, source: "resume"}` | resume; operators |

**Reads:**
- **Task facts** come from base tables with `ConsistentRead=True`: the task row, the dependency rows (status and `skip_origin` in one `BatchGetItem`, with no error swallowing), and the pause flag.
- **Asset facts** come through `asset_conditions` (§4).
- **GSIs are used only to find candidates** (attention index, `pipeline-execution-index` for resume) and for asset-partition lookups (§4). They are never used to read the task facts a decision rests on.

**Decision.** `decide(row, deps, assets, paused) → Decision` is a pure function. `Decision` carries the verdict and the reasons.

| Row | Facts | Verdict |
|---|---|---|
| missing, status not reconcilable, or holds `pause_token` | — | `noop` |
| reconcilable | the trigger rule cannot be satisfied | `skip` / `upstream_failed` (ADR #115) |
| reconcilable | deps pending or assets not ready | `wait` |
| reconcilable | satisfied, pipeline paused | `paused` |
| reconcilable | satisfied, not paused | `ready` |

**Actions.** `T` is the `wait_token` read in this invocation; `S` is the row's `started_at`.

1. **`ready` / `skip` / `upstream_failed`:** `SendTaskSuccess(T)` with the payload the wrapper understands (`deps_ready`, `deps_skip`, `deps_blocked`). If it is accepted, the reconciler deletes the run's subscriptions (best effort; leftovers are harmless `noop`s and expire by TTL). **It writes no status**: the wrapper does (§6.4).
2. **`paused`:** `SET status = waiting_paused IF wait_token = :T AND started_at = :S AND status = :waiting AND attribute_not_exists(pause_token)`.
3. **Dead token** (`TaskTimedOut`, `InvalidToken`, `TaskDoesNotExist`). The reconciler checks the row's `wrapper_execution_arn`.
   - If that execution is still running, the token was consumed by a concurrent wake: do nothing.
   - If it `SUCCEEDED` and the verdict was `skip`, a pre-upgrade wrapper consumed the signal and expected the sender to write the status: write `skipped` (with `skip_origin='rule'`), conditional on `T` and `S`.
   - If it does not exist and the row registered less than 2 minutes ago, do nothing: DescribeExecution is eventually consistent, so a new wrapper may not show yet (`sweep.EXECUTION_VISIBLE_AFTER`). The sweeper checks the row again.
   - Otherwise, if it is not running and the row still holds `T` and `S`, the row is a zombie (for example, a wrapper stopped outside polyris). Set `status = aborted` with an explanatory `error`, remove it from the attention index, and write a warning. Then fail the run's token (`orchestration_token`): no wrapper is left to tell the run, which otherwise stays `RUNNING` (ADR #135). The outcome is loud, not silent.
   - Zombies are also found without a signal. A row past its liveness deadline (`started_at` + `orchestration_timeout` + 1 h, or three timeouts for a wrapper started before the upgrade, which still retries its wait) gets the same wrapper check from the sweeper.
4. **Transient error** (throttling, 5xx, timeouts, and a read of an index that is still being built — DynamoDB's `ValidationException` "Cannot read from backfilling global secondary index", seen when an upgrade adds an index): **raise**. Nothing is written. The caller's retry or the sweeper repeats the call.
5. **Non-transient error** (AccessDenied, missing resource, validation, a malformed spec, an unexpected exception in `decide`/`asset_conditions`): `SendTaskFailure(T, error="CoordinationError", cause=<exception>)`. The error name is always `CoordinationError`, never a forwarded `States.*` name, so the wrapper's `Retry` (which matches `States.Timeout`) cannot turn it into a re-registration. A failed condition check is never an error: another owner already acted, so it counts as a `noop`. The wrapper's existing `Catch` → failure handler marks the task `failed` with the real cause, visible in the console and the bell (D22, level 2). If `SendTaskFailure` itself is denied, write a `coordination` warning; it reaches the bell (§9).
6. **`noop`:** if the status is not reconcilable, remove the row from the attention index, `IF status = :read_status AND started_at = :S`. Pinning to `started_at` means a re-registered row (new `started_at`) is never touched. The update uses `attribute_exists(execution_name)`, so it never creates a phantom row.
7. **Fail closed.** No read failure, partial result or unknown state leads to a success signal.

**Output.** `{execution_name, source, verdict, reasons, deps, assets, action, latency_ms}`, plus `enabled_at` for a task it signalled (§9), logged as one structured line and returned.

### 4. Asset conditions

`asset_conditions.evaluate(spec, *, target, now, reader, known_facts=(), recorded_by=None) → Evaluation` is the only implementation of `wait_for` and `schedule=[…]`. `target` is the date D below, `reader` is a DAL protocol, and `known_facts` carries the fact that triggered the call. A push passes its fact's `event_time` as `recorded_by`: facts recorded after it do not count (§6.1, Rule 55).

`D` is `partition_date(row.date)`, normalised exactly like fact rows (§1). This makes non-daily backfill keys (`YYYY-Www`, `YYYY-MM`, `YYYY-MM-DDTHH`) well defined.

**Rule B (D8, revised).** Calendar periods, not day counts. Fixed day counts leave gaps for irregular schedules: "first Monday of the month" has 35 days between runs.

| Asset granularity | A fact of `a` satisfies date D when |
|---|---|
| hourly, daily | `partition_date = D` (exact) |
| weekly | `partition_date ≤ D`, and its ISO week is D's week or the week before |
| monthly | `partition_date ≤ D`, and its month is D's month or the month before |

Among the facts that satisfy D, the one with the latest `partition_date` is used. This is as-of semantics: the freshest version that existed for D. Weekly and monthly assets act as reference data for daily consumers.

| Condition | Ready when |
|---|---|
| `a` | a fact of `a` satisfies D (table above) |
| `a.within(hours=N)` | the fact with the latest `partition_date ≤ D` has `event_time ≥ now − N h`. A backfill of an older partition never refreshes it |
| `a.consecutive(days=N)` | facts exist for each of the N dates ending at D |
| `[x, y]` / `x \| y` | AND / OR, at any nesting depth |

**Lookup (D23).** A GSI `asset-partition-index` on `asset-events` (PK `asset_name`, SK `partition_date`) serves every condition:
- `a` and `.within()` query `partition_date ≤ D` in descending order, then apply the period rule. All facts of the latest qualifying `partition_date` are read, with pagination, so several facts on one date are compared by `event_time`. No fixed day bound is needed.
- `consecutive` queries `partition_date BETWEEN D − N + 1 AND D`.

The index is eventually consistent. To close the lag window, the reader also runs one consistent base-table query per asset in the spec for facts written in the last 15 minutes (`event_time ≥ now − 15 min`) and merges the results. This covers two outlets of one producer that arrive in different stream batches, and a consumer that registers just after a fact was written. `known_facts` still carries the triggering fact.

**Granularity** is taken from the fact row. If the row has none (legacy rows), the evaluator uses the producer's registry entry via `source_dag`; failing that, `daily`.

**Unreadable facts.** A fact the reader cannot place on the date line counts for no task. Example: a row a version before this ADR recorded for a run date that is no partition key, such as `latest`. The reader writes one `coordination` warning for the fact and goes on, so one bad row never fails the decision of every task waiting on its asset (B-F2).

**Push specs** come from the consumer's registry entry, which has the full nesting and `.within()`. `asset-subscriptions` is only the index of who to evaluate.

### 5. Subscriptions are per-run pointers

| Kind | `dependency_key` | `subscriber` |
|---|---|---|
| task dependency | `{upstream}-{pipeline_execution_short}` | `execution_name` |
| `wait_for` | `asset:{name}` | `execution_name` |

- No `wait_token` on subscription rows; the token lives on the task row only.
- Subscription queries on the decision path are consistent, including in `query_subscriptions`.

### 6. Hints

Hints only say "re-check". Losing one delays a task; it never makes a decision wrong.

#### 6.1 Asset events → AssetFanout

A DynamoDB Stream on `AssetEventsTable` (`NEW_IMAGE`) feeds an event source mapping with these settings:

| Setting | Value |
|---|---|
| `StartingPosition` | `TRIM_HORIZON` |
| `FilterCriteria` | `INSERT` only |
| `BatchSize` | 10 |
| `MaximumRetryAttempts` | -1 |
| `MaximumRecordAgeInSeconds` | -1 (records live until the stream's 24 h retention) |
| `BisectBatchOnFunctionError` | true |
| `FunctionResponseTypes` | `ReportBatchItemFailures` |
| on-failure destination | **none** (D22) |

**Per-record work, with a time budget.** Records are processed one at a time, and so is the work inside a record. Records not reached within the budget are reported as batch-item failures and redelivered; the work is idempotent. Inside a record the work stops with 5 s of the invocation left: the waiters not reached are lost hints, which the sweeper's checks recover, and the push consumers not reached wait for the hourly catch-up. One record with more waiters than fit in an invocation therefore never times the function out and blocks its shard. Push runs before pull: a record's consumers are few and only the hourly catch-up replays a missed one, while its waiters may be many and the sweeper re-checks each. A fact without `fact_version = 2` was written by an older version, which started its own consumers; the stream leaves it to adoption (§8 step 5).
- **Pull:** query the `asset:{name}` subscriptions (consistent) and call `reconcile(subscriber, known_events=[record])` for each, one at a time.
- **Push** (unless `cascade = none`):
  - for each `asset-subscriptions` row, evaluate the consumer's registry spec for **the event's own date**, D = `partition_date`, with the same rule as pull (§4). A run therefore follows the cadence of the finest input: a daily `sales` event starts that day's run with the latest weekly `prices` (D12). If a coarse input was missing for two or more periods, the dates it blocked are not started later automatically; recovering them is a backfill. A push spec is evaluated as it stood when the triggering fact arrived: a `.within()` as of the fact's `event_time`, not the time of evaluation, and no fact recorded after it counts (`asset_conditions.evaluate(recorded_by=)`). A retry, a late record or a catch-up replay therefore gives the same answer as the first evaluation; counting a later fact let the catch-up start a run that a `cascade='none'` backfill had completed (Rule 55);
  - a consumer reacts only to facts recorded after it subscribed: a row whose `subscribed_at` is later than the fact's `event_time` is skipped. `subscribed_at` is written once, at the consumer's first registration (`if_not_exists`), so the registration that every run repeats does not move it. A row an older SDK wrote has no `subscribed_at`, and the `registered_at` such a pipeline rewrites on every run would move; the pipeline's last deploy, which `polyris-deploy` stamps in the registry (`registered_at`), is the cutover instead (B-U2);
  - a subscription its pipeline no longer has is deleted and starts nothing: the pipeline runs on a cron (registration writes `"{}"`), dropped the asset from its schedule, has no registry entry, or its state machine is gone. A destroyed pipeline keeps its registry entry, so its row goes the first time a fact would start it: the start fails, with one `push` warning. The delete is conditional on the `registered_at` it read, so a pipeline that registered again meanwhile keeps its row. A schedule that cannot be read, or none on a deployed pipeline (its registration failed), is only warned about: deleting the row would leave a pipeline that never runs again to re-register (B-U3);
  - if satisfied (or `cascade = all`), start the consumer with a deterministic name from the run-name helper (§6.6);
  - `ExecutionAlreadyExists` counts as done;
  - a permanent error (`StateMachineDoesNotExist`) drops the item and writes one warning;
  - a transient error writes a **push retry row** (§7).
- **Isolation.** A failure for one subscriber or consumer never fails the record. A record raises only if even the retry row cannot be written, and then the stream retries until the table is reachable again.

**Writers only write the fact.**
- `run_task` keeps `Record_Asset_Event` and drops its two notify states. A fact it cannot record becomes a `fact` warning that names the assets and the cause (`Warn_Facts_Not_Recorded`): the task's row is already `success`, so nothing else would show it. The run then ends `FAILED`, because it did not deliver its assets (maintainer decision, 2026-11-14: a loud failure over a quiet success with a warning).
- `mark_success` and every other writer outside `run_task` call `asset_events_repo.write_fact`. A fact that Mark Successful cannot write does not undo the action: it becomes a `fact` warning, and the response lists it in `failed_facts`. When the task's outlets cannot be read from the registry, the warning and `failed_facts` name `*`: no fact was recorded.

#### 6.2 Task completion → `notify_dependents`

- Per subscriber: one `reconcile` call, with `Retry` (full jitter).
- `Catch` logs and continues; the sweeper covers the task (§8). No retry row.
- The Map never fails.
- `run_task`'s `Notify_Dependents` step no longer fails a producer: its `Catch` writes a warning.
- The routing, signal and status states are deleted.

#### 6.3 Registration

1. `Save_Task_Record` additionally stores:
   - `starter_input` (the wrapper's full start input, JSON);
   - `orchestration_timeout`;
   - `attention = waiting#{h}`, where `h` is the first hex digit of `$hash(execution_name, "SHA-256")`;
   - `attention_at = now + 10 min` (first due time).

   Neither key is ever empty. Contract tests check the shard range and that JSONata and Python compute the same shard.

   A wrapper started before the upgrade can register again after its dependency wait timed out, and its input has neither `starter_input` nor `orchestration_timeout`. The row then stores `''` and `0`, which restart and the liveness deadline treat like an adopted row that lacks them (§8 step 5).

   The write is conditional on the owner's generation (§2.1): the row is new, or holds the generation this wrapper carries, or, for a row from before the upgrade, follows the B-O1 rule. When an operator decided the task meanwhile, or a restart replaced this wrapper, the write fails, the row is kept, and registration answers the wrapper `Superseded`: a current wrapper ends quietly, a pre-upgrade one fails into a failure handler that changes nothing (B-O1, B-O3).
2. **Fast path:** a task with no dependencies and no `wait_for` is signalled immediately. Pause is enforced at run start by `pause_waiter`.
3. **Everything else:** write all subscriptions in one Map, then call `reconcile(execution_name)` with `Retry`.
4. **Failures in the registration SFN are caught.**
   - **Transient** means Lambda throttling or service errors, the reconciler timing out or crashing (`Sandbox.Timedout`, `Lambda.Unknown`), or its own `CoordinationTransient`. Everything else is **non-transient** (B-G2).
   - **`Save_Task_Record` fails** after its `Retry`: there is no row for the sweeper to find, so the failure is always sent as `SendTaskFailure(wait_token, error="CoordinationError", cause)`. The failure handler then writes the `failed` row even when none exists yet: its update also sets the task's identity fields (`task_name`, `pipeline_execution`, `date`, ...) when they are missing, instead of being conditional on `attribute_exists`, so the failure appears in its run.
   - **Any later step fails non-transiently:** `SendTaskFailure(wait_token, error="CoordinationError", cause)`. The task fails loudly with the reason instead of hanging (B-G1).
   - **Any later step fails transiently** after `Retry`: registration ends quietly with `attention_at = now`. The row is already saved, so nothing is lost. The sweeper reconciles it within one run.

Deleted: `Check_Dependencies_And_Assets`, `Eval_Task_Deps`, `Route_Combined_Check`, the signal/status pairs, `Update_Status_Ready`, and the `check_assets` Lambda.

#### 6.4 Wrapper (owns post-wait status)

After `Wait_For_Dependencies` resumes:

| Signal | Wrapper action |
|---|---|
| `deps_ready` | `SET status = deps_ready REMOVE wait_token, attention, attention_at IF (generation = :g OR attribute_not_exists(generation)) AND status IN (:waiting, :waiting_paused, deps_ready)` |
| `deps_skip` | the same with `skipped`, `skip_origin='rule'`, `skip_reason` and `finished_at`, **before** notifying dependants |
| `deps_blocked` | the same with `upstream_failed`, its reason in `error`, and `finished_at`; then the failure handler as today, which leaves `upstream_failed` rows alone |

The generation fences the claim (§2.1), and accepting the target status makes a retry idempotent: when the write is applied but its answer is lost (a 5xx or a timeout), the state's `Retry` finds its own claim (B-W1).

If the condition fails, another owner has already moved the task. Example: an operator's skip or success, which raises the generation as it writes the status, before it stops the wrapper. The wrapper then ends quietly as *superseded*: it does not run the task and raises no failure. The same holds for every later write it or its `run_task` would make (B-O4, B-O5). A failure of the write itself, as opposed to its condition, fails the wrapper visibly.

The failure handler also maps `error.Error = 'UpstreamFailed'` to `upstream_failed`. A pre-upgrade wrapper that receives a block signal therefore still ends with the right status.

#### 6.5 Resume, restart, auto-skip

- **Resume:** clear the pause flag and send the `pause_token` callbacks as today, but drop the unconditional `deps_ready` write that followed them (`run_task` continues and writes `running` itself). Then call `reconcile({pipeline_execution, source: resume})`. That call finds the run's rows through `pipeline-execution-index` (candidates only) and checks them one at a time within a 10 s budget, so the console's call answers within the HTTP API's 30 s. The response reports how many rows were handled and how many were deferred. Deferred rows keep their attention entry with `attention_at` set to the run's resume time (`resumed_at` on the pause flag), so the next sweeper tick handles them; since their enabling time is no earlier than the resume (§9), they do not count as primary-path misses. A task that saves its pause token while resume runs, or whose release fails, is released by the sweeper (§8 step 2, B-P3); a release whose token is already dead counts as done.
- **Restart** replays the row's `starter_input`. A contract test checks that every starter of the wrapper (pipeline SFN, restart, backfill) passes every field the wrapper and `Save_Task_Record` read (B-R1).
- **`Auto_Skip_Register`** retries its write. If the write cannot be made, it fails the wrapper visibly and does not notify dependants (B-S1).

#### 6.6 Run names

One helper, `run_name(pipeline, date, kind, discriminator)`, names every non-scheduled pipeline run. The final 8 characters are a hash of `(pipeline, date, kind, discriminator)`, so the task-execution names derived from them are unique across pipelines, kinds and tiers. The whole name is ≤ 80 characters, truncating the pipeline name.

The derived wrapper name `{task_id}-{date}-{suffix}` must keep its suffix. When a task id is too long, the generator keeps a prefix of it plus a 6-character hash of the full id, and leaves `-{date}-{suffix}` intact. Generation fails if two task ids of one DAG would still produce the same name. This regenerates the ASL snapshots and takes effect when a pipeline is redeployed. Until then, long task ids keep today's behaviour.

| Run kind | Discriminator |
|---|---|
| push AND | none: one run per consumer and date, even after the spec changes |
| push OR / `cascade=all` | `(asset, event_time)` (one run per event) |
| backfill | backfill id + tier (B-X1) |

### 7. Indexes

**`attention-index`** on `pipeline-tokens` (D16):
- PK `attention`, SK `attention_at`.
- `attention_at` is a **due time**, not a creation time.
- Projection INCLUDE: `status`, `task_name`, `pipeline_name`, `pipeline_execution`, `task_date`, `kind`, `error`, `finished_at`, `started_at`, `attempts`.

| `attention` | Rows | Leaves the index |
|---|---|---|
| `waiting#{0-f}` | task rows that may need a wake | wrapper post-wait write; reconciler `noop`/zombie path; re-registration replaces the row |
| `paused#{0-f}` | task rows held in `run_task`'s pause wait, written by `pause_waiter` with the `pause_token` | `run_task` when it continues; the sweeper when the token is dead or the row no longer holds one |
| `retry#{0-f}` | **push** retry rows `_retry_push_{h(consumer, date or event)}` (pull needs none: the waiting row is already indexed) | deleted on success; exhausted after 24 h → warning; TTL 7 days |
| `warn#{0-f}` | coordination warnings (§9) | TTL 8 days |

**`asset-partition-index`** on `asset-events` (D23): PK `asset_name`, SK `partition_date`. Projection INCLUDE: `event_time`, `partition_key`, `granularity`, `cascade`, `source_dag`, `uri`.

Each table gets one GSI, so both fit the one-GSI-change-per-table limit and can ship in the same deploy.

### 8. Sweeper

An EventBridge Scheduler runs it at `rate(5 minutes)` with no scheduler retry. The function timeout is 240 s.

**Lease.** The `_sweeper_lease` row (`owner`, `expires_at = now + 270 s`) is taken with a conditional put that succeeds if the row is absent, expired or already its own, so a put resent after a lost answer passes. Only its owner releases it. A run that cannot take the lease exits normally; a sweeper that stops reporting is a health signal (§9).

**Each run, within a 200 s budget:**
1. **Due rows.** Query `waiting#0..f` for `attention_at ≤ now`, earliest due first. For each row:
   - Call `reconcile` in-process, one row at a time.
   - Unless it was woken, set `attention_at` to the next due time (backoff 10 → 20 → 30 min, then every 30 min) and increment `attempts`. The write is conditional on `started_at`.

   Due-time ordering means nothing starves. The backoff caps the work at 48 checks per waiting task per day.
2. **Pause releases.** Query `paused#0..f` for `attention_at ≤ now`. For each row, read the row and its run's pause flag (consistent):
   - the row no longer holds a `pause_token`, or its token is dead → drop its entry;
   - the run is still paused → due again, with the same backoff;
   - otherwise send the pause callback as resume does, and write a `sweeper` warning: resume missed the task.
3. **Push retries.** For `retry#0..f` rows that are due, retry with backoff (5 min → 1 h).
4. **Push catch-up,** hourly. For every subscribed asset, read the facts written in the last 48 hours (consistent base-table query) and replay AssetFanout's push branch for each of them: same date, same rule, same `cascade` handling, same deterministic names. Only facts with `fact_version = 2`, written by the new code, are replayed. Facts written by the old code were already handled by the old push path, so they are never replayed and nothing starts twice. A fact the replay cannot read is skipped with a log line: the stream warned about it when it was recorded. A consumer deployed after a fact is not started for it: the replay applies the `subscribed_at` rule of §6.1. A run that reaches its budget keeps its place, down to the last fact it replayed, and the next run goes on from there.
5. **Adoption.** When the adoption version (`sweep.ADOPTION_GENERATION`; unrelated to a task row's generation) differs from the one recorded in `_engine_health`, each run continues two resumable scans until both finish. Each scan then repeats for one more hour. A scan checks the budget before each row (Principle #52) and keeps the key of the last row it handled as its cursor. A fact it cannot read stays as it is, with a `coordination` warning (Principle #51). A release raises the generation only when it changes what a task row or a fact must carry, so an upgrade that keeps the row shapes scans nothing. The generation and the cursors are kept in `_engine_health`. Adopted rows that have no `orchestration_timeout` use the platform default for the liveness deadline. Adopted rows that have no `starter_input` restart the way they do today, with a warning.
   - **Task rows.** Every reconcilable row without an `attention` key gets one: `paused#` if it holds a `pause_token`, `waiting#` otherwise. Rows registered before or during the deploy are then reconciled from their facts, whatever pointer shape they were written with. The extra hour of repeats covers registrations that the old definitions wrote while the stack update was still running.
   - **Asset facts.** Every row without `partition_date` gets `partition_key`, `partition_date` and `granularity`, derived from `execution_date` and the producer's registry entry. It does **not** get `fact_version`. These writes are `MODIFY` records, which the INSERT-only stream filter ignores, so they trigger nothing.
6. **Engine self-check,** last:
   - event source mapping `Enabled`, and its last call to AssetFanout not failed (`LastProcessingResult`);
   - stream enabled;
   - the oldest overdue `waiting` or `paused` row;
   - push retry backlog;
   - the steps of this run that failed.

   The results go to the `_engine_health` row (§9). A failing step does not stop the run: the later steps and the self-check still run, and the next run tries the step again.

**Logging a primary-path miss.** When the sweeper wakes a task whose enabling fact is older than that task's previous due time, it writes a `sweeper` warning: the primary path missed it.

### 9. Failure handling and visibility (D21, D22)

**Three levels, with no extra infrastructure.** No custom metrics, no CloudWatch alarms, no SNS, no DLQ.

1. **Heals itself in code.** Retries, the sweeper, push retries and catch-up, idempotent signals and starts. No human is needed.
2. **Fails loudly in code.** Non-transient coordination errors fail the affected task with the real cause (§3 step 5; registration §6.3; a pause token `pause_waiter` cannot save fails the pause wait with `CoordinationError`, B-P4). Zombies are marked `aborted` with an explanation (§3 step 3), and their run is told (ADR #135). The user sees a failed task with its reason where they already look, and re-runs it after fixing the cause.
3. **Engine health is shown in the console.** This covers the case where no code runs at all (schedule removed, event source mapping disabled, stream disabled).
   - The sweeper writes `_engine_health`: last run, event source mapping state and last result, stream state, oldest overdue waiting row, push retry backlog, the steps that failed, and its adoption and catch-up progress. A reading it cannot take is listed with the failed steps, so the row is still written. A coordination error reaches the bell as a warning, not this row.
   - `/api/health` adds a `coordination` section. It also judges staleness itself: no sweeper run for 15 minutes means the engine is unhealthy.
   - The console shows a banner while the engine is unhealthy.

**Warnings.** One helper, `records.warning_item(*, kind, execution_name, task_name, pipeline_name, pipeline_execution, task_date, error, now)`, builds the row `_notify_warn_{kind}_{execution_name}`; SFN templates and generated ASL write the same shape, pinned by contract tests:
- `kind ∈ WarningKind`: `coordination`, `zombie`, `push`, `sweeper`, `notify`, `manual` for a manual action that kept a task's real output, and `fact` for asset facts a task or a manual action could not record;
- `attention = warn#{h}`, `attention_at = now`;
- TTL 8 days.

The bell reads warnings from `attention-index` by write time, deduplicated per execution and kind. Task failures are read as today.

**Logs and SLOs.** One structured line per `reconcile` call; retention via the existing `LogRetentionDays`.

`TimeToWake` is measured from the logs, during smoke and game-day only. It runs from the enabling time to an accepted signal. The enabling time is:
- for task dependencies, the latest required `finished_at` (for `one_*` rules, the earliest one that satisfies the rule);
- for assets, the newest required `event_time` (for OR, the earliest one that satisfies it);
- in both cases, no earlier than `started_at`, and no earlier than the resume time.

Targets: primary path p99 ≤ 30 s; through the sweeper ≤ 40 min.


### 10. Packaging

- `decide`, `asset_conditions`, `trigger_rules` and `partition_date` live in `polyris/coordination/`, under the 100% pure-logic coverage floor. `polyris/local.py` uses the same `trigger_rules`.
- The coordination Lambdas and the console import them from the polyris release their `requirements.txt` pins, as ADR #102 set up for the console (revision 7, 2026-11-14). A copy of the package in each Lambda kept them apart from that pin; it is no longer needed, because `main` moves only with a release, `scripts/release.sh` moves both pins with the version, and a deploy from source points both at the checkout. `tests/sdk/test_lambda_sdk_pins.py` checks that both pins name the repository's version and that the release script moves both, and the CI trial archive builds with the checkout's SDK and imports every function.
- One CodeUri, `sam/lambdas/coordination/`, holds three functions (`Reconcile`, `AssetFanout`, `Sweeper`), each with its own role. What the reconciler needs is one managed policy (`polyris-reconcile`) that every role running it attaches; each role adds only its own needs. A test records every call the code makes and compares it with the roles.

**Removed:**
- `EvaluateDepsFunction`, `CheckAssetsFunction`, `NotifyAssetSubscribersFunction`;
- `NotifyAssetConsumersSfn`, `QueuedAssetEventsTable`;
- their IAM, substitutions and env vars;
- the wake-up invoke in `mark_success`.

## Invariants

Each has a test (`tests/backend/coordination/README.md`).

1. **At most one effective wake per token.** A token is single-use, and the wrapper writes the status after it is consumed.
2. **No success signal without consistent reads of the task facts in the same invocation.** Asset facts come from the partition index plus `known_events`; their bounded staleness is covered by the sweeper.
3. **Status never moves backwards.** Each transition has one owner (§2). Every reconciler write is conditional on the token, the status and the `started_at` it read.
4. **Pointers are deleted only after an accepted signal.** Leftovers are `noop`s.
5. **Every reconcilable row without `pause_token` is in the attention index, and every row held in its pause wait is in `paused#`.** A row that leaves a reconcilable status leaves the index at once when the wrapper or `run_task` moves it, and otherwise at its next check (within 30 minutes), when the reconciler finds it settled. After a deploy, adoption completes.
6. **Facts are never overwritten.**
7. **Every asset-event INSERT is processed by AssetFanout.** Pull waiters are also covered by the sweeper, and push consumers by retry rows and the catch-up pass.
8. **Push runs at most once** per (consumer, date) for AND and per (consumer, event) for OR, and **at least once** within the catch-up window for each fact recorded after the consumer subscribed.
9. **A write made for an owner whose generation the row no longer holds changes nothing** (§2.1). Only the decision that raised the generation, or the next owner, moves the row.
10. **A run whose task rows have all finished has finished too** (ADR #135), unless the bell says it could not be told. A `stopped` task does not count as finished: its run may wait for a restart.

## Key design choices

- **The receiver writes the post-wake status.** "Signal, then the sender writes the status" breaks when the sender crashes in between, and a consumed token cannot be told from an expired one. The wrapper owns the transition, so neither problem exists.
- **One function decides and acts.** Six wake paths with different status semantics produced most of the defects.
- **Three failure levels instead of monitoring infrastructure (D22).** Recover automatically. Fail the affected task loudly when recovery is impossible. Show engine health where the user already looks. Polyris users have no on-call team; alarms would need setup, a reader and money.
- **Due-time sweeper.** Each waiting row carries its next check time. The sweeper does only what is due: no starvation, and the cost is bounded per task.
- **The fact table is the outbox.** The stream relays every write from any present or future writer.
- **Rule B with a partition index.** As-of semantics need a range read by partition date. An index answers it exactly, for every granularity and every backfill key format, without relying on write times or on validating every writer.
- **One run-name helper** makes the uniqueness of derived task names a property of the code, not of each caller.

## Consequences

**Behaviour:**
- `wait_for=[a, b]` waits for both.
- Mixed and nested specs are exact.
- Backfills wait for their own partition.
- Weekly and monthly assets satisfy daily consumers with their latest version from the current or the previous week/month.
- Pause, resume and restart never wake early and never lose a task.
- Registration failures fail the task with a reason.
- Push AND cannot lose a day.
- Producers never fail because of a consumer.

**User-visible changes** (CHANGELOG "Changed"):
- Plain `wait_for` is partition-aware (rule B). It applies at platform deploy, without a pipeline redeploy.
- `.within()` looks at the latest partition up to D.
- `cascade='none'` records the fact.
- `mark_success` dates facts by the task's partition.
- Push consumers whose inputs have different cadences run at the cadence of the finest input (for example daily, with the latest weekly input). Today they run only on dates when every input has a fact.

**Operations.** No new monitoring infrastructure. Engine health is shown in the console. Logs carry one line per `reconcile` call.

**Limits** (constants in the code):
- Work is sequential everywhere: the sweeper, AssetFanout and resume check one row at a time.
- The sweeper runs every 5 minutes for at most 200 s. A waiting task is checked 10, 20 and then every 30 minutes, so at most 48 times a day, and a lost hint delays it by at most that long.
- AssetFanout gets 60 s per invocation and keeps 5 s in reserve inside a record (§6.1). Resume checks a run's rows for 10 s; the rest are due at once for the sweeper.
- The stream keeps records for 24 hours. A push start that keeps failing is retried for 24 hours, then given up with a `push` warning. The push catch-up replays the facts of the last 48 hours, hourly.
- A waiting wrapper is checked for liveness after one `orchestration_timeout` plus an hour, or three timeouts for a wrapper started before the upgrade.

**Cost:**
- Fewer STANDARD-SFN transitions per registration and per producer run.
- No Express SFN per outlet.
- No push counters.
- Bounded sweeper work (≤ 48 checks per waiting task per day, in-process).
- Two GSIs (one extra write per task row change and per fact).
- No metrics or alarms.


## Migration

Forward-fix only (D13):

1. **No separate index deploy** (decided 2026-11-12). Both GSIs (one per table) ship in the main deploy, inside the window. CloudFormation can report the update complete before a new index is `ACTIVE` (seen in the 1.2.0 upgrade rehearsal): its backfill goes on after the stack update, which therefore takes no longer on a large table. Until the index is built, a read of it fails with `ValidationException` "Cannot read from backfilling global secondary index". The reconciler and the sweeper treat that as transient (§3 step 4): a task that waits for assets waits instead of starting, and the console's banner names the sweeper steps that cannot read the index yet. Both clear by themselves once the index is built. A separate release or a two-phase template parameter would have avoided that window at the price of an extra upgrade step.
2. **Window.**
   - Pause pipeline schedules.
   - Wait until no `run_task` execution is in flight: no task is `running`, `waiting_delay` or `waiting_decision`, and no run is paused (resume or stop paused runs first, since a task held at the start of a paused run is a `run_task` execution too). The console's Tasks view shows it; there is no separate script (D39). A producer started on the old definition that finishes after the deploy calls deleted targets and fails loudly; the drain avoids that.
   - Waiting wrappers do **not** need to finish: the sweeper adopts their rows (§8 step 5) and reconciles them from facts. A pre-upgrade wrapper that receives a skip is handled by the zombie rule (§3 step 3).
   - Existing asset facts get their partition fields during adoption, so the partition index sees them.
3. **Main deploy.**
   - `polyris-up --disable-rollback` (ADR #131). Its change set is checked first: a replaced or removed table stops it. This upgrade removes `QueuedAssetEventsTable`, which 1.2 no longer uses, so `polyris-up` asks before going on; without a terminal it needs `--accept-data-loss`. A failed update keeps what it already changed, so it is fixed forward (D13), not rolled back to code that does not know the new rows.
   - The run_task definition is updated only after the event source mapping exists (`DependsOn`).
4. **After.**
   - Resume schedules.
   - Re-run the fires missed during the window: the schedules that were paused, for the dates they would have run.
   - Check the health banner and the smoke subset.

The CHANGELOG upgrade note repeats these steps.

## Rules for the code

The lessons of this design's defects, as rules for whoever changes the coordination code. Root `CLAUDE.md` keeps one line per rule under the same numbers, which code and tests cite.

**Rule 40. Delete a wake-up pointer only after the signal it serves was accepted — never before**
A subscription, or any pointer that says "wake this waiter", may be deleted only after the signal it exists for was accepted. If you delete first and then fail to signal, nothing will ever re-check the waiter.

In the reconciler (ADR #130 §3), a run's pointers are deleted only after an accepted signal. Leftover pointers are harmless no-ops that expire by TTL.

**Rule 41. Wake-up hints may be lost; wake-up decisions may not**
- **Hint paths** only say "check this waiter again": the asset-events stream, `notify_dependents`, registration, resume. They may fail or drop a hint, because a periodic re-check from facts (the sweeper, ADR #130 §8) recovers it.
- **Decision and action paths** never fail silently: the reconciler, the wrapper's status write, and every write of a fact. They retry, fail the affected task with the real cause, or surface a console warning (#38). A fact is state, not a hint: nothing re-checks a fact that was never written, so Mark Successful and `run_task` turn a failed fact write into a `fact` warning (B-M2).

Anti-pattern: a wake-up path whose loss nothing re-checks, for example a fire-and-forget invoke with no periodic re-check behind it.

**Rule 43. The receiver of a signal owns the status transition that follows it**
When component A signals component B (e.g. `SendTaskSuccess` to a waiting wrapper), B writes the resulting status, not A. If A writes it, two things break:
- a crash between the signal and the write loses the status;
- a consumed token cannot be told apart from an expired one.

Under ADR #130 the wrapper writes `deps_ready`, `skipped` or `upstream_failed` itself, and the reconciler only signals.

**Rule 46. A task token handed over through a write is findable by the sweeper, and a failed write fails the token**
When a state stores its `waitForTaskToken` for another component to send (`pause_waiter` saves `pause_token` for resume):
- the same write gives the row an attention entry (`paused#`), so the sweeper releases the task if the sender missed it. A sender that finds tokens through an index or one scan misses a token written while it ran (B-P3);
- a failed write is caught and fails the token with the cause. Nothing else will ever hold that token, so without the catch the task waits for its heartbeat and then runs anyway (B-P4);
- adoption and any other backfill of attention entries route a row by the same rule as its writer: a row holding a pause token goes to `paused#`, not `waiting#`.

**Rule 47. A task row has one owner at a time, named by its generation**
Every write made for an owner (the wrapper, registration, `run_task`, the pause wait, the failure handler) carries `ownership.FENCE` with the owner's generation. Every write that ends an ownership (an operator's decision, a restart, the reconciler closing a zombie) raises it with `ownership.RAISE`, in the same update that records the decision. A fenced write ends its writer quietly: no status, no notification, no work. Six bugs came from writers that checked something else or nothing (B-O1…B-O5, B-W1); see ADR #130 §2.1. `tests/sdk/test_task_row_writers.py` lists every writer of the tokens table, and a new one fails it until it has a role there.

A retry after a lost answer must accept the row it already moved: a claim accepts its own target status under its own generation (B-W1). Test such a write by applying it twice.

**Rule 49. A helper template reads an input an older caller may omit through `$exists`**
During an upgrade, wrappers started on the old definitions keep calling the new helpers until they finish. `$string()` of a missing field fails the state, and with it the task: a pre-upgrade wrapper registering again failed on `starter_input`. Default such a field to a value its readers already treat as absent (`''`, `0`), and pin it with a test that runs the helper on the old caller's input (`PRE_UPGRADE_WRAPPER_INPUT` in `tests/sdk/test_registration_template.py`).

**Rule 50. A time that marks a beginning is written once**
When the writer runs again, as registration does on every run, write the field with `if_not_exists`. Otherwise it moves forward and silently narrows whatever it gates: `subscribed_at` decides which facts a push consumer reacts to, and a moving one would hide the facts a lost stream record left for the catch-up (B-U2).

**Rule 51. A reader of shared rows skips a row it cannot parse, and says so**
One malformed row must not fail every decision that reads the table. Catch the parse error per row, count the row for nothing, and write a warning. `FactStore.read` does this for asset facts (B-F2), the sweeper for push retry rows, and `/api/health` for the engine health row, which it names as unreadable instead of failing the whole check.

**Rule 52. A time budget is checked per item, and a periodic job's steps fail independently**
A budget checked only between batches lets one large item run the function into its timeout: AssetFanout checked it between records, so a record with many waiters was retried for 24 hours and blocked its shard, and the catch-up checked it between assets, so an asset with many facts never finished. Check the deadline before each item, give up the rest as lost hints where a re-check exists (#41), and keep a cursor inside the item's group (`catchup_within`). A periodic job runs each step in its own `try`: a failing step must not skip the later steps or the self-check, and the health report names it (`sweep.STEPS`, `failed_steps`).

**Rule 55. An evaluation as of a time reads only what was recorded by then**
A push judges a consumer's schedule as of its fact, and the stream, a redelivery and the hourly catch-up must give one answer. Reading the current facts with only the clock set back is not that: a fact recorded later still counts. The catch-up then started a consumer that a `cascade='none'` backfill had completed (ADR #57; external review, 1.2.0). Pass the cutoff to the evaluation (`recorded_by`), so that it drops the later facts from both the reader and the known facts. The cutoff is the writer's stamp, taken before the write commits. Two facts stamped within a write's latency of each other can therefore commit in the other order. If the later one's record is processed before the earlier one commits, neither stream push starts the consumer, and the hourly catch-up starts it instead.

## Out of scope

Each can be added later without rework.
- Task failures for older partition dates in the bell (`attention = failed#{h}` from the failure writers).
- Queue-based fan-out at scale (2.10).
- A task-status stream instead of `notify_dependents`.
- Hourly partition identity below a day.
- `all_done` after sibling failure (1.13).
- Push notifications (e-mail/Slack) about engine health: an integration on top of `_engine_health`.

**Deferred after the CR-6 reviews.** These need more than an addition, so each gets its own change after this release:
- **A waiting task as a row, not a process.** One scheduler would start the tasks that are ready, with no wrapper execution or task token while a task waits. That removes the class of bugs about who owns a task row (B-O1, B-W1 and the round 2 findings). The decision core, the facts, the stream and the sweeper carry over unchanged. The generation (§2.1) already closes that class inside the current model.
- Registration as one Lambda call holding the task token: one execution and about seven transitions fewer per task.
- One behaviour for a long pause: a task held at the start of a paused run starts after its 12-hour heartbeat, while a parked task fails at its timeout.
- Subscriptions keyed by partition, not by asset name: today a backfill multiplies AssetFanout's work.
- A producer run id on each fact, so that a fact write retried after a lost answer cannot record a second fact.
- A share of the sweeper's budget for each step, and a catch-up that replays only the facts since its last pass.
- Warnings on read paths at most once an hour per row; health signals for stream lag and for older-version rows that still appear.
- Removal of the migration code in 1.3: the adoption scans, the `$exists` guards for wrappers started before the upgrade, `close_skipped` for their skips, the `UpstreamFailed` mapping, the generation's branches for rows without one (`attribute_not_exists(generation)`, `ownership.is_legacy`, `sweep.LEGACY_WAITS`), `run_task`'s `attempt` conditions, the `fact_version` gates, the cutover taken from the registry's `registered_at`, and the frozen v1.1.4 wrapper of the test harness.

## Rejected alternatives

- **Accumulated flags with more transitions** (the unmerged AND-gate draft). It produced four hangs while being built, and it keeps the cause.
- **The sender writes the post-signal status.** A crash between signal and write loses the status, and a consumed token cannot be told from an expired one.
- **CloudWatch alarms, SNS and a DLQ** (rev 2). They need setup, a reader and recurring cost, and they leave recovery to a human (D22).
- **Event-time read bounds plus writer-side date validation** (rev 2). They are fragile with future-dated runs, mixed time formats and non-daily partition keys. The partition index answers exactly.
- **Rule A** ("an event covers its calendar period"). It makes consumers wait for end-of-period producers.
- **Fixed-day periods** (7 and 31 days, revision 3). They leave days that can never be satisfied for irregular schedules ("first Monday of the month").
- **Push keeps today's same-date semantics.** Rejected by the maintainer: a consumer's cadence is set by the grain of its own output, so the finest input drives the run, with coarser inputs as reference data.
- **Rule C** (forbid mixed cadences). It pushes the problem to users and is inexact for backfills.
- **Merging the subscription tables.** The per-run key fixes collisions without it.
- **Writer-published EventBridge/SQS events.** Writers would still have to remember to publish.
- **Reserved concurrency.** It fails deploys on low-limit accounts; the lease prevents sweeper overlap.
- **Two deploys with drain stubs.** The maintenance window plus sweeper adoption needs no shim code.

## Testing

The test strategy, scenario matrix, invariant harness, live smoke and game-day are described in `tests/backend/coordination/README.md`. Two requirements are normative:
- every invariant has a test;
- every defect in *Context* has a scenario that is green after the change, and a mutation probe that re-introduces the defect in the new code and makes that scenario fail. The ids in the defects table are the ones the scenarios carry (`tests/backend/coordination/defects.py`). Defects that live only in code this ADR deletes are reproduced once on the pre-change system (live smoke), not through a test binding to code that is about to be removed.
