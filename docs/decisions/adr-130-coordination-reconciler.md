---
area: engine
accepted: 2026-11-12
revised: 2026-11-26
supersedes: "#122, #128"
amends: "#57"
---
# ADR #130 — Coordination layer: one reconciler, facts as state, stream-driven hints

> **Superseded in part by ADR #137** (tasks as rows, 1.3.0). Its table *ADR #130 after this ADR* lists, for each section, invariant and rule below, whether it stays, changes or is retired; each section it changes says so here.
>
> **Supersedes:** ADR #122 (registration fast-path verdicts, `assets_ready` flag); ADR #128 (direct-invoke wake-up from `mark_success`). **Amends:** ADR #57: push consumers start with deterministic names, and `cascade='none'` *records* the asset fact but starts no push consumer (§1). **Unchanged:** trigger-rule semantics move into one implementation (ADR #138 states them today). An AND-gate design numbered #129 was drafted on an unmerged branch and never reached `main` (*Rejected alternatives*).

## Context

Every hang and wrong wake of the 1.1 coordination layer traced to five causes:
1. **Readiness was decided in six places, each with partial information** (the registration JSONata, `check_assets`, `notify_asset_subscribers`, `evaluate_deps`, the routing in `notify_dependents`, `resume_execution`), and they disagreed: one counted `success` but not `succeeded`, one called a verdict `blocked` and another `upstream_failed`, one ignored pause for tasks without dependencies.
2. **Readiness was accumulated from events, not evaluated from facts.** An `assets_ready` flag and a per-day counter and lock: an event that was never credited (lost, landed before its subscription, arrived during a pause) left the state wrong for good.
3. **Every writer of a fact had to notify every consumer**: `mark_success` left `wait_for` waiters asleep until ADR #128 added a second call.
4. **There was no safety net, so primary paths failed open:** they woke a task when unsure.
5. **Reads on the decision path were eventually consistent.**

The defects this fixed carry ids that the coordination harness's scenarios carry; `tests/backend/coordination/defects.py` describes each.

## Decision

### 1. Facts are the only state

Readiness is evaluated on demand from facts: the task rows, the asset events (`asset-events`) and the run's pause state, which the run row holds (ADR #137 §5). No readiness flag, set, counter or lock is written.

