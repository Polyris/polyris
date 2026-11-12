# ADR #130 — Coordination layer: one reconciler, facts as state, stream-driven hints

> **Status:** ACCEPTED (2026-11-12), revision 5. Implementation in progress on `feat/coordination-reconciler` (target release v1.2.0).
>
> The design went through five review rounds: break, architect, developer, QA, DevOps and reliability reviews. Every finding is resolved below or listed under *Out of scope*.
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
| B-R1 | `restart_task` passes neither `wait_for` nor the other starter fields → the restarted task is not gated on assets |
| B-N1 | Warning rows are dated by partition and grouped per run → warnings for older dates never reach the bell; one warning hides another |
| B-S1 | `Auto_Skip_Register` swallows a failed status write and still notifies dependants |
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
- one helper, `write_asset_fact`, used by `mark_success` and by every other fact writer outside `run_task`.

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

The write is conditional (`attribute_not_exists(event_time)`, retried with a fresh timestamp), so a fact is never overwritten.

`cascade='none'` (ADR #57) now records the fact but starts no push consumer. Pull waiters for that partition are woken, because the data exists. A writer census test fails on any `asset-events` writer that does not use the template or the helper.

### 2. Status ownership

`RECONCILABLE_STATUSES = {waiting, waiting_paused}` is a generated constant. Each transition has exactly one owner:

| Transition | Owner |
|---|---|
| → `waiting` (row created) | registration `Save_Task_Record` |
| `waiting` → `waiting_paused` (dependencies satisfied, pipeline paused) | reconciler (§3) |
| `waiting`/`waiting_paused` → `deps_ready`, `skipped` (+ `skip_origin='rule'`, `skip_reason`, `finished_at`), `upstream_failed` | **the wrapper**, after it receives the signal (§6.4) |
| → `failed` (task failures, including coordination errors) | wrapper → failure handler, which never overwrites `upstream_failed` |
| `waiting` → `aborted` (zombie: wrapper no longer running), or → `skipped` (a pre-upgrade wrapper consumed a skip signal) | reconciler (§3, step 3) |
| `waiting_delay`, `waiting_decision`, `running`, terminal statuses, `pause_token` rows | wrapper / `run_task` / `pause_waiter` / console actions, as today |

The party that **receives** a wake writes the status that follows it. A signal and its status therefore cannot be split by a crash, and the reconciler never has to tell a token it consumed from one that expired.

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
   - Otherwise, if it is not running and the row still holds `T` and `S`, the row is a zombie (for example, a wrapper stopped outside polyris). Set `status = aborted` with an explanatory `error`, remove it from the attention index, and write a warning. The outcome is loud, not silent.
   - Zombies are also found without a signal. A row past its liveness deadline (`started_at` + 3 × `orchestration_timeout` + 1 h, which covers the wrapper's timeout retries) gets the same wrapper check from the sweeper.
4. **Transient error** (throttling, 5xx, timeouts): **raise**. Nothing is written. The caller's retry or the sweeper repeats the call.
5. **Non-transient error** (AccessDenied, missing resource, validation, a malformed spec, an unexpected exception in `decide`/`asset_conditions`): `SendTaskFailure(T, error="CoordinationError", cause=<exception>)`. The error name is always `CoordinationError`, never a forwarded `States.*` name, so the wrapper's `Retry` (which matches `States.Timeout`) cannot turn it into a re-registration. A failed condition check is never an error: another owner already acted, so it counts as a `noop`. The wrapper's existing `Catch` → failure handler marks the task `failed` with the real cause, visible in the console and the bell (D22, level 2). If `SendTaskFailure` itself is denied, write a warning; the engine-health check reports it (§9).
6. **`noop`:** if the status is not reconcilable, remove the row from the attention index, `IF status = :read_status AND started_at = :S`. Pinning to `started_at` means a re-registered row (new `started_at`) is never touched. The update uses `attribute_exists(execution_name)`, so it never creates a phantom row.
7. **Fail closed.** No read failure, partial result or unknown state leads to a success signal.

**Output.** `{verdict, reasons, deps, assets, action, latency_ms}`, logged as one structured line and returned.

### 4. Asset conditions

`asset_conditions.evaluate(spec, *, partition_date, now, reader, known_events=()) → (ready, per_asset)` is the only implementation of `wait_for` and `schedule=[…]`. `reader` is a DAL protocol. `known_events` carries the event that triggered the call.

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

The index is eventually consistent. To close the lag window, the reader also runs one consistent base-table query per asset in the spec for facts written in the last 15 minutes (`event_time ≥ now − 15 min`) and merges the results. This covers two outlets of one producer that arrive in different stream batches, and a consumer that registers just after a fact was written. `known_events` still carries the triggering record.

**Granularity** is taken from the fact row. If the row has none (legacy rows), the evaluator uses the producer's registry entry via `source_dag`; failing that, `daily`.

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

**Per-record work, with a time budget.** Records not reached within the budget are reported as batch-item failures and redelivered; the work is idempotent.
- **Pull:** query the `asset:{name}` subscriptions (consistent) and call `reconcile(subscriber, known_events=[record])` with bounded parallelism.
- **Push** (unless `cascade = none`):
  - for each `asset-subscriptions` row, evaluate the consumer's registry spec for **the event's own date**, D = `partition_date`, with the same rule as pull (§4). A run therefore follows the cadence of the finest input: a daily `sales` event starts that day's run with the latest weekly `prices` (D12). If a coarse input was missing for two or more periods, the dates it blocked are not started later automatically; recovering them is a backfill. A `.within()` in a push spec is evaluated as of the triggering fact's `event_time`, not the time of evaluation, so a retry, a late record or a catch-up replay gives the same answer as the first evaluation;
  - if satisfied (or `cascade = all`), start the consumer with a deterministic name from the run-name helper (§6.6);
  - `ExecutionAlreadyExists` counts as done;
  - a permanent error (`StateMachineDoesNotExist`) drops the item and writes one warning;
  - a transient error writes a **push retry row** (§7).
- **Isolation.** A failure for one subscriber or consumer never fails the record. A record raises only if even the retry row cannot be written, and then the stream retries until the table is reachable again.

**Writers only write the fact.**
- `run_task` keeps `Record_Asset_Event` and drops its two notify states.
- `mark_success` and every other writer outside `run_task` call `write_asset_fact`.

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
2. **Fast path:** a task with no dependencies and no `wait_for` is signalled immediately. Pause is enforced at run start by `pause_waiter`.
3. **Everything else:** write all subscriptions in one Map, then call `reconcile(execution_name)` with `Retry`.
4. **Failures in the registration SFN are caught.**
   - **Transient** means Lambda throttling or service errors, or the reconciler's own `CoordinationTransient`. Everything else is **non-transient**.
   - **`Save_Task_Record` fails** after its `Retry`: there is no row for the sweeper to find, so the failure is always sent as `SendTaskFailure(wait_token, error="CoordinationError", cause)`. The failure handler then writes the `failed` row even when none exists yet: its update also sets the task's identity fields (`task_name`, `pipeline_execution`, `date`, ...) when they are missing, instead of being conditional on `attribute_exists`, so the failure appears in its run.
   - **Any later step fails non-transiently:** `SendTaskFailure(wait_token, error="CoordinationError", cause)`. The task fails loudly with the reason instead of hanging (B-G1).
   - **Any later step fails transiently** after `Retry`: registration ends quietly with `attention_at = now`. The row is already saved, so nothing is lost. The sweeper reconciles it within one run.

Deleted: `Check_Dependencies_And_Assets`, `Eval_Task_Deps`, `Route_Combined_Check`, the signal/status pairs, `Update_Status_Ready`, and the `check_assets` Lambda.

#### 6.4 Wrapper (owns post-wait status)

After `Wait_For_Dependencies` resumes:

| Signal | Wrapper action |
|---|---|
| `deps_ready` | `SET status = deps_ready REMOVE wait_token, attention, attention_at IF status IN (:waiting, :waiting_paused) AND wrapper_execution_arn = :self` |
| `deps_skip` | the same with `skipped`, `skip_origin='rule'`, `skip_reason` and `finished_at`, **before** notifying dependants |
| `deps_blocked` | the same with `upstream_failed`, its reason in `error`, and `finished_at`; then the failure handler as today, which leaves `upstream_failed` rows alone |

If the condition fails, another owner has already moved the task. Example: an operator's skip or success, which writes the status before it stops the wrapper. The wrapper then ends quietly as *superseded*: it does not run the task and raises no failure. A failure of the write itself, as opposed to its condition, fails the wrapper visibly.

The failure handler also maps `error.Error = 'UpstreamFailed'` to `upstream_failed`. A pre-upgrade wrapper that receives a block signal therefore still ends with the right status.

#### 6.5 Resume, restart, auto-skip

- **Resume:** clear the pause flag and send the `pause_token` callbacks as today, but drop the unconditional `deps_ready` write that followed them (`run_task` continues and writes `running` itself). Then call `reconcile({pipeline_execution, source: resume})`. That call finds the run's rows through `pipeline-execution-index` (candidates only), with bounded parallelism and a 20 s budget. The response reports how many rows were handled and how many were deferred. Deferred rows keep their attention entry with `attention_at = now`, so the next sweeper tick handles them; they do not count as primary-path misses.
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
| `retry#{0-f}` | **push** retry rows `_retry_push_{h(consumer, date or event)}` (pull needs none: the waiting row is already indexed) | deleted on success; exhausted after 24 h → warning; TTL 7 days |
| `warn#{0-f}` | coordination warnings (§9) | TTL 8 days |

**`asset-partition-index`** on `asset-events` (D23): PK `asset_name`, SK `partition_date`. Projection INCLUDE: `event_time`, `partition_key`, `granularity`, `cascade`, `source_dag`, `uri`.

Each table gets one GSI, so both fit the one-GSI-change-per-table limit and can ship in the same pre-deploy.

### 8. Sweeper

An EventBridge Scheduler runs it at `rate(5 minutes)` with no scheduler retry. The function timeout is 240 s.

**Lease.** The `_sweeper_lease` row (`owner`, `expires_at = now + 270 s`) is taken with a conditional put that succeeds if the row is absent or expired. Only its owner releases it. A run that cannot take the lease exits normally. A lease that has been stale for a long time is a health signal (§9).

**Each run, within a 200 s budget:**
1. **Due rows.** Query `waiting#0..f` for `attention_at ≤ now`, earliest due first. For each row:
   - Call `reconcile` in-process, with parallelism 10.
   - If it is still waiting, set `attention_at` to the next due time (backoff 10 → 20 → 30 min, then every 30 min) and increment `attempts`. The write is conditional on `started_at`.

   Due-time ordering means nothing starves. The backoff caps the work at 48 checks per waiting task per day.
2. **Push retries.** For `retry#0..f` rows that are due, retry with backoff (5 min → 1 h).
3. **Push catch-up,** hourly. For every subscribed asset, read the facts written in the last 48 hours (consistent base-table query) and replay AssetFanout's push branch for each of them: same date, same rule, same `cascade` handling, same deterministic names. Only facts with `fact_version = 2`, written by the new code, are replayed. Facts written by the old code were already handled by the old push path, so they are never replayed and nothing starts twice. No time cutover or per-subscription timestamp is needed.
4. **Engine self-check:**
   - event source mapping `Enabled`;
   - stream enabled;
   - the oldest overdue `waiting` row;
   - push retry backlog.

   The results go to the `_engine_health` row (§9).
5. **Adoption.** After a new platform version is first seen, each run continues two resumable scans until both finish. The task-row scan then repeats for one more hour. The version marker and the cursors are kept in `_engine_health`. Adopted rows that have no `orchestration_timeout` use the platform default for the liveness deadline. Adopted rows that have no `starter_input` restart the way they do today, with a warning.
   - **Task rows.** Every reconcilable row without an `attention` key gets one. Rows registered before or during the deploy are then reconciled from their facts, whatever pointer shape they were written with. The extra hour of repeats covers registrations that the old definitions wrote while the stack update was still running.
   - **Asset facts.** Every row without `partition_date` gets `partition_key`, `partition_date` and `granularity`, derived from `execution_date` and the producer's registry entry. It does **not** get `fact_version`. These writes are `MODIFY` records, which the INSERT-only stream filter ignores, so they trigger nothing.

**Logging a primary-path miss.** When the sweeper wakes a task whose enabling fact is older than that task's previous due time, it writes a `sweeper` warning: the primary path missed it.

### 9. Failure handling and visibility (D21, D22)

**Three levels, with no extra infrastructure.** No custom metrics, no CloudWatch alarms, no SNS, no DLQ.

1. **Heals itself in code.** Retries, the sweeper, push retries and catch-up, idempotent signals and starts. No human is needed.
2. **Fails loudly in code.** Non-transient coordination errors fail the affected task with the real cause (§3 step 5; registration §6.3). Zombies are marked `aborted` with an explanation (§3 step 3). The user sees a failed task with its reason where they already look, and re-runs it after fixing the cause.
3. **Engine health is shown in the console.** This covers the case where no code runs at all (schedule removed, event source mapping disabled, stream disabled).
   - The sweeper writes `_engine_health`: last run, version, adoption state, event source mapping and stream state, oldest overdue waiting row, push retry backlog, last coordination error.
   - `/api/health` adds a `coordination` section. It also judges staleness itself: no sweeper run for 15 minutes means the engine is unhealthy.
   - The console shows a banner while the engine is unhealthy.

**Warnings.** One helper, `write_warning(*, kind, execution_name, task_name, pipeline_name, pipeline_execution, task_date, error)`, writes `_notify_warn_{kind}_{execution_name}`:
- `kind ∈ WarningKind`: `coordination`, `zombie`, `push`, `sweeper`, `notify`;
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
- `python -m polyris.codegen.sync_coordination` emits them into `sam/lambdas/coordination/coordination_core/` **and** `sam/lambdas/console_api/coordination_core/`, with a `--check` gate in CI. The console and the Lambdas therefore always run the same code from the same platform release, independent of the SDK version pinned in `requirements.txt`.
- One CodeUri, `sam/lambdas/coordination/`, holds three functions (`Reconcile`, `AssetFanout`, `Sweeper`), each with its own least-privilege role.

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
5. **Every reconcilable row without `pause_token` is in the attention index.** A row that leaves a reconcilable status leaves the index within one sweeper run. After a deploy, adoption completes.
6. **Facts are never overwritten.**
7. **Every asset-event INSERT is processed by AssetFanout.** Pull waiters are also covered by the sweeper, and push consumers by retry rows and the catch-up pass.
8. **Push runs at most once** per (consumer, date) for AND and per (consumer, event) for OR, and **at least once** within the catch-up window.

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

**Cost:**
- Fewer STANDARD-SFN transitions per registration and per producer run.
- No Express SFN per outlet.
- No push counters.
- Bounded sweeper work (≤ 48 checks per waiting task per day, in-process).
- Two GSIs (one extra write per task row change and per fact).
- No metrics or alarms.


## Migration

Forward-fix only (D13):

1. **Index deploy.** Add both GSIs (one per table), then wait until both are `ACTIVE`. This is additive.
2. **Window.**
   - Pause pipeline schedules.
   - Wait until no `run_task` execution is running (a checkable list). A producer started on the old definition that finishes after the deploy calls deleted targets and fails loudly; the drain avoids that.
   - Waiting wrappers do **not** need to finish: the sweeper adopts their rows (§8 step 5) and reconciles them from facts. A pre-upgrade wrapper that receives a skip is handled by the zombie rule (§3 step 3).
   - Existing asset facts get their partition fields during adoption, so the partition index sees them.
3. **Main deploy.**
   - `sam deploy --disable-rollback`, after a change-set check: no unexpected replacements.
   - The run_task definition is updated only after the event source mapping exists (`DependsOn`).
4. **After.**
   - Resume schedules.
   - Re-run the fires missed during the window (the procedure lists them).
   - Check the health banner and the smoke subset.

The CHANGELOG upgrade note repeats these steps.

## Out of scope

Each can be added later without rework.
- Task failures for older partition dates in the bell (`attention = failed#{h}` from the failure writers).
- Queue-based fan-out at scale (2.10).
- A task-status stream instead of `notify_dependents`.
- Hourly partition identity below a day.
- `all_done` after sibling failure (1.13).
- Push notifications (e-mail/Slack) about engine health: an integration on top of `_engine_health`.

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