**Asset fact rows.** Writers: `run_task`, in its success transaction (ADR #137 §1), and one helper, `asset_events_repo.write_fact` (console DAL), used by Mark Successful in its action's transaction (ADR #137 §3) and by every other fact writer outside `run_task`. A fact is keyed by `(asset_name, event_time)`. Every writer puts it with `attribute_not_exists(event_time)` and retries a collision with a later time, so two facts of one asset in the same millisecond never overwrite each other: an overwrite would lose a fact and, as a `MODIFY`, never reach the INSERT-only stream mapping.

| Field | Meaning |
|---|---|
| `event_time` | write time, `YYYY-MM-DDTHH:MM:SS.sssZ`; readers accept the legacy `+00:00` form |
| `partition_key` | the run's partition, raw (`YYYY-MM-DD`, `YYYY-MM-DDTHH`, `YYYY-Www`, `YYYY-MM`) |
| `partition_date` | the partition's normalised date: the date itself, the date of an hour, the Monday that starts an ISO week, the first day of a month. Start-of-period dates make a backfill keyed `YYYY-MM` or `YYYY-Www` line up with a scheduled run keyed by its first day |
| `granularity` | the outlet's declared granularity (ADR #50) |
| `cascade` | `auto`, `none` or `all` |
| `source_dag` | the producing pipeline |

The partition of a fact is the **partition of the run that produced it** (for Mark Successful, the marked row's run, not the request's date). So is its cascade: `none` or `all` when a backfill started the run with `_suppress_asset_event` or `cascade_all`, `auto` otherwise; `run_task` reads the flags from its input, Mark Successful from the run's input item (`records.cascade_of`).

`cascade='none'` (ADR #57) records the fact but starts no push consumer; pull waiters for that partition are woken, because the data exists. `tests/sdk/test_run_task_facts.py` checks that `run_task` writes the fact the helper writes, and `tests/sdk/test_asset_fact_writers_wiring.py` that both writers reach the same table and keep facts equally long.

### 2. Status ownership

Superseded by ADR #137 §3.

### 2.1 One owner per task row: the generation

A task row carries `generation`, a number, and has one owner at a time, named by it (Rule 47).
- **Every write an owner makes is fenced** with `generation = :g`.
- **Ending an ownership raises it** (`ownership.RAISE`) in the same write that records the decision: an operator's action, the dispatcher's work on a request, the engine's stop of a run (ADR #137 invariant 6). A raised generation keeps the old finished status from serving a dependant of a restarted task.
- **A fenced write ends its writer quietly:** no status, no notification, no work. `run_task` ends at `Stale_Attempt_Superseded`.
- **A retry after a lost answer accepts the row it already moved:** a claim accepts its own target status under its own generation, so a retry after a lost answer (a 5xx, a timeout) passes, and a test applies such a write twice.

The writers are `Start_Run`, the dispatcher, `run_task` and the console's builder (ADR #137 §3); `tests/sdk/test_task_row_writers.py` lists every writer of the tokens table, and a new one fails it until it has a role there. Amended by ADR #137: the generation is mandatory (no branch for rows without one), and a decision also checks the run row's `run_version`.

### 3. The reconciler

`reconcile` is the dispatcher of ADR #137 §1, which decides and then writes and starts what it decided. What holds from this section:
- **Reads.** Task facts come from base tables with consistent reads (the task row, the dependency rows, the run row), with no error swallowing. Asset facts come through `asset_conditions` (§4). Indexes only find candidates and serve the asset-partition lookups (§4); a decision never rests on an index read of task facts.
- **Decision.** `decide(row, deps, assets, paused) → Decision` is a pure function in `polyris/coordination/`; `Decision` carries the verdict (`noop`, `skip`, `upstream_failed`, `wait`, `paused`, `ready`) and the reasons. A trigger rule that can no longer be satisfied gives `skip` or `upstream_failed` (ADR #138).
- **Transient errors** (throttling, 5xx, timeouts, and a read of an index still being built, DynamoDB's `ValidationException` "Cannot read from backfilling global secondary index", seen when an upgrade adds an index; `failures.is_transient`) raise, and nothing is written: the caller's retry or the sweeper repeats the call (Principle #53).
- **Non-transient errors** (a malformed spec, an unexpected exception in `decide` or `asset_conditions`) fail the affected task with the real cause (`CoordinationError`), where the console and the bell show it (§9, level 2); an `AccessDeniedException` warns instead (ADR #137 §7). A failed condition check is never an error: another owner already acted, so it counts as a `noop`.
- **Fail closed.** No read failure, partial result or unknown state leads to a start, a close or a run's answer.
- **Output.** One structured log line per pass, with its counts (ADR #137 §7), and the enabling time of a task it found ready (§9).

### 4. Asset conditions

`asset_conditions.evaluate(spec, *, target, now, reader, known_facts=(), recorded_by=None) → Evaluation` is the only implementation of `wait_for` and `schedule=[…]`. `target` is the date D below, `reader` a DAL protocol, and `known_facts` the fact that triggered the call. A push passes its fact's `event_time` as `recorded_by`: facts recorded after it do not count (§6.1, Rule 55).

D is `partition_date(row.date)`, normalised exactly like fact rows (§1), so non-daily backfill keys (`YYYY-Www`, `YYYY-MM`, `YYYY-MM-DDTHH`) are well defined.

**Rule B.** Calendar periods, not day counts: fixed day counts leave gaps for irregular schedules ("first Monday of the month" has 35 days between runs).

| Asset granularity | A fact of `a` satisfies date D when |
|---|---|
| hourly, daily | `partition_date = D` (exact) |
| weekly | `partition_date ≤ D`, and its ISO week is D's week or the week before |
| monthly | `partition_date ≤ D`, and its month is D's month or the month before |

Among the facts that satisfy D, the one with the latest `partition_date` is used: as-of semantics, the freshest version that existed for D. Weekly and monthly assets act as reference data for daily consumers.

| Condition | Ready when |
|---|---|
| `a` | a fact of `a` satisfies D (table above) |
| `a.within(hours=N)` | the fact with the latest `partition_date ≤ D` has `event_time ≥ now − N h`. A backfill of an older partition never refreshes it |
| `a.consecutive(days=N)` | facts exist for each of the N dates ending at D |
| `[x, y]` / `x \| y` | AND / OR, at any nesting depth |

**Lookup.** A GSI `asset-partition-index` on `asset-events` (PK `asset_name`, SK `partition_date`) serves every condition: `a` and `.within()` query `partition_date ≤ D` in descending order, then apply the period rule, reading every fact of the latest qualifying `partition_date` (with pagination) and comparing them by `event_time`; `consecutive` queries `partition_date BETWEEN D − N + 1 AND D`. The index is eventually consistent, so the reader also runs one consistent base-table query per asset in the spec for facts written in the last 15 minutes and merges the results; this covers two outlets of one producer that arrive in different stream batches, and a consumer that registers just after a fact was written. `known_facts` still carries the triggering fact.

**Granularity** is taken from the fact row; a fact written before ADR #130 has none, and its key's format gives it (`records.fact_from_item`).

**Unreadable facts.** A fact the reader cannot place on the date line (a row an older version recorded for a run date that is no partition key, such as `latest`) counts for no task. The reader writes one `coordination` warning for the fact and goes on, so one bad row never fails the decision of every task waiting on its asset (Rule 51).

**Push specs** come from the consumer's registry entry, which has the full nesting and `.within()`. `asset-subscriptions` is only the index of whom to evaluate.

### 5. Subscriptions are per-run pointers

Amended by ADR #137 §7: only the `wait_for` pointers remain. In `dep-subscriptions`, `dependency_key = asset:{name}`, `subscriber` = the waiting task's `execution_name`, `run` = its run row's key, and no token. Queries of them on the decision path are consistent.

### 6. Hints

Hints only say "check again". Losing one delays a task; it never makes a decision wrong (Rule 41).

#### 6.1 Asset events → AssetFanout

A DynamoDB stream on `AssetEventsTable` (`NEW_IMAGE`) feeds an event source mapping:

| Setting | Value |
|---|---|
| `StartingPosition` | `TRIM_HORIZON` |
| `FilterCriteria` | `INSERT` only |
| `BatchSize` | 10 |
| `MaximumRetryAttempts` | -1 |
| `MaximumRecordAgeInSeconds` | -1 (records live until the stream's 24 h retention) |
| `BisectBatchOnFunctionError` | true |
| `FunctionResponseTypes` | `ReportBatchItemFailures` |
| on-failure destination | none (§9) |

The mapping exists before any definition that records facts: `run_task`'s state machine depends on it (`DependsOn: AssetEventsFanoutMapping` in `sam/template.yaml`), so a fact a new definition records finds the mapping that wakes its waiters.

**Per-record work, with a time budget.** Records are processed one at a time, and so is the work inside a record. Records not reached within the budget are reported as batch-item failures and redelivered; the work is idempotent. Inside a record the work stops with 5 s of the invocation left: the waiters not reached are lost hints, which the sweeper's checks recover, and the push consumers not reached wait for the hourly catch-up. So one record with more waiters than fit in an invocation never times the function out and blocks its shard (Rule 52). Push runs before pull: a record's consumers are few and only the catch-up replays a missed one, while its waiters may be many and the sweeper checks each again.
- **Pull:** query the `asset:{name}` pointers (consistent) and make one narrow pass of the dispatcher over each run with a waiting task (ADR #137 §7).
- **Push** (unless `cascade = none`):
  - for each `asset-subscriptions` row, evaluate the consumer's registry spec for **the event's own date**, D = `partition_date`, with the same rule as pull (§4). A run therefore follows the cadence of its finest input: a daily `sales` event starts that day's run with the latest weekly `prices`. If a coarse input was missing for two or more periods, the dates it blocked are not started later automatically; recovering them is a backfill.
  - A push spec is evaluated as it stood when the triggering fact arrived: a `.within()` as of the fact's `event_time`, and no fact recorded after it counts (`asset_conditions.evaluate(recorded_by=)`). A retry, a late record or a catch-up replay gives the same answer as the first evaluation (Rule 55).
  - A consumer reacts only to facts recorded after it subscribed: a row whose `subscribed_at` is later than the fact's `event_time` is skipped. `subscribed_at` is written once, at the consumer's first registration (`if_not_exists`), so the registration every run repeats does not move it (Rule 50). A row without `subscribed_at` takes the pipeline's last deploy (`registered_at`) as the cutover.
  - A subscription its pipeline no longer has is deleted and starts nothing: the pipeline runs on a cron (registration writes `"{}"`), dropped the asset from its schedule, has no registry entry, or its state machine is gone (a destroyed pipeline keeps its registry entry, so its row goes the first time a fact would start it, with one `push` warning). The delete is conditional on the `registered_at` it read, so a pipeline that registered again meanwhile keeps its row. A schedule that cannot be read, or none on a deployed pipeline, is only warned about: deleting the row would leave a pipeline that never runs again to re-register.
  - If satisfied (or `cascade = all`), start the consumer with a deterministic name (§6.6); `ExecutionAlreadyExists` counts as done; a permanent error (`StateMachineDoesNotExist`) drops the item with one warning; a transient error writes a push retry row (§7).
- **Isolation.** A failure for one subscriber or consumer never fails the record. A record raises only if even the retry row cannot be written, and then the stream retries until the table is reachable again.

**Writers only write the fact**, and no writer notifies a consumer: the stream relays every write (*Key design choices*). A fact belongs to its success: `run_task` and Mark Successful write it in the same transaction as the status, so neither can succeed without its facts (ADR #137 invariant 12).

#### 6.2 Task completion → `notify_dependents`

Superseded by ADR #137 §7.

#### 6.3 Registration

Superseded by ADR #137 §1.

#### 6.4 Wrapper (owns post-wait status)

Superseded by ADR #137 §1.

#### 6.5 Resume, restart, auto-skip

Superseded by ADR #137 §3.

#### 6.6 Run names

One helper, `run_name(pipeline, date, kind, discriminator)`, names every non-scheduled pipeline run. The final 8 characters are a hash of `(pipeline, date, kind, discriminator)`, so the task-execution names derived from them are unique across pipelines, kinds and tiers. The whole name is at most 80 characters, truncating the pipeline name. A `run_task` execution's name derives from the row key, the run's `StartTime` and the generation (ADR #137 invariant 4).

| Run kind | Discriminator |
|---|---|
| push AND | none: one run per consumer and date, even after the spec changes |
| push OR / `cascade=all` | `(asset, event_time)`: one run per event |
| backfill | backfill id + tier |

### 7. Indexes

**`attention-index`** on `pipeline-tokens`: PK `attention`, SK `attention_at`, where `attention_at` is a due time, not a creation time. Its projection includes `status`, `task_name`, `pipeline_name`, `pipeline_execution`, `task_date`, `kind`, `error`, `finished_at`, `started_at`, `attempts`. Each kind is sharded `{kind}#{0-f}` by the first hex digit of a hash of the row key. The kinds of task and run rows are in ADR #137 §6; besides them:

| `attention` | Rows | Leaves the index |
|---|---|---|
| `retry#{0-f}` | push retry rows `_retry_push_{h(consumer, date or event)}` | deleted on success; exhausted after 24 h, with a warning; TTL 7 days |
| `warn#{0-f}` | warnings (§9) | TTL 8 days |

**`asset-partition-index`** on `asset-events`: PK `asset_name`, SK `partition_date`; projection includes `event_time`, `partition_key`, `granularity`, `cascade`, `source_dag`, `uri`.

Amended by ADR #137: no `paused#` entries, and a task row has a due time only for a time of its own (ADR #137 invariant 9).

### 8. Sweeper

An EventBridge Scheduler runs it at `rate(5 minutes)` with no scheduler retry; the function timeout is 240 s.

**Lease.** The `_sweeper_lease` row (`owner`, `expires_at = now + 270 s`) is taken with a conditional put that succeeds if the row is absent, expired or already its own, so a put resent after a lost answer passes. Only its owner releases it. A run that cannot take the lease exits normally; a sweeper that stops reporting is a health signal (§9).

**Each run, within a 200 s budget,** runs each step in its own `try` (Rule 52):
1. **Due rows.** One full pass of the dispatcher over each run with due entries, earliest due first (ADR #137 §7), within the `DescribeExecution` budget (`sweep.DESCRIBES`, ADR #137 §1).
2. **Push retries.** Due `retry#` rows are retried with backoff (5 min → 1 h).
3. **Push catch-up,** hourly. For every subscribed asset, read the facts of the last 48 hours (consistent base-table query) and replay AssetFanout's push branch for each: same date, same rule, same `cascade` handling, same deterministic names, the same `subscribed_at` rule (§6.1). A fact the replay cannot read is skipped with a log line (the stream warned about it when it was recorded). A run that reaches its budget keeps its place, down to the last fact it replayed, and the next run goes on from there (`catchup_within`).
4. **Engine self-check,** last: the event source mappings `Enabled` and their last calls not failed (`LastProcessingResult`), the streams enabled, the oldest overdue `waiting` row, the push retry backlog, the steps of this run that failed. The results go to `_engine_health` (§9). A failing step stops neither the later steps nor the self-check, and the next run tries it again.

Amended by ADR #137: the pause-release step and the adoption scans are gone, and lost hints are judged per run after a grace (ADR #137 §7).

### 9. Failure handling and visibility

**Three levels, with no extra infrastructure:** no custom metrics, no CloudWatch alarms, no SNS, no DLQ (Principle #45). Polyris users have no on-call team; alarms would need setup, a reader and money.

1. **Heals itself in code.** Retries, the sweeper, push retries and catch-up, idempotent writes and starts. No human is needed.
2. **Fails loudly in code.** A non-transient coordination error fails the affected task with the real cause (§3), where the user already looks, and the user re-runs it after fixing the cause. ADR #137 §6 lists what fills this level.
3. **Engine health is shown in the console.** This covers the case where no code runs at all (schedule removed, event source mapping disabled, stream disabled).
   - The sweeper writes `_engine_health`: last run, each event source mapping's state, last result and last change, the streams' state, the oldest overdue row, the push retry backlog, the steps that failed, its catch-up progress, and the names of the resources it checked (with the region). A reading it cannot take is listed with the failed steps, so the row is still written. A coordination error reaches the bell as a warning, not this row.
   - `/api/health` adds a `coordination` section and judges staleness itself: no sweeper run for 15 minutes means the engine is unhealthy.
   - One pure function, `sweep.engine_findings`, turns the report into findings, each with a title, the resource it concerns, its impact, its fix and, when every name is known, a command. `/api/health` returns them to a verified caller; anyone else gets the status words only (ADR #65, API tokens, *Health details for verified callers*).
   - The console shows a **System** pill in its header: green, amber "needs attention" with one toast per new problem, or grey "status unknown" when it cannot read the answer. Its panel lists the findings.
   - The rules that keep it honest: the core writes the final English and the UI never branches on the kind of finding; the pill takes its level from the status words, never from the length of a list the short answer leaves out; DynamoDB latency (`degraded`) and task failures do not colour it, because task failures belong to the bell; a mapping that a deploy is creating or updating counts only after 15 minutes in that state; the anonymous answer is built from an allowlist. "Since when" is shown only where AWS keeps the time (the mapping's `LastModified`); no state is kept across runs for it. A banner on every page was rejected: it alarmed without saying what it concerned or what to do.

**Warnings.** One helper, `records.warning_item(*, kind, execution_name, task_name, pipeline_name, pipeline_execution, task_date, error, now)`, builds the row `_notify_warn_{kind}_{execution_name}`; SFN templates and generated ASL write the same shape, pinned by contract tests. `kind ∈ WarningKind` (`coordination`, `push`, `sweeper`, `manual` for a manual action that kept a task's real output, `step`, ADR #137 §9); `attention = warn#{h}`, `attention_at = now`; TTL 8 days. The bell reads warnings from `attention-index` by write time, deduplicated per execution and kind.

**The enabling time,** from which a wake's latency and a lost hint are judged (Principle #48; `trigger_rules.enabled_at`, `Evaluation.enabled_at`):
- for task dependencies, the latest required `finished_at` (for `one_success`, the earliest success; for `one_failed`, the earliest final failure, ADR #138 §2);
- for assets, the newest required `event_time` (for an OR group, the earliest member that satisfies it);
- in both cases, no earlier than `started_at`, and never before the resume time.

Targets: the primary path p99 ≤ 30 s; through the sweeper ≤ 40 min. `TimeToWake` is measured from the logs, during smoke and game day only.

### 10. Packaging

- `decide`, `asset_conditions`, `trigger_rules` and `partition_date` live in `polyris/coordination/`, under the 100% pure-logic coverage floor. `polyris/local.py` uses the same `trigger_rules`.
- The coordination Lambdas and the console import them from the polyris release their `requirements.txt` pins (ADR #102). `main` moves only with a release, `scripts/release.sh` moves both pins with the version, and a deploy from source points both at the checkout. `tests/sdk/test_lambda_sdk_pins.py` checks that both pins name the repository's version and that the release script moves both, and the CI trial archive builds with the checkout's SDK and imports every function.
- One CodeUri, `sam/lambdas/coordination/`, holds the coordination functions, each with its own role. What the dispatcher needs is one managed policy (`polyris-reconcile`) that every role running it attaches; each role adds only its own needs. A test records every call the code makes and compares it with the roles.

## Invariants

Each has a test (`tests/backend/coordination/README.md`). ADR #137 *ADR #130 after this ADR* maps them onto its own invariants.

1. Retired by ADR #137: no token is left to guard.
2. **No start without consistent reads of the task facts in the same pass.** Asset facts come from the partition index plus the known facts; their bounded staleness is covered by the sweeper.
3. Superseded by ADR #137 invariants 2 and 7: each transition has one writer, and every write is conditional on what its writer read.
4. **The `asset:{name}` pointers of a row are deleted only once the row has left its dependency wait** (Rule 40).
5. Replaced by ADR #137 invariant 9.
6. **Facts are never overwritten.**
7. **Every asset-event INSERT is processed by AssetFanout.** Pull waiters are also covered by the sweeper, and push consumers by retry rows and the catch-up.
8. **Push runs at most once** per (consumer, date) for AND and per (consumer, event) for OR, and **at least once** within the catch-up window for each fact recorded after the consumer subscribed.
9. Superseded by ADR #137 invariant 5: a write made for an owner whose generation the row no longer holds changes nothing.
10. Superseded by ADR #137 invariants 6 and 8: a run whose rows are all final has finished, and a `stopped` row counts as final.

## Key design choices

- **One function decides and acts.** Six wake paths with different status semantics produced most of the defects.
- **Three failure levels instead of monitoring infrastructure** (§9). Recover automatically; fail the affected task loudly when recovery is impossible; show engine health where the user already looks.
- **Due-time sweeper.** A row with a time of its own carries its next check time, and the sweeper does only what is due: no starvation, bounded cost.
- **The fact table is the outbox.** The stream relays every write from any present or future writer.
- **Rule B with a partition index.** As-of semantics need a range read by partition date. An index answers it exactly, for every granularity and every backfill key format, without relying on write times or on validating every writer.
- **One run-name helper** makes the uniqueness of derived names a property of the code, not of each caller.

## Consequences

- `wait_for=[a, b]` waits for both; mixed and nested specs are exact; backfills wait for their own partition; weekly and monthly assets satisfy daily consumers with their latest version from the current or previous week or month.
- Push consumers whose inputs have different cadences run at the cadence of the finest input; a push AND cannot lose a day; producers never fail because of a consumer.
- **Limits** (constants in the code): the sweeper runs every 5 minutes for at most 200 s; AssetFanout gets 60 s per invocation and keeps 5 s in reserve inside a record (§6.1); the streams keep records for 24 hours; a push start that keeps failing is retried for 24 hours, then given up with a `push` warning; the push catch-up replays the facts of the last 48 hours, hourly.

## Rules for the code

The lessons of this design's defects, as rules for whoever changes the coordination code. Root `CLAUDE.md` keeps one line per rule under the same numbers, which code and tests cite. ADR #137 *ADR #130 after this ADR* restated them for tasks as rows; the text below is the rule in force.

**Rule 40. Delete a row's `asset:{name}` pointers only once the row has left its dependency wait.** A pointer says "wake this waiter"; deleted too early, nothing re-checks the waiter. The dispatcher deletes them once the row leaves `waiting` and `waiting_paused`, and a restart writes them again.

**Rule 41. Wake-up hints may be lost; wake-up decisions, status writes and fact writes may not.**
- **Hint paths** only say "check this waiter again": the two streams and the end of an execution (ADR #137 §7), with the sweeper's checks of the run rows behind them. They may fail or drop a hint, because the periodic re-check from facts recovers it.
- **Decision and action paths** never fail silently: the dispatcher's writes and starts, the run's answer, and every write of a fact, which shares a transaction with its status. They retry, fail the affected task with the real cause, or surface a console warning (Principle #38). A fact is state, not a hint: nothing re-checks a fact that was never written.

Anti-pattern: a wake-up path whose loss nothing re-checks, for example a fire-and-forget invoke with no periodic re-check behind it.

**Rule 43.** Retired by ADR #137: no token signals a task. The dispatcher writes the transition it decides before it starts `run_task` (ADR #137 invariant 3).

**Rule 46. The run's token, which `Start_Run` hands over through the run row, is findable by the sweeper, and a failed write fails `Start_Run`.** The run row is due at least until its current token is answered (ADR #137 §3), and a new token raises `run_version`, so no pass removes the run row's due time on a read taken before it. Any other token handed over through a write follows the same rule: the same write makes it findable, and a write that fails fails the token with the cause.

**Rule 47. A task row has one owner at a time, named by its generation.** Every write made for an owner carries `ownership.FENCE` with the owner's generation; every write that ends an ownership raises it with `ownership.RAISE` in the same update that records the decision. A fenced write ends its writer quietly: no status, no notification, no work. The writers are `Start_Run`, the dispatcher, `run_task` and the console's builder; `tests/sdk/test_task_row_writers.py` lists every writer of the tokens table, and a new one fails it until it has a role there. A retry after a lost answer must accept the row it already moved (§2.1): test such a write by applying it twice.

**Rule 49. A platform function that a pipeline's state machine calls reads each format it supports and refuses any other, with the cause** (`Start_Run`'s arguments, ADR #137 §1). A key added to `run_task`'s input after a release is read through `$exists` for one release: one stack updates the dispatcher and `run_task`, but not at the same moment. Default such a field to a value its readers already treat as absent, and pin it with a test that runs the template on the older caller's input.

**Rule 50. A time that marks a beginning is written once.** When the writer runs again, as registration does on every run, write the field with `if_not_exists`. Otherwise it moves forward and silently narrows whatever it gates: `subscribed_at` decides which facts a push consumer reacts to, and a moving one would hide the facts a lost stream record left for the catch-up.

**Rule 51. A reader of shared rows skips a row it cannot parse, and says so.** One malformed row must not fail every decision that reads the table. Catch the parse error per row, count the row for nothing, and write a warning. `FactStore.read` does this for asset facts, the sweeper for push retry rows, and `/api/health` for the engine health row, which it names as unreadable instead of failing the whole check.

**Rule 52. A time budget is checked per item, and a periodic job's steps fail independently.** A budget checked only between batches lets one large item run the function into its timeout: AssetFanout checked it between records, so a record with many waiters was retried for 24 hours and blocked its shard, and the catch-up checked it between assets, so an asset with many facts never finished. Check the deadline before each item, give up the rest as lost hints where a re-check exists (Rule 41), and keep a cursor inside the item's group (`catchup_within`). A periodic job runs each step in its own `try`: a failing step must not skip the later steps or the self-check, and the health report names it (`sweep.STEPS`, `failed_steps`). Every pass of the dispatcher, narrow or full, has a time budget below its function's timeout (ADR #137 §7).

**Rule 55. An evaluation as of a time reads only what was recorded by then.** A push judges a consumer's schedule as of its fact, and the stream, a redelivery and the hourly catch-up must give one answer. Reading the current facts with only the clock set back is not that: a fact recorded later still counts, and the catch-up then started a consumer that a `cascade='none'` backfill had completed (ADR #57). Pass the cutoff to the evaluation (`recorded_by`), so that it drops the later facts from both the reader and the known facts. The cutoff is the writer's stamp, taken before the write commits, so two facts stamped within a write's latency of each other can commit in the other order; if the later one's record is processed before the earlier one commits, neither stream push starts the consumer, and the hourly catch-up starts it instead.

## Out of scope

Each can be added later without rework: queue-based fan-out at scale; hourly partition identity below a day; push notifications about engine health (an integration on top of `_engine_health`); subscriptions keyed by partition, not by asset name (a backfill multiplies AssetFanout's work); a producer run id on each fact, so a fact write retried after a lost answer cannot record a second fact; a share of the sweeper's budget for each step, and a catch-up that replays only the facts since its last pass; warnings on read paths at most once an hour per row.

## Rejected alternatives

- **Accumulated flags with more transitions** (the unmerged AND-gate draft): it produced four hangs while being built, and it keeps the cause (Principle #42).
- **CloudWatch alarms, SNS and a DLQ:** they need setup, a reader and recurring cost, and they leave recovery to a human.
- **Event-time read bounds plus writer-side date validation:** fragile with future-dated runs, mixed time formats and non-daily partition keys; the partition index answers exactly.
- **Rule A** ("an event covers its calendar period"): it makes consumers wait for end-of-period producers.
- **Fixed-day periods** (7 and 31 days): they leave days that can never be satisfied for irregular schedules.
- **Push keeps same-date semantics:** a consumer's cadence is set by the grain of its own output, so the finest input drives the run, with coarser inputs as reference data.
- **Rule C** (forbid mixed cadences): it pushes the problem to users and is inexact for backfills.
- **Merging the subscription tables:** the per-run key fixes collisions without it.
- **Writer-published EventBridge or SQS events:** writers would still have to remember to publish.
- **Reserved concurrency for the sweeper:** it fails deploys on low-limit accounts; the lease prevents overlap.

## Testing

The test strategy, scenario matrix, invariant harness, live smoke and game day are described in `tests/backend/coordination/README.md`. Two requirements are normative:
- every invariant has a test;
- every defect this layer fixed has a scenario that is green after the change, and a mutation probe that re-introduces the defect and makes that scenario fail. The ids are the ones the scenarios carry (`tests/backend/coordination/defects.py`).
