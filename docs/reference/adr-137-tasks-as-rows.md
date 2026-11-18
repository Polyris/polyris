# ADR #137 — Tasks as rows: one dispatcher starts the tasks, one token per run

> **Status:** ACCEPTED (2026-11-17; proposed 2026-11-15), revision 9 (2026-11-18: the results of TR-0's probes; revision 8, 2026-11-17: the plan review's decisions). Target release: 1.3.0.
>
> **Will supersede, once accepted:**
> - ADR #130 in part: *ADR #130 after this ADR* lists each section, invariant and rule.
> - ADR #135, decision 1 and its table, the rule that whoever ends a task answers its run. Its decisions 2–4 carry over to the one place that answers a run now (§4), except that a `stopped` row now counts as finished.
> - ADR #2: dependencies are no longer resolved through `notify_dependents` and task tokens.
> - ADR #3: the helpers that the wrapper was split into go with it.
> - ADR #20, in part: a waiting task leaves Step Functions, and the polyris console shows why it waits (§1).
> - ADR #121, decisions 1 and 2: no wrapper is left to stop, and Restart and Mark Successful take the task's spec from its spec version, not from the registry.
> - ADR #120, decisions 4 and 5: the restart helper's field and the `attempt` conditions go with the wrapper; the generation fences instead.
> - ADR #23: a run's graph comes from the spec version that its run row names (§8). New runs write no DAG snapshot, and the console reads the snapshots of 1.2 runs until 1.4 (*Migration*).
> - ADR #112, decisions 2 and 5 and the reconciliation in decision 7, and ADR #136, decisions 3 and 5: no console route checks a run against Step Functions (§5). ADR #136's API stays, and its `stats_incomplete` now means a start day that could not be read.
>
> **Will amend, once accepted:**
> - ADR #114: its decision stands, and a failed task still waits for a human decision. What follows a final failure changes (§4). This is the ADR of its own that ADR #114 asked for before a run stops aborting on a failure.
> - ADR #112: a run with a failed task stays `running`, with the number of its failed tasks, until every row is final, and a failed direct step makes the run `failed` (§4).
> - ADR #120, decisions 2 and 3: the output marker and Mark Successful's asset facts join the action's transaction (§3).
> - ADR #123: a task takes each upstream's status from its own run, and an output only from an upstream that succeeded or that an operator or the run skipped (§1). Every write to `output#` renews its TTL for a year, and `input#` lives 90 days from each write (§5).
> - ADR #125: `run_task`'s `attempt` guard goes; the generation and the execution name replace it. The counters the console shows stay.
> - ADR #107: the work states no longer retry `States.Timeout` on their own, so a timeout is one failed attempt of the task's retry loop (§1).
> - ADR #108, `TaskConfigKey`: the contract it pins runs from the SDK through the task spec (§8) to `run_task`, whose input the dispatcher builds (§9). Two ADRs carry the number 108, this one and the inline one on the runs feed below; renumbering one of them is a separate docs task.
> - ADR #22: its one exception goes. The console no longer reconciles a run's status against Step Functions, and `reconcile_execution_status` goes with the statuses `timed_out` and `recovered`; the dispatcher judges an ended execution (§4).
> - ADR #18: the Express helpers and the wrapper go; `run_task`, `bulk_backfill` and the test state machines stay Standard.
> - ADR #19: a waiting task costs nothing because it is a row, not because it waits on a token.
> - ADR #54: `bulk_backfill` waits for each run as long as the run lives, not 24 hours (§5).
> - ADR #51: a backfill record carries a `date`, and lives while its backfill runs and 90 days after, instead of 30 days from its creation (§5).
> - ADR #108, *A pipeline's runs are read through an inverted GSI*: the Runs tab, without a filter or with a pipeline filter, reads a sparse index of run rows instead of task rows (§5).
> - ADR #39: asset data comes from the specs of each pipeline's `spec_id`, not from the registry's `tasks` (§8).
> - ADR #110: `extend` leaves the execution controls, with its route (§5).
> - ADR #131: `polyris-up` checks a platform older than 1.3 before it upgrades it, and lists the pipelines to redeploy (*Migration*).
>
> **Related:** ADR #138 reworks the trigger rules for this run end and counts `stopped` as a failure for them. It supersedes ADR #116, ADR #117 and parts of ADR #115.
>
> **Unchanged:** what *ADR #130 after this ADR* marks unchanged, and the DSL apart from the time-limit defaults (§5), the check of duplicate outlets (§8) and the removals under *Consequences*.

## Context

ADR #130 made readiness a decision taken in one place, from facts, with hints that may be lost and a sweeper that checks again. That layer is right, and it carries over. The defects that the 1.2 reviews and the release rehearsal kept finding come from the layer under it: every task is a live `dependency_wrapper` execution from the start of its run, waiting on single-use tokens.

| Class | Evidence | Why a process per task produces it |
|---|---|---|
| Who owns a task row | B-O1…B-O5, B-W1 | A wrapper lives on after an operator decided its task, after a restart replaced it and across an upgrade. The row has many writers: the wrapper, registration, `run_task`, the pause wait, the failure handler, the restart helper, the console and the reconciler |
| A run left `RUNNING` | F16, Z1, B-RN1…B-RN4 | Every task holds a token of its run, and six places must answer one (ADR #135) |
| Zombies and liveness deadlines | ADR #130 §3 step 3, `sweep.liveness_deadline` | A `waiting` row means "a live process somewhere". The sweeper can check that only through `DescribeExecution`, which lags |
| A token handed over through a write | B-P3, B-P4 | A task held by a pause is a `run_task` execution parked on a pause token with a 12-hour heartbeat |
| Restart | B-O2, B-R1…B-R3 | A restart stops two executions, hands the row over and starts a third, and each step can fail on its own |
| Registration as a process | B-G1, B-G2, B-S1 | A separate execution creates the row and must then signal the wrapper |
| Versions in flight | B-O3, the adoption scans, `ownership.is_legacy`, the harness's frozen v1.1.4 wrapper | An execution runs for days on an old definition and calls the new helpers |
| Writes after success | B-F3 | Status, output, facts and the run's answer are separate states with separate catches |

Principle #42 has triggered. In the external review of 1.2.0, the class "a process ended and told no one" produced F16, B-RN1, Z1 and two more cases. ADR #135 closed it with one rule ("answer twice"), and the generation closed the ownership class with another (ADR #130 §2.1). Every writer must follow both rules, and the model that needs them stays. This ADR changes the model.

## Decision

### 1. The model

A waiting task is a row in the tasks table (`pipeline-tokens`) and nothing else: it has no execution and no token. An execution exists only while the task's work runs. Each run has one row of its own, the run row, which holds the run's only token.

```text
1.2: a task is a process

  pipeline --starts--> dependency_wrapper(load)         lives for the whole wait
                         registration, a separate execution: row, pointers, reconcile
                         waits on its wait_token, for hours
  extract ends --> notify_dependents --> reconcile --> SendTaskSuccess(load's wait_token)
                         the wrapper claims the row, then starts run_task(load)

1.3: a task is a row

  pipeline --> Start_Run: writes the run row (creating, with the run's token),
               the run's input and every task row (waiting), then marks the run active

  the run turns active  --> tasks-table stream  ---\
  extract's row changes --> tasks-table stream  ---+--> dispatcher: a pass over the run, decide(load)
  a fact is recorded    --> asset-events stream ---+      ready --> writes deps_ready --> starts run_task(load)
  the sweeper, as a net ---------------------------/
```

- **`Start_Run` writes the run.** The generated pipeline state machine keeps its registration chain: `Register_Pipeline` and the asset subscriptions (§8). Then it runs `Start_Run`, a Lambda invoke that waits for a task token (`lambda:invoke.waitForTaskToken`), in a `Parallel` beside the direct steps.
  - **The specs come from a table.** `polyris-deploy` writes every task's spec to `pipeline-specs` before it changes the pipeline's stack, and the state's arguments carry only a reference to them (§8). `Start_Run` reads and checks the specs. It writes on each task row only the fields that the dispatcher decides on, with the hash of the task's spec text, and on the run row the `spec_id` (§3). The full spec stays in `pipeline-specs`, and the dispatcher reads a task's spec when it starts that task (below). The dispatcher, Restart and Mark Successful never read the registry.
  - **The run's input is an item of its own.** `Start_Run` writes the run's `variables` and `skip_tasks`, and the flags that set the cascade of its facts (ADR #57), once, to the run's input item (§3). `triggered_by`, a short value that every run list shows, stays on the run row. Nothing writes it again. Task rows keep no copy of it, apart from the few fields that the console reads on them (§3).
  - **The format comes with it.** The arguments carry the pipeline's format version, which `Start_Run` checks before anything else and stamps on the run row. It refuses a format it does not support, with the cause (*Migration*).
  - **The run row first, active last.** `Start_Run` writes the run row as `creating`, with its token. It updates the row and never puts it, so a direct step's mark written before it survives (below). Then it writes the run's input and one row per task: `waiting`, or `skipped` with `skip_origin = run` for a task named in the run's `skip_tasks`. That list is the only one it reads, as the wrapper did (ADR #60); it does not read `skip_on_backfill`, since whoever starts a run names the tasks to skip. Then come the `asset:{name}` pointers of the tasks with `wait_for`, and last the run row is marked `active`, unless it carries the stop mark (§4). Nothing starts for a run whose run row is missing or still `creating`, so a `Start_Run` that fails half-way starts no task.
  - **Conditional writes.** Each row is a conditional put, because a batch write takes no condition, and `Start_Run`'s timeout fits the largest pipeline that `polyris-deploy` accepts (§8). Every row carries the execution's name and `StartTime`, and a pass or a stop acts only on the rows of its own run. An existing row is accepted only when it carries this execution's name and `StartTime`, so a retry or a redrive writes nothing twice. A row of another run fails `Start_Run` with the cause, as `ExecutionAlreadyExists` failed a second run of the same name in 1.2.
  - **One token per run.** Each attempt of `Start_Run` brings a new token. The run row keeps the token of the latest attempt, ordered by `State.RetryCount`, and after a redrive by `Execution.RedriveCount` first (§9). In TR-0, `State.EnteredTime` was the same for every retry of the state, so only these counters order the tokens. A later attempt's token replaces the stored one, clears its answered mark and raises `run_version`, so no pass ends the run's checks on a read taken before it (§3). An earlier attempt's token never replaces a later one, and neither moves the run row back to `creating`. The stop mark refuses both a new token and the activation, so an attempt that comes after the engine stopped the run fails into the `Catch`, with a warning (§4).
  - **It returns once the run is active.** The activation wakes the dispatcher through the tasks table's stream (§7), and its first pass starts the root tasks. That pass answers the token at once when every row is final already, for example when every task was skipped.
  - **A redrive** that runs `Start_Run` again finds the rows in place and writes none of them twice. Its newer token makes the run row due at once, and the sweeper's next pass answers it from the rows as they are: a run whose rows are all final ends again within 5 minutes, with their outcome. TR-0 showed both cases. A redrive of a failed run runs no `Start_Run`: the execution fails only after the `Parallel` (below), so it fails again at once, and the documented way to run a failed run's tasks again is Restart. A redrive of an aborted run runs `Start_Run` again, with `RedriveCount` 1 and `RetryCount` 0, so the order by `RedriveCount` first stays.
  - **Retry.** `Start_Run`'s `Retry` treats Lambda throttling (`Lambda.TooManyRequestsException`), `Sandbox.Timedout`, `Lambda.Unknown` and HTTP 5xx as transient (Principle #53). No entry matches the error name with which the dispatcher answers a failed run (§9). Its timeout and retry window derive from the declared maximum number of tasks per pipeline (§8). The sweeper waits for a run row still `creating` as long as the whole `Retry` envelope, throttling included: every attempt's timeout and every backoff. One parity test pins that window, the template timeouts of `Start_Run` and of the dispatcher, and the pass budgets below them (Principle #28).
  - **Catch.** A `Start_Run` that fails for good starts no task, so its `Catch` makes the failure visible. It writes a `coordination` warning with the run and the cause, built like every warning row of the generated state machine. Then the branch records the error, as a direct step's branch does, and the pipeline fails after the `Parallel` (below): a failure inside the `Parallel` would cancel the direct steps still running. This covers retries used up, a refused format, specs that are missing or do not match their fingerprint (§8), rows of another run under the same name, an input over 256 KB, and an attempt that finds the stop mark. The `Catch` also names `States.DataLimitExceeded`, which AWS documents as outside `States.ALL`; in TR-0, `States.ALL` took it from an oversized result too. The dispatcher's answer of a failed run is not such a failure: a `Catch` ahead of it takes that answer by its error name, and the branch records it like a direct step's error, without a warning.
- **One dispatcher decides and acts.** The `reconcile` function of ADR #130 becomes the dispatcher, under the same logical IDs and names (*Migration*). Its one entry point is a pass over a set of a run's rows (§7): the stream and the sweeper pass over all of them, and AssetFanout over the rows that wait for a new fact. A pass reads the run row and the rows of its set once, with consistent reads, and decides each waiting row with `decide`. Then it writes what it decided:
  - `ready`: it writes `deps_ready`, then starts `run_task`;
  - `skip` or `upstream_failed`: it writes that status;
  - `paused`: it writes `waiting_paused`;
  - `wait`: it records the reasons from `decide` and the row's next deadline, with the time of the decision, only when they differ from those the row holds. The reasons hold no time, such as the age of a fact, so a pass over a run where nothing changed writes nothing (*Cost*).

  Each decision that moves a row is one transaction: the row's write, conditional on the status and the generation the pass read, and a `ConditionCheck` on the run row's `run_version` as the pass read it. Every write that can make such a decision wrong raises `run_version` in its own transaction (§3). Those are an operator's action, a reset of a Restart with dependants, a new token, a Pause or a Resume, and every request, the stop mark included. So no decision stands on a read taken before such a write, and the transaction holds two items, whatever the number of upstreams.
  - The same check keeps a `deps_ready` write out of a run that a Pause (§5) or a Stop (§4) reached after the pass's read. A pass that read the run row paused or being stopped writes no `deps_ready` at all.
  - A refused transaction is decided again from a new read of the run row, the row and its direct upstreams, within the pass's budget.
  - Such writes are few: a run sees a handful in its life, besides the actions of its operators. A decision is therefore refused only when one of them lands between a pass's read and its write, and during a Restart with dependants about once per reset.

  Last, a full pass decides the run's end and writes the run's summary when it changed (§4). It signals no token, because no task holds one.
  - **It owns `wait_before`.** A ready task with a countdown stays `waiting` until `enabled_at + wait_before` (`trigger_rules.enabled_at`). The row records the countdown's end, which is its due time and the console's countdown. No pass decides the row again before then. At the end a pass decides it again from its upstreams, without judging a `.within()` condition again, so an upstream restarted during the countdown holds it. A pause holds it like any other ready task (§5).
  - **It reads a task's spec when it starts the task.** One `GetItem` on `pipeline-specs`, by the hash of the spec text on the task's row, gives the full spec. With the run's input item, read once per pass that starts tasks, the dispatcher builds `run_task`'s input in Python (§9). A text never changes, so a restart runs the same spec.
  - **It passes on the upstreams' statuses.** It starts `run_task` with the statuses of the task's direct upstreams in this run, with their skip origins, as the pass read them.
  - **A start that keeps failing.** A transient error leaves the row `deps_ready`, due again after a backoff of up to 5 minutes. The row records when its first and its latest attempts failed, from which the due-time function gives its next try. A start that has kept failing for 15 minutes writes one `coordination` warning, and one that has failed for 6 hours fails the row with the last error (§5).
  - **It writes the timeline.** `DEPS_READY`, `DEPS_SKIPPED` and `DEPS_BLOCKED` follow the status, best effort, as the wrapper writes them today. A row's `task_run_id` is the name of its `run_task` execution (invariant 4), so each generation has a timeline of its own.
- **Each pass judges a row from the row itself.** The deadline function and the due-time function are pure (§9): they read the row, its upstreams and the run row, never the row's stored due time. So a pass acts on each row of its set whose time has come, whether or not the row's own due time is there:
  - it fails a row past its limit or its decision deadline, and decides again a row whose countdown has ended;
  - it starts again a `deps_ready` row whose next try has come, unless the run is paused or being stopped (§5);
  - in the sweeper's passes only, it judges the execution of a `deps_ready` or `running` row whose liveness check has come. A stream or narrow pass makes no `DescribeExecution` call, so concurrent passes never judge the same rows twice.

  A narrow pass judges only the rows of its set, not the upstreams it reads with them. Each pass also compares the stored due time of each row of its set with the due-time function, and writes the difference: it adds, moves or removes the due time.
  - A removal or a postponement that a pause causes carries the same `ConditionCheck` on `run_version` as a decision, so a Resume that lands after the pass's read keeps the due time (§5).
  - Any other race costs one extra check, or a due time missing until the next full pass: at the latest its run row's check, within 30 minutes (invariant 9). A row's own due time is therefore only a wake-up for precision: it makes the row's check punctual.
- **The console shows why a task waits.** The task detail shows the reasons and the deadline that the dispatcher recorded, with their time. It also shows each upstream's verdict, which the console computes with `trigger_rules.evaluate` from the rows it already loads. No route is added. This replaces what the Step Functions console showed of a waiting task (ADR #20).
- **`run_task` only runs the work.** Its first state moves the row from `deps_ready` to `running` under the generation it was started with. It no longer checks the pause, waits for a decision, notifies dependants or answers the run.
  - **What it reads from its upstreams.** It reads an upstream's output from `output#` only when that upstream succeeded, or an operator or the run skipped it. An upstream that succeeded has an `output#` item, written with its success. When that item is missing, `run_task` fails the task with this cause, instead of starting the work without the upstream's output as 1.2 did. Any other upstream reaches the task as its status alone, so a task that runs after a failure never reads an older run's output of the same date. `run_task` passes the statuses on to the task: in the event's `upstream` entries, and for Glue, ECS and Batch tasks in `POLYRIS_UPSTREAM_STATUSES`, beside `POLYRIS_TASK_NAME` (§9). Through `xcom`, reading the output of an upstream that failed finally raises `XComUpstreamFailedError`; its status stays readable. An EMR step gets no environment variables (ADR #123), and so no statuses: after an upstream that failed, `xcom` can give it an older run's output of the same date. This is a documented limitation, and `run_task` adds nothing to the step's arguments, which the user's program parses.
  - **The work's time limit.** `execution_timeout` is the `TimeoutSeconds` of its work states, for one attempt at a time. A timeout is a failed attempt, which goes through the task's retries and then to the decision of ADR #114. The work states no longer retry `States.Timeout` themselves: `Run_Task_SFN` and `Run_Task_Lambda` do today, so one attempt could run three times, and a test pins the removal. A timeout ends the state, not always the work: a Lambda invocation runs on until its own timeout. A `.sync` job is stopped by Step Functions: in TR-0, Glue, ECS, Batch, Athena and a nested execution stopped within 15 to 60 seconds of the timeout; EMR was not probed. So a retry after a timeout waits at least 60 seconds, and two copies of the work do not overlap.
  - A task that fails after its retries is written `waiting_decision`, with the time it began to wait and the decision timeout then in force, and `run_task` ends. The row's entry in `attention-index` falls due at the decision's deadline (§6). No process waits for the decision (§5).
  - **One transaction on success** writes the status, the output and the asset facts. The output is stored once, in `output#`, the store that `xcom.pull()` and the console read; the task row keeps no `result`. A dependant therefore never starts before the data it reads (invariant 12), and a success without its facts cannot happen. A transaction holds at most 100 items, so a task has at most 98 outlets, a cap that `polyris-deploy` enforces (§8).
  - After `TransactionCanceledException`, `run_task` reads the row and branches on what it finds. Its own success under its generation is a retry after a lost answer, and it stops. A raised generation ends it quietly (Rule 47). Any other cause (a conflict, a throttled item, a fact time already taken) retries the transaction, with a new time for that fact.
  - A success that still cannot be recorded makes the row `waiting_decision`, with the cause, and the work runs again only if an operator restarts it. The transaction's `Catch` also names `States.DataLimitExceeded`, so a payload too large for the transaction ends there too, not `failed`. A work result over the state's 256 KB fails the work state with that error before any transaction (TR-0), as in 1.2: a large value goes through the claim-check pattern of `DATA_PASSING.md`. Its `Retry` and `Catch` name the errors of the SDK integration (`DynamoDb.*`), not those of the optimised one (`DynamoDB.*`).
  - `run_task` keeps the calls to the notify function (`NotifyFunction`): the call when a task fails, unchanged, and the call after a success, which the wrapper makes today.
- **The dispatcher answers the run** once every row of the run is final (§4). No other code answers a run's token.
- **Direct steps stay, and their failures wait for the rows.** The steps without dependencies (`wait`, `pass`, `sns`, `sqs`, `s3` and the like) remain branches of the `Parallel`, beside `Start_Run`. A failed direct step is caught in its branch. The branch records the error and writes a `step` warning, a new `WarningKind`.
  - In a pipeline with tasks, the branch also marks the failure on the run row. A pipeline without tasks has no `Start_Run` and no run row (§8), so its branches write no mark.
  - The mark carries the execution's name and `StartTime`, so it and `Start_Run`'s writes accept each other in either order. A mark that comes first makes a run row with no status, token or `start_day`, which nothing acts on until `Start_Run` completes it. The mark writes the history TTL when the row has none (§5), and its `Retry` covers throttling and a conflict with a transaction that checks the run row (§7).
  - `Start_Run`'s branch records its own failure and the dispatcher's answer of a failed run the same way. After the `Parallel`, the pipeline fails its execution when a branch recorded an error, so the execution ends only once every branch has. The `Start_Run` state ends only when its token is answered, once every row is final, so the run's tasks finish first.
- **Wrapper steps become rows.** The `lambda`, `glue`, `ecs` and `athena` steps, which run through the wrapper today, become rows like tasks, with a task's defaults (§5). Their classes have no field for a time limit, so the 1 hour of a `lambda` step and the 24 hours of the others, which the generator gave their branches, go with the other 24-hour defaults.
- **Steps without a generator leave the DSL.** `Choice`, `Map`, `Sensor` and `ShortCircuit`, with the `Condition` helper, were exported, but no generator existed for them, so a pipeline that used one failed at generation. They are deleted with their tests. A `dag.py` that imports one gets an `ImportError`, and the CHANGELOG says why. A test fails for any exported step without a generator.
- **The wrapper goes.** `dependency_wrapper` and the helpers that served it are deleted, with no adapter and no stub (*What is removed*). `run_task`, `bulk_backfill` and the three test state machines remain.

### 2. Invariants

Each invariant has a test in the coordination harness, written before the code that satisfies it, and checked from the phase that builds its mechanism (*Tests*).

1. **A task that does not run has no execution and no token.** Its state is its row alone, a countdown of `wait_before` included.
2. **Apart from an operator's action, only the dispatcher moves a row out of `waiting` or `waiting_paused`,** with a write conditional on the status and the generation it read, and on the run row's `run_version` as it read it (§1).
3. **Write first, then start.** A row becomes `deps_ready` before its `StartExecution`, and a pass over its run starts a `deps_ready` row again once its next try has come, unless the run is paused or being stopped. ADR #130 rejected a sender that writes the status for two reasons: a crash between the signal and the write lost the status, and a consumed token could not be told from an expired one. With no token, and a start that is safe to repeat, neither applies.
4. **At most one `run_task` per row and generation.** The execution's name is a prefix of the row key and a hash of the key, the run's `StartTime` and the generation, within the 80 characters Step Functions allows. `ExecutionAlreadyExists` counts as done. With the start time in the hash, a run name used again once its old rows have expired cannot meet an old `run_task` name (§5).
5. **`run_task` writes only under its own generation.** A refused write ends it quietly (Rule 47).
6. **An action that ends a task's ownership is one transaction that raises the generation:** an operator's action, the dispatcher's work on a request, and the engine's stop of a run (§3, §4). Stopping an execution afterwards is cleanup only, because the raised generation already fences it.
7. **One pure table in `polyris/coordination/` allows each transition** (§3, Principle #48). It says, for each status, the row's kind in `attention-index`, if any (§6), and for each transition, whether it wakes the dispatcher (§7). Every write of a row takes its condition from that table and its due time from the due-time function (§9). `tests/sdk/test_task_row_writers.py` checks every writer against both, and every writer of the run row against whether it raises `run_version` (§3).
8. **A run's end is decided from facts.** When every row of a run is final, its current token is answered, and the run row records which token was answered. A closed token means it was answered already, or its execution has ended. Conversely, no token is answered and no execution ends while a row is not final, outside the stop paths of §4.
9. **Every unfinished row is covered by its run row's due time in `attention-index`.** The run row is due, at most 30 minutes apart, while its run has an unfinished row, a stop in progress or a request, and until its answer is recorded (§3). Each check of it is a full pass, which judges every row from the row itself (§1). So the invariant holds by construction: a row needs no due time of its own to reach its limit, its deadline or its next check.
   - A row with a time of its own also has a due time of its own, which makes that check punctual. Such a time is a limit on its wait outside a pause, a countdown, a decision deadline, a start to retry, or a `run_task` whose liveness the dispatcher judges.
   - Every pass writes the due times of its rows as the due-time function gives them, so the run row's check puts back a lost one within 30 minutes.
   - A row that waits only for other rows, or for a pause to end, is woken by their changes or by the Resume, and its run row's check finds a lost hint.

   This replaces ADR #130 invariant 5, which put every waiting row in the index.
10. **No fail-open.** No read failure, partial result or unknown state leads to a start, a close or a run's answer. An execution that `DescribeExecution` does not show yet is not a dead one (`sweep.EXECUTION_VISIBLE_AFTER`).
11. **A row outlives its run.** No row that a run writes expires before the run ends: its task rows, its `output#` rows, its pointers, its run row and its input (§5). Timeline events and task inputs (`input#`) are not such rows: only the console reads them.
    - When a row of an active run is missing all the same, the dispatcher fails each waiting dependant of it with the cause, counts the row as failed for the run's end, and writes a `coordination` warning.
    - A task row whose run row is missing is stopped, with the cause, by the first check or pass that reads it. That is the sweeper's check of a row with a due time of its own, as for a 1.2 row left in flight (*Migration*), or a pass that a change of another row of its run starts. Neither may come for a run whose run row was deleted by hand while it waited on a lost hint or a pause; such rows stay until their TTL.
12. **No status is visible before the data its dependants read.** A status write is the wake-up (§7). So `run_task` and every operator action that ends a task write the status in the same transaction as the output, the output marker and the asset facts.

### 3. Who writes which transition

This section is the table of invariant 7. `TERMINAL_STATUSES` and `SETTLED_STATUSES` become one set, `SETTLED_STATUSES`. *Final* means a status in it: `success`, `skipped`, `failed`, `upstream_failed` and `stopped`, and `succeeded` and `aborted` on rows written before 1.3. The *Wakes* column says whether a transition wakes the dispatcher through the stream (§7). The *Attention* column gives the row's entry in `attention-index` after the write (§6): a due time is an entry of the `waiting` kind, which a row has only for a time of its own (invariant 9).

**Task rows.**

| From | To | Writer | Wakes | Attention |
|---|---|---|---|---|
| no row | `waiting`, or `skipped` with `skip_origin = run` for a task in the run's `skip_tasks` | `Start_Run` | no | its due time, if it has one |
| `waiting` | `waiting_paused`, and back | the dispatcher | no | none in `waiting_paused` |
| `waiting`, `waiting_paused` | `deps_ready`, before it starts `run_task` | the dispatcher | no | due at its next start or check |
| `waiting`, `waiting_paused` | `skipped` with `skip_origin = rule`, or `upstream_failed` | the dispatcher | yes | none |
| `waiting`, `waiting_paused` | `failed`: at the wait limit, on a `CoordinationError`, or when a dependency's row is missing | the dispatcher; the sweeper finds the limit | yes | `failed` |
| `deps_ready` | `failed`, when the start is refused for good (an input over 256 KB) or keeps failing past its deadline (§5) | the dispatcher | yes | `failed` |
| `deps_ready` | `running`, then `success` or `waiting_decision` | `run_task`, under its generation | for `success` and `waiting_decision` | due at its next check while `running`; none on `success`; `decision` at its deadline |
| `deps_ready`, `running` | `failed`, when the execution of the row's current generation ended without an outcome | the dispatcher, which judges that execution through `DescribeExecution` at the sweeper's check of the row | yes | `failed` |
| `waiting_decision` | `failed`, at the decision timeout | the dispatcher; the sweeper finds the deadline | yes | `failed` |
| any status but `success` and `skipped` | `skipped` with `skip_origin = manual`, or `success` | the console: Skip, Mark Successful | yes | none |
| a status that is not final | `failed` | the console: Fail | yes | `failed` |
| a status that is not final | `stopped` | the console: Stop; the dispatcher: a Stop of a run (§4), and a row whose run row is missing | yes | none |
| a final status, or `waiting_decision` | `waiting` | the console: Restart; the dispatcher: the dependants of a Restart with dependants | yes | its due time, if it has one |

Every write by the console raises the generation, and so does every write that the dispatcher makes for a request or a stop. Every operator action also raises the run row's `run_version` in its transaction (below). Nothing writes `aborted` any more: every Stop writes `stopped`, a waiting task's included.

**The run row.**

| From | To | Writer | `run_version` | Wakes |
|---|---|---|---|---|
| no row, or a direct step's mark alone | `creating`, with the attempt's token | `Start_Run` | raised | no |
| no row, `creating`, `active` | the same, with the mark of a failed direct step | the step's branch (§1) | kept | no |
| `creating` | `active`, after the last task row, unless the stop mark is set | `Start_Run` | raised | yes |
| `creating`, `active` | the same status with a newer token, which clears the answered mark, unless the stop mark is set | a later attempt of `Start_Run`, or a redrive | raised | no |
| `creating`, `active` | the same status with the stop mark, which starts a stop of the run | the console: Stop of a run, while `active`; the dispatcher, for a run whose execution ended before its answer or that never became active (§4) | raised | yes |
| `active` | the same status with a request, the run row's long TTL and its due time. The request is the renewal of the history TTL, and for Restart with dependants also the reset of the task's dependants | the console: Restart, Restart with dependants (§5) | raised | yes |
| `active` | the same status, due at once when it has no due time | the console: Skip, Fail, Mark Successful and Stop of a task, in the action's transaction, also of a row already final | raised | no: the task row's change wakes |
| `active` | the same status | the dispatcher: each reset of a Restart with dependants | raised | no |
| `active` | the same status with the pause opened or closed | the console: Pause, Resume | raised | yes |
| `active` | the same status with a request cleared | the dispatcher, once it has carried the request out | raised | no |
| `creating`, `active` | the same status with its token answered and marked | the dispatcher, once every row is final (§4), or once it has stopped the rows of a run that never became active | raised | no |
| `creating`, `active` | the same status with the stop done | the dispatcher, once every row is final and the run's answer is marked (§4) | raised | no |

No transition leads back to `creating`.
- **`run_version`** is raised by every write in the table except the mark of a failed direct step, and by nothing else. The writes of the run row's due time, its summary and its TTL keep it, and so does the mark. None of them can make a decision wrong, or the removal of the due time.
- A pass's decisions check it and never raise it (§1). A pass raises it only by a write of the table: a reset, its stop mark, the answered mark, the stop done or a request cleared. Each such write is conditional on `run_version = :read`, the value the pass holds, and sets `:read + 1`.
  - When the condition holds, no other write raised it in between, so the pass goes on holding `:read + 1`.
  - When it fails, an action, a token or a request came in between. The pass reads the run row and its rows again, and decides again from the new reads.
- A pass that changes the run's summary writes it (§4), a write that wakes nothing.

**The run row's due time** is the safety check of its run's rows (invariant 9), and each check of it is a full pass over the run (§7).
- `Start_Run` sets it when it writes the run row. A request makes it due at once, and so does a pass that leaves rows for its budget or a conflict, which also sets the run row's `left_at` (§7). "Due at once" always writes the current time, a new value, so a move by the sweeper that read the old one fails.
- A Restart sets it again in its transaction, also after the run's end. Skip, Fail, Mark Successful and Stop of a task make it due at once in their transaction when it has none. So after the run's end, a lost stream record cannot leave the summary stale (§4).
- It stays while any row of the run is unfinished, a stop is in progress or a request waits on it. After each check, the sweeper moves it on by the backoff, at most 30 minutes, and clears `left_at`. It does the same after a check whose pass failed, and writes a `coordination` warning for the run (§7).
- Each move is conditional on the due time and the `run_version` that the sweeper read. So it never postpones a check that a request, a pass or an action set meanwhile, and after a write that raised `run_version` the run row stays due for the sweeper's next run.
- A pass removes it only once every row is final, no stop is in progress, no request waits and the run's answer is marked. The removal is conditional on the `run_version` the pass read, so a new token, an action or a request that lands meanwhile keeps it. A run that never became active is closed the same way, instead of staying due until its row expires.

**The run row's contract.** Its fields are named constants in `polyris/coordination/` (§9).
- **Key.** `_run_` followed by the run's `pipeline_execution`. It starts with `_`, so `is_internal_record` skips it, and every console loop over task rows skips it through `should_skip_token_row`.
- **Indexes.** It carries no `date`, `pipeline_execution` or `backfill_id`, and its `pipeline_name` alone puts it in neither date index, since both need `date` too. Only `start_day` puts it in `run-start-index` (§5), and its due time puts it in `attention-index` while it is due.
- **Fields.** `status`; the current token, its attempt order and the answered mark; the stop mark and whether the stop is done; `run_version`; `left_at`, the time a pass last left rows; the execution's name and `StartTime`; `pipeline_name`, the run's date as `run_date`, `start_day`, `started_at`, and `finished_at` with the answer; `triggered_by`; `spec_id`, `task_count` and the format version; the summary; the mark of a failed direct step; the requests; the pause intervals; the TTL.
- **Size.** Every decision's transaction checks the run row, and DynamoDB bills the whole item, so the row stays under 4 KB. It keeps at most ten pause intervals: a Pause past ten merges the two oldest, which can only move a deadline later. A Stop's reason is capped at 500 characters, and a test builds the largest row that the code can write.
- **No task names.** A pass takes them from the run's spec version, and the dispatcher keeps the names of each `spec_id` it has read, since a version never changes. A spec version read with fewer items than `task_count` is a read failure (invariant 10). 2,000 names would weigh about 120 KB.
- **The run's input** is an item of its own, keyed `_run_` followed by the run's `pipeline_execution` and `#input`. It holds the run's `variables`, `skip_tasks` and cascade flags, and enters no index. `Start_Run` writes it once (§1). The dispatcher reads it to build `run_task`'s input, Mark Successful for the cascade flags, and the console for what it shows.

**Fields that stay on task rows for the console.** Besides its status, its times and the decision fields (§8), a task row keeps `triggered_by`, which the feeds and the History tabs show, and `attempt` and `retry_attempt`, the counters of ADR #125. It also keeps `parent_execution_id`, `task_type`, `task_run_id`, `backfill_id`, the key of `backfill-id-index`, and `task_date`: its run's date again, since `attention-index` projects that field for the bell and not `date` (§6).

**Operator actions.** Every operator action goes through one pure builder in `polyris/coordination/`: Skip, Fail, Mark Successful, Stop, Restart, Restart with dependants, Stop of a run, Pause and Resume. It builds the transactions from the rows its caller read. The console sends the light ones. The dispatcher sends those of the heavy ones (below) and of its own stop of a run (§4). Pause and Resume update the run row's pause state; only the dispatcher moves rows between `waiting` and `waiting_paused`.
- An action that ends a task is one transaction (invariants 6 and 12). It writes the status, conditional on the status and the generation it read, and raises the generation. It writes the output marker that dependants and the console read, and for Mark Successful the asset facts of the outlets in the task's spec. Skip writes `skip_origin = manual` in the same transaction.
- The same transaction raises the run row's `run_version` and makes the run row due when it has no due time (above), also for a Skip or a Mark Successful of a row already final. So a pass that read the task before the action decides none of its dependants from that read (§1, ADR #138 §4).
- The marker's write carries its own condition, that `output#` is still as the caller read it (Principle #31). When the caller finds real output there, the marker stays out and a `manual` warning says so, as today. A concurrent `xcom.push` cancels the transaction, and the caller builds it again from a new read.
- A transaction refused because a row's status moved under it is built again from a new read, while the action still applies.
- A restart writes `waiting`, raises the generation and replaces the row's attention entry with its due time, if it has one (invariant 9). The same transaction raises `run_version`, writes the run row's long TTL and makes the run row due (above). So the old final status never serves a dependant (B-R2) and leaves the bell. It also renews the `output#` TTL of the task's direct upstreams and requests the renewal of the history TTL on its run's rows (§5). Then it writes the row's `asset:{name}` pointers.
- **Heavy actions are requests.** Stop of a run, Restart with dependants and the renewal of the history TTL touch every row of a run. At an estimated 25 ms per transaction, 2,000 rows take about 50 seconds, past the 30 seconds API Gateway waits. A Stop that timed out would leave upstreams running.
  - So the console writes the request on the run row, with who asked and when, and answers at once. The dispatcher carries it out in passes, within their time budgets, through the same builder, and retries what failed. While it works, the UI shows the action in progress, for example "stopping…".
  - The costs: the action takes seconds on a small run and longer on a large one, and the counts in the run lists can lag by seconds. A request makes the run row due at once, so when the stream's consumer is down, the sweeper takes it within 5 minutes, and the System status shows the consumer's failure (§6).
  - The console refuses a Restart with dependants while another reset or a Stop of the run is in progress, and a Stop of a run cancels a pending reset.
- **Restart with dependants** is the Restart route with `with_dependants` set (`POST /api/task-restart`), so it adds no route. It restarts the task at once, in the same transaction as its request.
  - Each pass that carries out the request builds the set to reset from its own reads: the descendants in the same run that are `upstream_failed` or that a rule skipped. A skip by an operator or by the run stays.
  - It resets them the same way, one transaction per row, upstreams first. Each reset raises `run_version` under the condition above, so a decision read before a reset loses its transaction (§1). An action that lands between two resets makes the next one fail, and the pass builds its set again from new reads.
  - Only a pass that resets nothing clears the request, so a dependant decided while the reset runs is reset by a later pass.
- A Restart is refused for a row that names no spec version: a row of a run from before 1.3 (*Migration*). It is refused as well while a Stop of its run is in progress.
- A Restart is also refused once its run started more than a year ago, less a day (`RESTART_TTL_MARGIN`). Every row the Restart needs outlives that: the upstreams' `output#` lives a year from its last write, and the run's history a year and 90 days from each write (§5). DynamoDB may delete a row at any time after its TTL, and a renewal that came later would find rows of the run gone.
- An action on a task whose `run_task` is running stops that execution afterwards, as cleanup. A Lambda invocation runs on, but nothing records its result: the raised generation fences the stopped `run_task`.

### 4. How a run ends

- **A row is final** when its status is in `SETTLED_STATUSES` (§3). A row that waits for a decision is not final. One function answers "final" for the run's end, for the trigger rules (ADR #138) and for the console's run status.
- **A run has finished when every row is final.** The dispatcher checks it at the end of each full pass over the run, from the rows it read. The outcome is ADR #112's derivation (`derive_execution_status`), which also takes the run row's mark of a failed direct step (§1). `success` answers the token with `SendTaskSuccess`; `failed` and `aborted` answer it with `SendTaskFailure`, which names the failed tasks. The list is capped so that the cause stays within the 32,768 characters Step Functions accepts: a longer cause fails the answer for good. The run row keeps no outcome of its own: the console derives the run's status from the rows, or from the run row's summary, and the mark.
- **The summary.** Each full pass counts the run's rows per status and writes the counts on the run row when they changed: a cache derived from facts, not counters accumulated from events (Principle #42). It writes them only when no pass that began reading later has written some, so a slow pass never replaces a newer count. The run lists take each run's status from it, with `derive_execution_status` (§5). Its counts lag the rows by seconds after a change that wakes the dispatcher (§3). After one that does not, such as a start, they lag until the run row's next check, up to about 30 minutes.
- **The answer is marked by token.** After the answer, the run row records the token it answered. It leaves the attention index once no request waits on it (§3). The mark is conditional on the `run_version` the pass read, which a newer token raises, so it is written only while that token is still the current one; a newer token clears it (§1, §3). A run whose token changed after an answer is therefore answered again. If the answer does not get through:
  - a closed token (`DEAD_TOKEN_CODES`) counts as answered: the run was answered already, or its execution has ended;
  - a transient error leaves the run row due, and the sweeper repeats the answer;
  - any other error writes a `coordination` warning and leaves the run row due (Principle #38).
- **A failure no longer ends the run at once.** A failed task first waits for a human decision, as ADR #114 decided. Once the failure is final, by Fail or by the decision timeout, the dispatcher decides its dependants by their trigger rules (ADR #138). A dependant that needs the task to succeed becomes `upstream_failed`, and an `all_done` dependant runs. Independent tasks run to completion, and the run fails only when every row is final. Airflow, Dagster and Prefect end a run the same way. There is no fail-fast option.
- **Stop is final for the run.** The dependants of a stopped task are decided as after a failure (ADR #138), and the run ends when its other rows are final. A stopped row has no deadline. A Restart of the stopped task stays possible later, even after its run has ended: alone it reruns that task, and with dependants it also reruns those the stop decided.
- **The console's run status follows the same rule.** A run with a failed task shows `running`, with the number of its failed tasks, until every row is final. Both early branches of `derive_execution_status` change: they return `failed` and `aborted` today before every row is final.
  - The run lists read run rows with their summaries (§5). A date filter still finds its runs through the task rows of its date; it reads the page's run rows in one `BatchGetItem`, and takes a 1.3 run's status from its run row.
  - The console asks Step Functions nothing about a run. The engine keeps the run row true: a run whose execution ended before its answer is stopped at the run row's next check (below).
- **Stop of a run** writes the stop mark on the run row, with who asked and why, and is refused while the run row is `creating`. The mark starts a stop, a request that is in progress until every row is final and the run's answer is marked; the dispatcher then records the stop as done.
  - While the stop is in progress, the dispatcher makes the transaction of a Stop for every unfinished row, in passes, dependants before their upstreams. So no pass sees a stopped upstream while a dependant still waits: the run's `all_done` and `one_failed` rows are stopped too, and no clean-up starts after it.
  - No row becomes `deps_ready` during the stop. The stop mark raises `run_version`, which the `ConditionCheck` of each `deps_ready` transaction checks, so a pass that read the run row before the mark writes no `deps_ready` after it (§1). A row that became `deps_ready` before the mark is stopped like the others.
  - A row whose status moved under its transaction is read again, and stopped from its new status unless that one is final. A Stop of a run writes no output markers.
  - Then the dispatcher stops the pipeline execution, and any running `run_task`, as cleanup. Every row is final then, so the dispatcher answers the run, and the closed token counts as answered.
  - Once the stop is done, no pass stops a row again, and a Restart is accepted and reruns its task. The mark stays and refuses the run's activation and any new token (§1).
- **A run whose execution ended before its answer** is stopped the same way, at the run row's next check: the dispatcher writes the stop mark with the cause. That write is conditional on no stop mark, since a Stop of the run also stops the execution before the answer is marked, and its mark keeps who asked and why. That covers an execution stopped in the AWS console, or together with the `bulk_backfill` execution that started it, and one that reached its year (§5). The dispatcher reads the execution's status through `DescribeExecution`.
- **A run that never became active,** with a run row still `creating` after `Start_Run`'s whole retry window (§1), is stopped the same way, with the execution's error as the cause. The mark keeps a late attempt of `Start_Run` from activating the run on top of the stopped rows. The dispatcher then answers the run's token, which closes the run row (§3).

### 5. Time limits, retention and pause

The defaults change in 1.3:

| What | In 1.3 | In 1.2 |
|---|---|---|
| One attempt of a task's work | none by default. `execution_timeout` sets one; a timeout is a failed attempt, so retries and then a decision follow (§1) | `execution_timeout`, 24 hours by default, bounded the whole task (its waiting, countdown and every attempt) and stopped nothing |
| Waiting for tasks of the same run | none of its own: the wait ends with the task it waits for, which either starts it or makes it `upstream_failed` | `orchestration_timeout`, which fell back to `execution_timeout` |
| Waiting for `wait_for` facts | 7 days, counted from when the task's own dependencies are met (`trigger_rules.enabled_at`); then the task fails | the same as above |
| All waiting, when `orchestration_timeout` is set | capped by it; unset, it no longer falls back to `execution_timeout`. `polyris-deploy` warns when it is under 7 days on a task with `wait_for` | the same cap |
| A countdown (`wait_before`) | ends at the sweeper's first check after it, up to 5 minutes late | ended on time, in the wrapper |
| A start that keeps failing | one warning after 15 minutes, then `failed` after 6 hours, both counted from the first failed attempt (§1) | no such state: the wrapper started `run_task` itself |
| A decision on a failed task | the global decision timeout (`__global_settings__`, 5 hours by default) | the same |
| A stopped task | none: Stop is final for its run (§4) | held its run until the branch's limit |
| The run | one year, the longest a Standard execution runs | the longest of its branches |
| A run's history: its task rows, run row, input and `wait_for` pointers | a year and 90 days from each write, longer than a run lives | 31 days from each write, or a day more than a longer `orchestration_timeout`; the DAG snapshot 120 days |
| A date's data: `output#`, operator markers included | a year from the last write | 31 days: a success without `xcom.push` wrote it anew, and the other writes kept the first TTL (`if_not_exists`); the console's markers 30 days |
| A task's input: `input#` | 90 days from each write | 31 days from its first write (`if_not_exists`) |
| Timeline events (`task-events`) | 90 days from each write | 31 days from the task's start |
| Backfill records | as long as the backfill runs, and 90 days after it ends | 30 days from creation, never renewed |

- **Precision.** A limit fires at the sweeper's check of the row, which falls due at the limit: up to 5 minutes late while the sweeper keeps up with its due rows. A countdown ends the same way, and that check writes no lost-hint warning: the sweeper is how a countdown ends. Everything else runs on hints within seconds: a task that finishes, a fact that arrives, a resume. A lost hint, or a row's lost due time, costs at most the run row's next check, up to 30 minutes, since that check judges every row from the row itself (§1). A lost hint also writes one lost-hint warning for its run (§7). The self-check reports the oldest overdue row, run rows included (§6).
- **The deadline function.** A row's next deadline is defined clause by clause in this section, so it is one pure function in `polyris/coordination/` with a test per clause (Principle #48, §9). Besides the limits in the table, its clauses are:
  - a Restart starts every limit of the row again;
  - `orchestration_timeout` covers the wait until the row is ready, not its countdown, as in 1.2;
  - a ready row is not decided again during its countdown, so a `.within()` condition is not judged again when the countdown ends;
  - an upstream's failure counts from when it became final, at the decision or its timeout, not from when its work failed;
  - the paused time that falls inside a wait moves its deadline (below);
  - a `deps_ready` row whose start keeps failing gets one warning at `START_WARN_AFTER`, 15 minutes, and fails at `START_FAIL_AFTER`, 6 hours, both counted from its first failed attempt. Time counts, not attempts, so a short burst of throttling warns no one. Paused time moves both, and an `AccessDeniedException` counts toward neither (§7).
- **Retention.** A run writes rows with three lifetimes.
  - **Its history:** its task rows, its run row, its input and its `wait_for` pointers. Each gets a TTL of one year and 90 days at every write, longer than a run can live. Nothing shortens it at the run's end, so a finished run's history stays about 15 months after its last write (*Alternatives*). The Runs tab and the pipeline page show the runs of 90 days. A date filter reaches any date whose rows remain, without the tasks' inputs after 90 days.
  - **A Restart renews the history.** It writes the run row's own long TTL in its transaction, and requests the renewal of the long TTL on its run's other rows (§3). The dispatcher writes it in passes.
  - Every renewal write is conditional on the row's existence (`attribute_exists`), so it never writes a row of the key alone where TTL deleted one; such a row shows as a task without a status. A Restart is refused once the run row's TTL is less than a day away (§3).
  - **A date's data:** `output#`, operator markers included. Its TTL is a year from the last write: every write renews it, instead of keeping the first one (`if_not_exists`), and no run's end resets it. So a rerun of any date of the past year that skips tasks (`skip_tasks`) still finds their outputs. A Restart renews the `output#` TTL of the task's direct upstreams, conditional on the item's existence (`attribute_exists`), so it never writes an item of the key alone. A Restart is refused once its run is a year old (§3), so those items still exist.
  - **A task's input:** `input#`, what the task received at its start: its upstreams' outputs and the run's variables. Only the console reads it (`routes/tasks.py`, `get_task_output`), never a rerun, so it lives 90 days from each write, the same 90 days as the console's window of runs. Kept a year, it would be the largest part of the table's storage.
  - **Timeline events** (`task-events`) get 90 days from each write, with no reset. A run longer than 90 days loses its earliest events. They only feed the console's timeline, so invariant 11 does not cover them.
  - **Backfill records** get the long TTL while the backfill runs, and 90 days once `bulk_backfill` writes their final status. In 1.2 they got 30 days at creation, never renewed: a backfill longer than a month lost its record mid-run, and `bulk_backfill`'s `ADD completed_partitions` then created a record of the key alone, without a TTL.
  - **Unchanged:** asset facts 365 days, bell warnings 8 days, push retries 7 days and the results bucket 30 days. Push subscriptions and the registry have no TTL.
- **The table grows, so its scans go.** These lifetimes make the tasks table about ten times as large as in 1.2, at 1,000 tasks a day and rows of about 1.1 KB measured on a dev table. Task rows stay about fifteen times as long, `input#` about three times and `output#` about twelve times.
  - The two console routes that scan the whole table on a pipeline page's first load, `/api/pipeline-status` and `/api/pipeline-dag` without a run, move to a query of `pipeline-date-index`.
  - Every search of backfill records (`backfills_repo`) moves from a scan of the whole table to a query of `pipeline-date-index` with `pipeline_name = _polyris_bulk_backfill`. This move is a precondition of the lifetimes above. The bell calls `list_recent` every 30 seconds, and each call scans the whole table. At ten times 1.2's size, one scan would take an estimated 22–38 seconds, against the 30 seconds that API Gateway waits for the console's answer.
  - Backfill records gain a `date`, the day they started, so they enter that index. That index projects few of their fields, and a change of projection would replace it, so the search reads the full records with `BatchGetItem`.
  - The `date` also puts them in `date-pipeline-index`. Every reader of the two date indexes skips them through `should_skip_token_row`, as the feeds already do, and a census test checks that every read of those indexes skips internal and backfill rows. The bell no longer reads them (§6).
- **Run lists read run rows.** The Runs tab and the sidebar's pipelines list read run rows, not task rows, from a new sparse index of the tasks table, `run-start-index`.
  - **The index** holds run rows only. Its partition key is `start_day`, the `YYYY-MM-DD` of the execution's start, which only run rows carry, and its sort key `started_at`. It projects what the lists show: `pipeline_name`, `run_date`, `status`, `finished_at`, `triggered_by`, the summary and the mark of a failed direct step. Each run's status comes from its summary (§4), so a list reads no other item.
  - **The Runs tab** walks the start days newest first and stops once its page is full. Stopping early is correct, because the shard key is the day the run started, not its logical date. Its window becomes 90 days; the Tasks tab keeps 14 days (`SLA_DAYS`). Today the runs feed walks every date of its window, one query per date, and must: a rerun of an old date, started today, sorts first by `started_at`. A walk of 90 dates would cost about six times as much per load as one of 14.
  - **A pipeline filter** walks the same start days, with a filter on `pipeline_name`, until the page is full or the 90 days end. Today it reads the pipeline's task rows through `pipeline-date-index`: about 102,000 rows for 50 runs of a daily 2,000-task pipeline, in a 256 MB function behind API Gateway's 30 seconds.
  - **A date filter** still reads the task rows of its one logical date (§4). With it neither tab has a window, nor has the Tasks tab with a pipeline filter. The Tasks tab keeps its walk of dates: it has tens of times more rows, so an index of task rows risks write hot spots on large pipelines.
  - **The pipelines list** (`/api/pipelines?stats=true`, polled every 30 seconds by each visible console) reads the run rows of 14 start days. A pipeline's `status` and `waiting_on` come from its latest run's summary. `recent_runs` keeps its meaning: the last 10 runs whose logical date is within the 14 days, newest first by date, then `started_at`. Its API stays as ADR #136 defines it, and `stats_incomplete` now means a start day that could not be read.
  - **The pipeline page's runs** (`/api/pipeline-executions`: the history of a pipeline's latest runs, and a range of dates) read the same run rows, with a filter on `pipeline_name`. The history walks the start days newest first, like the Runs tab. A range walks every start day of the 90-day window, with a filter on `run_date` too: one query per start day, 90 in all, whatever dates it asks for. Only an opened run reads its task rows, by `pipeline_execution`. Today the history of a daily 2,000-task pipeline reads about 30,000 task rows, and a month about 60,000.
  - **A run can start before its logical date.** `current_date` comes from the request, and the console's Run dialog fills in the browser's local date. So a run started after local midnight east of UTC carries a date a day ahead of its UTC start day. A run for a later date, started today, is valid too, and nothing refuses it.
  - So a list reads start days alone only where it orders runs by their start: the Runs tab, whose early stop stays correct, and the pipeline page's history. A list keyed by logical dates walks its whole window of start days, as a range does.
  - A range misses a run that started more than 90 days ago, though its history is kept longer: the console's window is 90 days. A run still running after 90 days is rare.
  - The pipelines list keeps its 14 start days. A run that started before them, for a date within them, is missing from `recent_runs`. So a run started ahead of its date leaves the strip that much earlier: a day early for one that the Run dialog dated.
  - **No route asks Step Functions about a run,** since the engine keeps the run row true (§4). The console's remaining calls concern pipelines and tasks: `StartExecution` of a run started from the console, `ListStateMachines` for the health check and for a pipeline the registry has no ARN for, and `StopExecution` of a task's `run_task` after an action.
  - The costs: a run stopped in the AWS console shows `running` until the run row's next check, up to about 30 minutes, where 1.2.1 showed it within 30 seconds. After the upgrade the pipelines list shows no 1.2 run: a pipeline's status appears with its first 1.3 run, and its strip of the last 10 runs fills over 14 days.
  - The UI takes each window from its own constant, and a parity test pins each to the console's (Principle #28).
  - The index is a DynamoDB schema change that the maintainer approved (Principle #6), and the only index that 1.3 adds (*Migration*). While DynamoDB builds it on an upgraded table, its reads answer "backfilling". The Runs tab and the pipelines list treat that as transient and say so. In TR-0 the build took about 8 minutes on 100,000 rows, the size of 1.2's table at 1,000 tasks a day, and CloudFormation reported the table updated after 44 seconds, while the index was still building. So `polyris-up` waits for the index to become active and shows that it waits.
- **Pause.** A pause means "start nothing": the run's ready tasks stay `waiting_paused`, and a pass over a paused run starts no row, not even a `deps_ready` one whose start had failed. Tasks that already run go on. A pause lasts until it is lifted.
  - **The run row holds the pause.** Pause opens an interval and Resume closes it, each with a conditional update of the run row, which keeps at most ten intervals (§3). The pause flag of 1.2, a row of its own, goes.
  - **No start after a Pause.** A Pause raises `run_version`, which the `ConditionCheck` of each `deps_ready` transaction checks (§1). So a Pause that lands after a pass read the run row holds every row that the pass has not yet written `deps_ready`, as a Stop of the run does (§4). A row written `deps_ready` before the Pause landed still starts.
  - **One owner per question.** A pause's time counts toward no waiting limit, neither for dependencies nor for a decision: the deadline function moves each deadline by the paused time inside its wait. `trigger_rules.enabled_at`, from which the deadlines and the countdown count, reads no pause. Only the sweeper's lost-hint check reads the resume, through an enabling time that is never before it (ADR #130 §9). So no pause is counted twice: the clamp of the enabling time to `resumed_at` in `reconcile.py` serves that check only.
  - **Due times in a pause.** While the run is paused, a `waiting_paused` row, a `deps_ready` row whose start the pause holds and a row's limit on its wait have no due time (invariant 9). A decision deadline keeps its entry, which the bell reads whatever its time, and the entry falls due far in the future; the bell shows no deadline for it.
  - Neither a limit nor a decision deadline can fire during a pause, so a paused run costs no check of either. A pass removes or postpones these due times with the `ConditionCheck` on `run_version` of §1, so a Resume that lands after its read keeps them.
  - A pause does not stop a countdown: a task whose `wait_before` ends inside a pause starts when the pause is lifted.
  - `/api/execution-extend` and the console's "+12h" button go.
  - On resume, every ready task starts at once. Resume closes the interval, and the run row's change reaches the dispatcher through the stream (§7). The pass decides the `waiting_paused` rows and starts the `deps_ready` rows that the pause held. It sets the due time of each limit and decision deadline again, moved by the paused time. Rows it does not reach within its time budget are left to the next pass, which `left_at` wakes at once, and are no lost hints (§7, Rule 52). A Resume whose stream record is lost is found at the run row's next check, up to 30 minutes later.
- **The run's safety limit.** `Start_Run` waits for its token for one year, the longest a Standard execution runs, whatever the DAG. There is no derivation, no margin and no heartbeat: the task limits and the sweeper do the work, and a broken engine shows in the System status (§6). A run whose execution reaches the year is stopped like any run whose execution ended before its answer (§4).
- **`bulk_backfill`** waits for each run as long as the run lives: its 24-hour limit per run goes with the other 24-hour defaults.

### 6. Failure visibility

The three levels of ADR #130 §9 stay; what fills them changes.

1. **Heals itself.** A start is repeated under the same name when its next try comes. The sweeper checks every due row in the attention index, and a run row's check judges every row of its run from the row itself and puts back a lost due time (invariant 9). Both streams retry a failed batch, and `run_task` retries its success transaction after a conflict. Every pass has a time budget: a pass that runs out wakes the next through `left_at` and leaves its run row due, instead of failing its batch (§7).
2. **Fails loudly.** A non-transient error of the dispatcher writes `failed`, with the cause, into the row itself; no token, wrapper or failure handler stands in between.
   - A start refused for good, and a `run_task` execution that ended without an outcome, make the row `failed` with the error and the cause. A success that cannot be recorded makes the row wait for a decision, with the cause (§1).
   - A `deps_ready` row whose start keeps failing writes one `coordination` warning after 15 minutes, and fails after 6 hours (§5).
   - A missing row fails its waiting dependants, with a warning (invariant 11).
   - A `Start_Run` that fails for good writes a `coordination` warning with the run and the cause, and the execution fails after the `Parallel`. The rows of a run that never became active are stopped with the execution's error (§1, §4).
   - A failed direct step writes a `step` warning and marks the run row, and the run's execution fails once every branch has ended (§1).
   - The rows of a run whose execution ended before its answer are stopped with the cause (§4).
3. **Shows engine health in the console.** The sweeper's self-check and the System pill also cover the tasks table's stream and its event source mapping, as they cover AssetFanout's: the mapping's state, and its `LastProcessingResult`, which names a failed last batch. An `AccessDeniedException` in the dispatcher shows there too (§7). No custom metric, alarm or topic is added (Principle #45).
   - The oldest overdue row, which may be a run row, shows a sweeper that cannot keep up. It does not show a stream consumer that stopped: the sweeper makes its passes in its own process, so its checks go on.
   - A consumer that stopped, or a shard held by a record that keeps raising a transient error, shows in two ways. The mapping's state or `LastProcessingResult` turns the System status red, though one shard among healthy ones may show only now and then: in TR-0, `LastProcessingResult` read OK again as soon as a healthy shard processed a batch, while a held shard kept retrying. So the sweeper's report also counts the runs whose check found lost hints, and `engine_findings` shows a count above zero in the System status. Each run that it holds back also gets a lost-hint warning in the bell at its run row's checks, since its rows wait past the grace (§7).
   - Meanwhile those runs go on at their run rows' checks, up to 30 minutes apart, and a held record leaves the stream after 24 hours.
   - An engine whose every pass fails, for example on a parse error after a format change, ages no entry: each failed pass moves its run row by the backoff. It shows through the sweeper's count of failed passes instead (§7).

**The bell finds failures by when they happened.** Today it reads every task row of three logical dates (`date-pipeline-index` with a status filter, up to 5,000 rows every 30 seconds), so a failure in a run of an older date never shows. In 1.3 such failures become common: a `wait_for` task fails at its 7-day limit, and a run waits for days. So the bell reads `attention-index` only, as it does for warnings, with no new index and no new kind of row:

| Kind | Rows | `attention_at` | Read by |
|---|---|---|---|
| `waiting` | a task row with a time of its own (invariant 9): `waiting` with a limit or a countdown, `deps_ready` or `running`; the run row while it is due (§3) | its next check | the sweeper, once due |
| `decision` | a task row `waiting_decision` | its decision deadline; far in the future while its run is paused (§5) | the sweeper, once due; the bell, every entry |
| `failed` | a task row `failed` | when it failed | the bell, within its window |
| `retry` | a push start to retry | its next try | the sweeper, unchanged |
| `warn` | a warning row | when it was written | the bell, within its window, unchanged |

- A row's entry follows its status and its times (§3), so the write that changes them replaces or removes it. A Restart removes a `failed` or `decision` entry, and writes a `waiting` one only for a row with a limit outside a pause. The `paused` kind goes.
- The run row reuses the `waiting` kind. Its only reader is the sweeper, which groups the due entries by run, so a run row and the task rows of its run fall into one pass. The `_run_` key tells them apart. A kind of its own would add 16 shard queries to each sweeper run and 16 to its self-check, and no reader needs the two apart.
- The index keeps its projection, since a change would replace it. The projection holds what the bell shows: the status, task, pipeline, run, error and `task_date`, which task rows now carry too (§3).
- The bell reads every `decision` entry, and the `failed` and `warn` entries of its window, from the 16 shards of each kind. It keeps one notification per run, a decision request over a failure, as today, and adds the warnings and the backfills.
- A failure outside `run_task` shows there like any other: a wait limit, a decision timeout, a Fail decision, a `run_task` that died.

### 7. Wake-ups

A hint only says "check again". The dispatcher's writes, its starts and the run's answer are decisions, which a lost hint never undoes (Rule 41).

| Source | Wakes the dispatcher for |
|---|---|
| A DynamoDB stream on the tasks table, through a new event source mapping | the run of a row whose transition the *Wakes* column of §3 marks. These are a task row that became final or `waiting_decision`, moved from a final failure to `success` or `skipped`, or was restarted. They are also a run row that became active, was paused or resumed, received a request, or whose `left_at` a pass set |
| The stream on `asset-events`, through AssetFanout | a narrow pass over each run with a task that waits for the asset (below) |
| The sweeper, every 5 minutes | the run of every row that is due |

- The tasks table's stream replaces `notify_dependents` and `query_subscriptions`. It carries every writer's change, so no writer has to remember to notify; ADR #130 chose the fact table's stream for the same reason.
- **The filter comes from the transition table.** The stream carries both images (`NEW_AND_OLD_IMAGES`), so each pattern names an old and a new value. The patterns are generated from the *Wakes* column of §3, or pinned to it by a parity test, and the harness's stream adapter uses the same predicate (Principle #42). A hand-kept list would lose the wake-up of a transition added later, and only the run row's check would find the rows it should have woken, up to 30 minutes late, with a misleading lost-hint warning.
  - Each task-row pattern requires `generation`, a field that only task rows carry, because `output#` and warning rows have a `status` too (§9).
  - The patterns take four of the five filters that Lambda allows per mapping by default. Three are for task rows: a row that became final or `waiting_decision`, a row that was restarted, and a final failure that became `success` or `skipped`. The fourth lets every insert and change of a run row through, since a filter cannot compare a field's old and new values.
  - A task-row filter says "became" through the old image. The first, for example, requires a final or `waiting_decision` status in the new image, and an `anything-but` of the final statuses on the old image's status.
  - A filter may let through more records than the predicate wakes for, and the dispatcher drops the rest with the same predicate. Those are a `waiting_decision` row rewritten in a pause, and each write of a run row that the *Wakes* column does not mark, such as its due time or its summary, unless it changes `left_at`. A run row changes a few times per pass at most, so they cost little; TR-0 counts them.
  - On task rows, due-time and TTL writes match no pattern, and no pattern matches a TTL deletion.
  - A test runs every transition of the table through the template's `FilterCriteria` and the predicate, with fixture records for an insert without an `OldImage`, an `output#` row, a warning row, a TTL write and a run row's summary write. TR-0 checked each pattern live: of 25 writes, exactly the 11 expected records passed, and no change from one final status to another, TTL write, `output#` row, warning row or deletion did.
- **Batches.** The mapping takes the batch and retry settings of the asset-events mapping (ADR #130 §6.1), with no on-failure destination, and a batching window (`MaximumBatchingWindowInSeconds`) of 1–2 seconds. It starts at `TRIM_HORIZON`: in TR-0 a new mapping that started at `LATEST` missed the records written in its first two minutes. A mapping that was disabled and enabled again went on from where it stopped and lost no record. The dispatcher groups a batch's records by run and makes one pass per run. Its `ParallelizationFactor` is above 1 but small: a new AWS account may run only 10 Lambda invocations at a time, and the dispatcher shares them with every other function of the platform. Two passes over one run at the same time are safe: every write is conditional and every start has a derived name.
- **A pass over a `creating` run returns at once.** The run row's insert passes its filter, and the predicate drops it. `Start_Run`'s task-row inserts match no filter, since an insert has no `OldImage`, as TR-0 confirmed.
- **Time budgets.** Every pass has a time budget below its function's timeout, whoever calls it: the stream, AssetFanout or the sweeper (Rule 52). A pass that does not reach every row of its set makes its run row due at once (§3). It also sets the run row's `left_at` to its own time, when it wrote something or when `left_at` is absent.
  - A change of `left_at` wakes the dispatcher through the stream, so the next pass goes on at once; if the record is lost, the sweeper goes on within 5 minutes. The start of 2,000 root tasks, or the Resume of a large run, then goes on in passes back to back, not at the sweeper's pace.
  - A run where no write gets through wakes at most one more pass this way, so passes never loop without progress. The sweeper's check clears `left_at`, and rows it finds while `left_at` is set are no lost hints (below).
  - The mapping retries a failed batch forever (`MaximumRetryAttempts: -1`, `MaximumRecordAgeInSeconds: -1`). A pass that kept timing out would therefore hold up its shard until its records left the stream, after 24 hours; the budgets prevent that.
- **Conflicts.** Every decision's transaction checks the run row, so a pass sends a run's transactions one at a time. Two passes over one run, or a pass and an action, can still cancel each other's writes (`TransactionConflict`), which the SDK does not retry. A pass retries such a write after a short random backoff, within its budget, and the console does the same for an action. A row that a pass leaves for a conflict is left in the same way, through `left_at`, and is no lost hint.
- **Errors.** A record raises only on a transient error. Anything else ends in the row or in a warning, so one bad record never holds up its shard (Rules 51 and 52).
  - An `AccessDeniedException` is not transient, and it fails no row either, a denied start included (§5). It writes a `coordination` warning, shows in the System status and leaves the row to its next check, or its run row's, so the run goes on once the permission is fixed.
  - Root `CLAUDE.md` asks every Lambda to raise an `AccessDeniedException`. With retries that never end, a raise would hold up a shard, so that rule gains an exception for stream consumers in the same change.
- Adding a stream changes the table in place; CloudFormation does not replace it. It is a schema change (Principle #6), which the maintainer approved for this design.
- **Reads.** A full pass reads the run row, then the run's task rows by key, from the task names of its spec version (§3), with consistent reads and no index on the decision path. `attention-index` keeps its keys and its projection; its kinds are in §6. `date-pipeline-index` keeps `wrapper_execution_arn` in its projection, though nothing writes or reads the field any more: a change of projection replaces the index.
- **Logs.** A pass writes one log line, with its run and its counts, and one line per row it changed. It writes none for a row it left as it was, such as an unchanged `wait`: a line per row per pass would grow as N², like the reads (*Cost*). A harness test counts the lines of a pass over a run where nothing changed.
- The `asset:{name}` pointers stay in `dep-subscriptions` for AssetFanout; the task-dependency pointers go. The dispatcher deletes a row's pointers once the row has left the dependency wait (`waiting` and `waiting_paused`), and a restart writes them again (Rule 40).
- **An asset wake is a narrow pass.** AssetFanout passes over each run with a task that waits for the asset. It reads the run row, the rows that the asset's pointers name in that run and their direct upstreams, by key, and decides them with the same `decide`.
  - The full pass is the same code over every row of the run: one entry point takes a set of rows, so the logic cannot fork.
  - A narrow pass leaves the run's end and its summary to the next full pass, which the stream starts once a status changes.
  - With a full pass per wake, 90 facts for a 2,000-task pipeline that waits for 90 dates would read about 16 million rows and hold one shard for about an hour.
- **The sweeper** keeps its lease, budget, backoff and schedule. It reads the due entries of the `waiting` and `decision` kinds (§6), run rows included, groups them by run and makes one full pass over each run. The pass decides every waiting row of the run and judges every row from the row itself (§1). So each row whose time has come gets the check of its status, whether or not its own due time was there:
  - a `waiting` row at the end of its countdown is decided again, and starts if it is still ready and the run is not paused; one past its limit fails, unless the run is paused (§5);
  - a `deps_ready` row whose next try has come is started again, unless its run is paused or being stopped, and a failed attempt counts toward its deadlines (§5);
  - a `deps_ready` or `running` row whose liveness check has come has the execution of its current generation judged, and is failed when that execution ended without an outcome (§3);
  - a `waiting_decision` row is checked against its deadline, which a pause moves (§5);
  - a run row still `creating` after `Start_Run`'s retry window is stopped (§4); an active one has its execution checked, its requests carried out and its end decided (§4).

  The pass writes only what changed: its decisions, and the due times that differ from the due-time function. The sweeper then moves the run row's due time on and clears its `left_at`, one small write per run (§3).
  - **A pass that fails** leaves its run to the backoff too. The sweeper moves the run row's due time the same way and writes a `coordination` warning for the run, with the cause. So one broken run, such as one whose spec item was deleted by hand, neither takes the sweeper's budget first at every sweep nor turns the System status red for the whole engine.
  - The warning's key is its kind and the run (`records.warning_item`), so each failed check replaces the same row, and the bell shows it once, at its latest time.
  - The sweeper's report counts its passes and the failed ones. A sweep in which more than half of the passes failed, and at least two, adds a failed step to the report, with the count and the first cause. `engine_findings` shows it in the System status as any failed step, so an engine whose every pass fails turns it red (§6).
  - With few active runs, a sweep may run fewer than two passes; the runs' warnings in the bell are then the sign.
  - **Lost hints.** A row whose wait the pass ends is a lost hint only when its enabling time is older than the check's time minus `LOST_HINT_GRACE`, 5 minutes. The grace is longer than the stream's batching window and its retries of a failed batch, so a row whose stream pass is still under way does not count.
  - Neither does a row whose countdown ended, nor any row the check finds while the run row carries `left_at`: a pass left those (above).
  - A lost hint costs at most the run row's next check. The check writes one `sweeper` warning for its run, with the number of lost hints and a few of their task names, where ADR #130 §8 wrote one per row.

  Push retries, the push catch-up and the self-check stay; the pause-release step and the adoption scans go. The self-check's oldest overdue row may be a run row.

### 8. Task specs and pipeline limits

A task's spec is everything needed to run the task and to draw it. That means its type, resource, configuration (`task_config`, ADR #108), dependencies, trigger rule, limits and retries, `wait_before`, outlets, inlets, `wait_for` and `skip_on_backfill`, which include every field the graph view shows. The specs leave the definition for a table of their own, so the size of a pipeline has a limit of ours, not the limits of a Step Functions state or definition.

- **What a task row carries.** Only what the dispatcher decides on: the dependencies, the trigger rule, the limits, `wait_before`, `wait_for` and `skip_origin`, with the `spec_id` that names the spec version and the hash of the task's spec text. That is about 1 KB, besides the status fields and the fields that the console reads (§3). The full spec stays in `pipeline-specs`, and the dispatcher reads it once per start of the task (§1).
  - So the cost of a pass does not depend on the size of a spec. Every full pass reads every row of its run, and DynamoDB bills the whole item.
  - A daily 2,000-task pipeline costs an estimated $17 a month in DynamoDB with rows of about 1 KB, almost all of it the reads of its full passes, and about $95 with rows of about 20 KB (*Cost*).
- **The table, `pipeline-specs`.** It is on demand and carries the billing tags (Principle #54). Its partition and sort keys are strings, and it holds two kinds of item:
  - **Texts.** The partition `{pipeline}#text`, the sha256 of the text as the sort key, and the task's spec as one string attribute. A text is stored once, however many versions use it.
  - **Versions.** The partition `spec_id`, `{pipeline}#{fingerprint}`, and the sort key `task_id`, with the hash of that task's text: about 100 bytes per task. A version's fingerprint is the sha256 over its `(task_id, text hash)` pairs in key order.
  - A text is canonical JSON: `sort_keys`, compact, `ensure_ascii=False`, `allow_nan=False`. DynamoDB stores the number `1.0` as `1` and keeps no order in a map, so a hash of the attributes read back would not match the deploy's. Moto, the mock that unit tests use, keeps both, so only a live table shows it. Every reader checks the sha256 of each text it reads.
  - It has no TTL, which a template test pins. It has no deletion protection, which would make `polyris-down` and the rollback of a failed first install fail. The table goes with the platform stack.
  - Versions and texts never change and are never deleted. A redrive runs the original definition for up to 14 days after its execution ended, and identical DAGs share their version items. A destroyed pipeline's specs therefore stay until the platform is removed. Point-in-time recovery is not needed: the specs are derived, and a redeploy writes them again.
  - So a 2,000-task pipeline with 5 KB specs, deployed ten times a day with a few tasks changed each time, grows the table by about 0.75 GB a year instead of 36 GB. A deploy writes about 200 KB instead of 10 MB.
- **`polyris-deploy` writes the specs before the stack.**
  1. It writes each text that the table does not hold yet, with a conditional put that counts a text already there as written, then every version item, and retries `UnprocessedItems`. An access-denied error stops the deploy.
  2. It reads the version back with a consistent query and its texts with `BatchGetItem`, and checks the count, each text's hash and the fingerprint.
  3. Only then does it create the change set.

  The definition carries `Start_Run`'s arguments: the format, the full `spec_id` and `task_count`. No state's input carries a spec.
- **`Start_Run` checks what it reads.** It checks the format first. It reads the version with a strongly consistent, paginated `Query`, then the texts with `BatchGetItem`, retrying `UnprocessedKeys`. It checks each text's hash and recomputes the fingerprint. It parses each spec and rejects one with a key it does not know (§9), as the parser of asset conditions does. It writes on each task row the fields that the dispatcher decides on, with the hash of its text, so the dispatcher reads a task's spec with one `GetItem` (§1).
  - A fingerprint or a hash that does not match, a missing item or a `task_count` of 0 fails `Start_Run` before it writes a row, and its `Catch` writes the warning (§1).
  - Throttling is a transient error, which `Start_Run` retries; it never counts as a missing spec.
  - A pipeline without tasks has no `Start_Run` and no run row: its direct steps run alone, as in 1.2, and write no mark on a run row (§1).
- **Registration and the graph.** Today `Register_Pipeline` and `Save_DAG_Snapshot` take every task's metadata as state input. `Register_Pipeline`'s input passes 256 KB at about 245–460 tasks. Its `Catch` on `States.ALL` cannot catch `States.DataLimitExceeded`, so every run would die in its first state, with no warning. The registry item reaches DynamoDB's 400 KB at about 420–800 tasks.
  - `Register_Pipeline` writes the `spec_id` and `task_count` into the registry, instead of the tasks' metadata. The pipeline's own state machine writes them, so the registry follows a CloudFormation rollback. Its `Catch` names `States.DataLimitExceeded`.
  - The per-run DAG snapshot becomes the run row's `spec_id`. A spec version never changes, so that reference names the graph the run ran with. `Save_DAG_Snapshot` goes, with its rows; the console reads the snapshots of 1.2 runs until 1.4 (*Migration*).
  - The console draws a run's graph from the specs of its run row's `spec_id`, and a pipeline's graph the same way from the registry. As in 1.2, direct steps are not graph nodes (`generators.py` excludes them); a failed one shows through the run's status and its warning.
  - **Every other reader of the registry's `tasks` and `dag` moves to the specs,** through one accessor of the console's DAL. Today these readers are `upstream_integration.py`, `routes/pipelines_info.py` and `routes/tasks.py` in the console, and `stores.Registry` in the coordination Lambdas, which goes with the 1.1.4 code. They read a missing field as empty (`'[]'` or `'{}'`), so a reader left behind would fail silently: a backfill would run the tasks that its user excluded with `skip_on_backfill`. A census test fails for any code that reads the registry's `tasks` or `dag`.
- **The limits are ours.** `polyris-deploy` and `polyris-validate` refuse a pipeline beyond any of them, with the limit and the measured size.
  - **Tasks per pipeline:** a declared maximum of about 2,000. The first test deploy (TR-7) measures the exact number. The harness runs a pipeline of about 200 tasks, and the release rehearsal runs one of the full size live (*Tests*). `Start_Run`'s timeout and the sweeper's window for a run row still `creating` derive from it (§1).
  - **Outlets per task:** 98. `run_task`'s success is one transaction of at most 100 items: the status, the output and up to 98 facts (§1). Mark Successful writes the same transaction, with the same cap.
  - **Bytes:** each task's spec, outlets included, against `run_task`'s 256 KB input, and the definition against Step Functions' 1 MB, as `polyris-deploy` sends it. Today `validate_asl` measures the definition without the indentation that `polyris-deploy` sends (`indent=2`): 230 tasks validate at 937 KB and deploy at 1.11 MB.
  - **Duplicate outlets:** the decorator rejects the same asset twice in a task's `outlets`. Two puts on one key cancel the success transaction for good, so the success could never be recorded.
- **IAM and tests.** The role that runs `polyris-deploy` needs `dynamodb:PutItem`, `dynamodb:BatchWriteItem`, `dynamodb:BatchGetItem` and `dynamodb:Query` on `pipeline-specs` (*Migration*). A test ties the deploy policy in `IAM_PERMISSIONS.md` to the calls in `deploy.py`. A fault-injection test drops or alters a version item or a text, and the deploy must stop before `create_change_set`. Golden spec files show what a change to the generator does to the specs, and expose a change of their shape made without a format bump.

### 9. Contracts and pure functions

A value that two pieces of code must read alike gets one named constant, and a parity test where it crosses a file or a language (Principles #13, #27 and #28).

| Contract | Its home | Parity |
|---|---|---|
| The fields of the run row and of the run's input (§3) | constants in `polyris/coordination/`, read by `Start_Run`, the dispatcher and the console | the generated JSONata of the step mark and of `Start_Run`'s arguments |
| The spec's keys | an enum in `polyris/constants.py`, beside `TaskConfigKey`. The task spec's parser becomes `polyris/coordination/task_spec.py`, and today's `spec.py`, the parser of asset conditions, is renamed `asset_spec.py` | the generator against the enum |
| Canonical JSON, the text hash and the fingerprint (§8) | one function in `polyris/coordination/`, which `polyris-deploy`, `Start_Run`, the dispatcher and the console call | none needed, since one function serves all; a test reads `1.0` back as `1`, as DynamoDB does |
| The format version and its registry field | one SDK constant, which `polyris-deploy` writes and `Start_Run` and the console read | the generated `Start_Run` arguments |
| `run_task`'s input | constants for its keys, from which the dispatcher builds it in Python; today `run_task` reads 39 `$states.input` keys | the keys that `run_task`'s template reads |
| The upstream statuses for `xcom` | `POLYRIS_UPSTREAM_STATUSES`: a JSON object from each direct upstream's name to its status and skip origin | `test_xcom_coupled_constants_parity.py` |
| The field only task rows carry | `generation`, which the stream's task-row patterns require (§7) | the template's `FilterCriteria` |
| `SkipOrigin` | `polyris/constants.py`, emitted by `sync_enums` (ADR #138) | `check-generate-enums` |
| `WarningKind` | gains `step`, loses `fact`, `zombie` and `notify` | the contract test of `records.warning_item` |
| The error of a failed run's answer | one constant, which the dispatcher sends and the generated `Catch` names, and which no `Retry` of `Start_Run` matches | the generator's tests |

Values defined clause by clause are pure functions in `polyris/coordination/`, with a test per clause (Principle #48):
- the transition table, with each status's attention kind, its *Wakes* column and the writes of the run row that raise `run_version` (§3, §6);
- a row's next deadline (§5);
- a task row's next due time (invariant 9). It reads the row's own fields, its upstreams and the run row's pause state, never the clock, so a pass writes a due time only when it changes (§1).
  - It gives a limit's deadline, a countdown's end, a decision deadline, a start's next try, and a running execution's next check, from the time and the number of its checks so far.
  - It gives none for a row that waits only for other rows, and in a paused run none for a row the pause holds or a limit. A decision deadline then falls due far in the future (§5);
- the order of `Start_Run`'s tokens (§1);
- the history TTL and its renewal (§5);
- the run's summary and its outcome (§4);
- canonical spec JSON, with the text hash and the fingerprint (§8).

## ADR #130 after this ADR

Root `CLAUDE.md` keeps the rule numbers, which code and tests cite; the texts change as below.

| ADR #130 | After this ADR |
|---|---|
| §1 facts, §4 asset conditions, §6.6 run names, §10 packaging | Unchanged |
| §2, status ownership | Superseded by §3 here |
| §2.1, the generation | It stays and becomes mandatory: the fence is `generation = :g`, and a decision also checks the run row's `run_version` (§1). The writers it lists, the claims, registration's condition and the branches for rows without a generation go |
| §3, the reconciler | It becomes the dispatcher (§1), which writes what it decides. Step 3, dead tokens and zombies, goes |
| §5, subscriptions | Only the `asset:{name}` pointers remain (§7) |
| §6.1, asset events | Unchanged, except that neither `run_task` nor Mark Successful can succeed without its facts (invariant 12) |
| §6.2–§6.5 | Superseded: task-completion hints, registration, the wrapper's post-wait status, and resume, restart and auto-skip (§1, §3, §7) |
| §7, indexes | No `paused#` entries; `waiting_decision` and `failed` rows get kinds of their own, which the bell reads (§6). A task row has a due time there only for a time of its own, and its run row's due time covers every row (invariant 9) |
| §8, sweeper | Steps 2 (pause releases) and 5 (adoption) go. Step 1 makes one full pass per run, which judges every row from the row itself, writes only the due times that changed, and writes one lost-hint warning per run and check, after a grace (§7). The lease, the schedule and the self-check stay, and the self-check counts failed passes |
| §9, visibility | Level 2 changes (§6); levels 1 and 3 keep their meaning |
| Invariant 1 | Retired: no token is left to guard |
| Invariant 2 | Stays, read as "no start without consistent reads of the task facts" |
| Invariant 3 | Becomes invariants 2 and 7 |
| Invariant 4 | Applies to the `asset:{name}` pointers |
| Invariant 5 | Replaced by invariant 9: a row that waits only for other rows has no due time of its own, and its run row's check covers it |
| Invariants 6, 7 and 8 | Stay |
| Invariants 9 and 10 | Become invariants 5, 6 and 8; a `stopped` row now counts as finished |
| Rule 40 | Applies to the `asset:{name}` pointers: delete them once the row has left the dependency wait |
| Rule 41 | The hints are the two streams, with the sweeper's checks of the run rows behind them. The decisions are the dispatcher's writes and starts, the run's answer, and every write of a fact, which now shares a transaction with its status |
| Rule 43 | Retired: no token signals a task. The dispatcher writes the transition it decides before it starts `run_task` (invariant 3) |
| Rule 46 | Applies to the run's token, which `Start_Run` hands over through the run row: the run row is due at least until its current token is answered (§3), and a failed write fails `Start_Run`. A new token raises `run_version`, so no pass removes the run row's due time on a read taken before it |
| Rule 47 | The writers are `Start_Run`, the dispatcher, `run_task` and the console's builder, and the fence has no branch for rows without a generation |
| Rule 49 | Restated for `Start_Run`'s arguments: a platform function that a pipeline's state machine calls reads each format it supports and refuses any other, with the cause |
| Rule 52 | Stays, and applies to every pass, narrow or full: each has a time budget below its function's timeout (§7) |
| Rules 50, 51 and 55 | Stay |

## Migration and upgrade

**Path.** 1.1.4 → 1.2 → 1.3. All code that kept 1.1.4 working goes (*What is removed*).

**`polyris-up` checks the platform first.** It makes these checks only when the deployed version is below 1.3.0: in 1.3, `run_task` and `bulk_backfill` executions run almost all the time, so a check for runs in flight would refuse every patch release. It refuses, with the reason and what to do, when:
- the platform is older than 1.2.0. The stack's description carries the version (ADR #131). A platform deployed with `sam deploy` carries none, and then the `_engine_health` row, which only 1.2 writes, tells 1.2 from 1.1.4;
- 1.2's adoption has not finished, or finished less than 48 hours ago, as the `_engine_health` row records it. Until then, older rows and facts still need the compatibility code that 1.3 removes;
- a run is in flight: an execution of one of the platform's Standard state machines is running. `ListExecutions` does not list Express executions, so the check lists the Standard ones only. Nothing adopts a 1.2 run, so a long run is stopped and started again after the upgrade.

The checks need `states:ListExecutions`, `dynamodb:GetItem` on `pipeline-tokens` and `dynamodb:Scan` on the registry, which `IAM_PERMISSIONS.md` documents. An access-denied error refuses the upgrade and names the missing permission.

An explicit flag overrides the checks of the drain and of the adoption, but never the check of the version. A 1.2 run left in flight all the same has no run row, so none of its rows starts. Its waiting rows keep the entries that 1.2 gave them, of the `waiting` kind, due within 30 minutes. The sweeper stops each when it falls due, with the cause, by the rule for a task row whose run row is missing (invariant 11). A row it does not find there, such as one left `running`, `waiting_delay`, `waiting_decision` or in a pause wait, names no spec version. The console shows it as stale while its status is not final and refuses to restart it, and it expires with 1.2's TTL.

**The order of the upgrade.**
1. Install the 1.3 SDK and run `polyris-validate --all` over the pipelines' code. Fix what it reports: a `DAG` field or a step class that 1.3 removes, or a pipeline beyond a limit of §8. Such a `dag.py` cannot be redeployed in step 5, and its pipeline stops with the upgrade.
2. Disable the pipelines' schedules, so that no new 1.2 run starts.
3. Let the runs in flight finish, or stop them.
4. Run `polyris-up`.
5. Redeploy every pipeline. `polyris-deploy` sets each schedule's state again on every deploy, so the redeploy turns the schedules back on, also one that was off on purpose before the upgrade. `is_paused_upon_creation=True` keeps a pipeline's schedule off.

A push consumer can still start a run when a fact arrives during the drain. A run that starts between the check and the update is left in flight, and the rule above stops its rows.

**`polyris-up` stops what 1.2 leaves running.** The override flag, and a run that starts between the check and the update, leave 1.2 executions running. A wrapper that waits on its token makes no transition, so it never ends. The cleanup that deletes its state machine may wait on it, and `polyris-up` would then report a failure after its hour although the update succeeded. A pipeline's run left running would also block the deletion of the pipeline's stack later. Nothing adopts a 1.2 run, so `polyris-up` stops them:
- **Which.** Every execution of the platform's Standard state machines and of the pipelines' state machines that started before the 1.3 update ran. A pipeline redeployed meanwhile, from CI or another terminal, keeps its first 1.3 run. Express executions end within five minutes on their own.
- **When.** Once the stack reaches `UPDATE_COMPLETE_CLEANUP_IN_PROGRESS`, when the eight are being deleted and no new execution of them can start. In TR-0 the cleanup waited on a state machine whose executions ran, 10 minutes until the probe stopped them. `ListExecutions` and `StopExecution` worked on a state machine being deleted, and its deletion ended about a minute after the stop.
- **In what order.** The starters first: `bulk_backfill`, the pipelines' executions and the wrappers; then `run_task`. It lists again until a listing comes back empty, since a wrapper can start `run_task` between a listing and its stop.
- **Again on the next run.** The step is idempotent and runs on every `polyris-up` while such executions remain, so an upgrade interrupted after the update still finishes it.
- **Reported.** It prints the runs it stopped, so that the user can start them again after the redeploy.
- **Permissions.** It finds the pipelines' state machines in the registry, and through `states:ListStateMachines` where the registry lacks one. It also needs `states:ListExecutions` and `states:StopExecution`, which `IAM_PERMISSIONS.md` documents.

A 1.2 pipeline can still be started until its redeploy: by Run in the console, by its schedule, or by a fact for a push consumer. Such a run fails at once, since the wrapper is gone, and the upgrade notes ask the user to run it again after the redeploy.

**Every pipeline is redeployed.** A normal `polyris-deploy` updates the pipeline's stack in place and deletes nothing. 1.3 deletes `dependency_wrapper` and the other old helpers outright, with no adapter and no stub. A pipeline that was not redeployed therefore fails at once: its run's start of the wrapper is refused with `StateMachineDoesNotExist`, and nothing hangs. Three things make a forgotten or mismatched pipeline visible:
- At the end of the upgrade, `polyris-up` lists the pipelines that still need a redeploy, with the command for each. It finds them in the registry, the way `polyris-down` does (ADR #131).
- `polyris-deploy` records a pipeline format version in the registry: one integer, a deploy-owned field written with `registered_at`. The console marks a pipeline whose version is missing or older than the platform's: "redeploy needed". The value is one SDK constant that both sides read (Principle #28), and it serves any later change of the format too.
- The same version travels in `Start_Run`'s arguments, and `Start_Run` refuses a format it does not support (§1). A deploy across this change stops earlier: `polyris-deploy` reads the platform stack's outputs, and the wrapper's output goes while `Start_Run`'s comes.

**The deploy role first.** Before the upgrade, add `dynamodb:PutItem`, `dynamodb:BatchWriteItem`, `dynamodb:BatchGetItem` and `dynamodb:Query` on `pipeline-specs` to the role that runs `polyris-deploy` (§8). Without them, every redeploy, which the upgrade makes mandatory, stops at the write of its specs, with the cause.

**Runs from 1.2 in the console.** They have no run row and no `spec_id`. The Runs tab, without a filter or with a pipeline filter, the pipelines list and the pipeline page's history and date ranges show only runs started on 1.3 (§5). The Tasks tab, the pipeline page for a chosen date and a date filter still find the older ones until their rows expire, 31 days after their last write. The upgrade writes no run rows for 1.2 runs. A pipeline whose last 1.2 run failed therefore leaves the pipelines list's **Needs attention** until its first 1.3 run, so the upgrade notes ask the user to handle failed runs before upgrading. The bell shows no failure of a 1.2 run, whose rows have no entry of the new kinds (§6).
- **Their graph** comes from their 1.2 DAG snapshot, through the console's existing reader: step 1 of `get_pipeline_dag` in `routes/pipelines_info.py`. 1.3 keeps that reader for runs without a `spec_id` only. This is an expand and contract change: 1.3 keeps the old path beside the new one, and 1.4 removes it.
- **Release 1.4 removes the reader** and its tests, and a guard test fails from version 1.4.0 while the reader exists. The skip of `dag_snapshot::` rows in `should_skip_token_row` and `is_internal_record` stays until the last snapshot has expired, 120 days after the last 1.2 run, unless the upgrade to 1.4 deletes the leftover rows. Without it, a leftover snapshot would show as a task without a status.
- **Their backfill records** carry no `date`, so the search of backfill records, a query of `pipeline-date-index` in 1.3 (§5), no longer finds them. They drop out of the bell and the runs feed, and expire 30 days after their creation.

**The template change.** It is one change set, and the CHANGELOG's "Upgrading to 1.3.0" gives the steps.
- **Added:** `Start_Run`'s function, with its role and log group; the tasks table's stream and its event source mapping; `run-start-index`; the `pipeline-specs` table; and the stack outputs that `polyris-deploy` reads, `Start_Run`'s ARN and the name of `pipeline-specs`.
- **Removed:** eight state machines and their seven log groups; the `query-subscriptions` function, with `QuerySubscriptionsRole`, `QuerySubscriptionsLogGroup` and `QuerySubscriptionsPermissionFromSfn`; and the `DependencyWrapperArn` output with its export. A stack of your own that imports that export stops the update. The 1.2 wrappers' logs go with their log groups, which the CHANGELOG says.
- **IAM:** the dispatcher's role reads the tasks table's stream (`dynamodb:DescribeStream`, `GetRecords`, `GetShardIterator`, `ListStreams`) and `pipeline-specs`. It may start, describe and stop `run_task` and pipeline executions, and answer a run's token. The sweeper's role gains `dynamodb:DescribeTable` on the tasks table, and reads `pipeline-specs` through the dispatcher's managed policy, which it attaches already. `Start_Run`'s own role writes the run's rows and pointers and reads `pipeline-specs`. `ConsoleApiRole` may read `pipeline-specs`; its grant on the tasks table's indexes (`pipeline-tokens/index/*`) covers the new index. It no longer needs `states:DescribeExecution`, nor the calls that answered or kept alive a token.
- **Names kept:** the dispatcher keeps every logical ID and physical name of `reconcile`, for its function, role, managed policy and log group. A rename would replace them, and the managed policy's fixed name would collide (`EntityAlreadyExists`, Principle #33).
- **Tables:** the change replaces or removes no table. DynamoDB creates at most one global secondary index per table update, so `run-start-index` is the only index that 1.3 adds. TR-0 confirmed that CloudFormation takes the index and the stream on one table in one update, with the new table and the deletions, and that a rollback of that update restores the table.
- **Template tests:** `pipeline-specs` has no TTL, no `DeletionProtectionEnabled` and no `Retain` policy, since a table a rollback left behind would block the next `polyris-up`. The mapping's `FilterCriteria` is checked against fixture records (§7).

**A failed update rolls back.** `polyris-up` rolls back a failed update by default. CloudFormation deletes removed resources only in the cleanup after a successful update, so a rollback restores all of 1.2, its state machines included. With `--disable-rollback`, the update keeps what it changed and is fixed forward.

## What is removed

| Where | What |
|---|---|
| State machines (8 of 13) | `dependency_wrapper`, `registration`, `notify_dependents`, `failure_handler`, `pause_waiter`, `restart_task`, `restart_wrapper`, `register_pipeline`. `run_task` shrinks from 59 states to about 20–25, and its work states lose their `States.Timeout` retries |
| Lambda functions | `query-subscriptions`, with its role, log group and permission (*Migration*) |
| Pipeline definition | the branch per task; `Save_DAG_Snapshot`, its warning state and its `dag_snapshot::` rows (§8). The console reads the snapshots of 1.2 runs until 1.4, and they expire 120 days after their write (*Migration*) |
| Registry | the `tasks` and `dag` fields, with each task's `task_config`; `Register_Pipeline` writes the `spec_id` and `task_count` instead (§8) |
| Coordination code | the dependency signals, while the closed-token codes stay for the run's token; dead tokens, zombies and the liveness deadline of waiting rows; the run's answer from every writer but the dispatcher |
| Sweeper | the pause-release step and the adoption scans |
| Console | the route `/api/execution-extend` and the "+12h" button. The route `POST /api/pipeline-register`, which nothing called and which wrote the registry's `tasks`: it is deleted on this branch already, and the console serves 27 routes, 26 once `/api/execution-extend` goes. In task actions, the notify of dependants and the run's answer. The marking of tasks whose run's execution ended, at read time, and `reconcile_execution_status` with the execution statuses `timed_out` and `recovered` (ADR #22). `check_with_sfn`, `reconcile_sfn_status`, `_reconcile_running` and `RECONCILE_DEADLINE_SECONDS`, which checked running runs against Step Functions in the pipelines list and the run lists (§5). The bell's read of task rows by date (§6). The call to the reconciler on resume; the start of `restart_task`; the link to a task's wrapper execution. The registry lookup of a task's configuration and outlets (`_lookup_dag_node`), since they come from the spec version. The lookup of a run's graph in the registry's `dag`. The scans of the whole table for backfill records, which a query replaces (§5) |
| Stack outputs | `DependencyWrapperArn` and its export |
| Tasks table | on task rows, `wait_token`, `pause_token`, `wrapper_execution_arn` and `orchestration_token`, and `result`, since the output lives in `output#` only. Also on task rows, `starter_input` and `task_config`, which the decision fields, the spec version and the run's input item replace. The `waiting_delay` status, and `aborted` as a status anything writes. The pause flag rows, which the run row replaces (§5). The task-dependency pointers in `dep-subscriptions`, and the `paused#` entries of `attention-index` |
| Timeline | the `WRAPPER_STARTED` event |
| Warnings | the `fact` kind, and the `failed_facts` of Mark Successful, since a status and its facts are one write; the `zombie` and `notify` kinds, which only removed paths write |
| DSL | the step classes `Choice`, `Map`, `Sensor` and `ShortCircuit` and the `Condition` helper; the thirteen `DAG` fields that nothing reads (*Consequences*) |
| 1.1.4 compatibility | the close of skips that pre-upgrade wrappers consumed, the `fact_version` gates, the cutover taken from `registered_at`, the `$exists` guards for older callers, the registry lookup of an old fact's granularity, the `UpstreamFailed` mapping and `run_task`'s `attempt` conditions. The generation's branches for rows without one go too, so the fence becomes `generation = :g` |
| Tests | the tests of the deleted templates and step classes; the test of the wrapper's execution names, which a test of `run_task`'s names replaces; most run-answer tests; the harness's frozen v1.1.4 wrapper; the scenarios and probes that test only tokens |

## Consequences

**Version.** The release stays 1.3.0, though by the letter of SemVer it is a major one. Two console routes and a stack output go, an unchanged `dag.py` behaves differently, and a `dag.py` that sets a `DAG` field nothing read stops at that line. A 2.0 before there are users would spend the major version on an internal rewrite. The CHANGELOG opens with a "Breaking" block, above "Changed", and states the policy:

> Until 2.0, a minor release may change how runs behave, remove console routes, stack outputs and DSL parameters that had no effect, add deploy-time limits, and require upgrade steps. Each such change is listed under "Breaking", and `polyris-up` checks the upgrade's preconditions itself.

**For the user**, in the 1.3 CHANGELOG. Under "Breaking":
- Every pipeline needs a `polyris-deploy` after the upgrade. Until then its runs fail at once, and the console marks it "redeploy needed".
- `polyris-up` refuses to upgrade a platform older than 1.2.0, a platform with a run in flight, and one whose 1.2 adoption is unfinished or less than 48 hours old. An explicit flag overrides the last two checks, never the first. Disable the pipelines' schedules before the upgrade; the redeploy turns them back on.
- A failed task no longer ends its run at once. After the decision, its dependants follow their trigger rules (ADR #138), independent tasks run to completion, and the run ends when every task has finished. Until then the console shows the run `running`, with the number of its failed tasks.
- A Stop of a task is final for its run: its dependants' trigger rules count it as a failure (ADR #138). A later Restart reruns that task only; Restart with dependants also reruns the dependants that the Stop decided.
- The work of a task has no time limit by default; before, it had 24 hours. `execution_timeout` now limits each attempt, and a timeout is a failed attempt; before, it bounded the whole task. A timeout ends the attempt, but a Lambda invocation runs on until its own timeout.
- Waiting for tasks of the same run has no limit of its own, and waiting for `wait_for` facts gets 7 days by default, counted from when the task's own dependencies are met. A set `orchestration_timeout` still caps all waiting.
- A pause lasts until it is lifted and uses up no waiting limit. The "+12h" button and `/api/execution-extend` are gone, and on resume every ready task starts at once.
- The stack output `DependencyWrapperArn` and its export are gone, and the 1.2 wrappers' logs are deleted with their log groups.
- `polyris.TERMINAL_STATUSES` is gone: it merges into `SETTLED_STATUSES`, which also holds `stopped`. `FAILURE_STATUSES` gains `stopped`, and `derive_execution_status` returns `running` until every row is final (§4). `waiting_delay` leaves the statuses, so a leftover 1.2 row in it shows the icon of an unknown status.
- The step classes `Choice`, `Map`, `Sensor` and `ShortCircuit`, and the `Condition` helper, are gone. They never had a generator, so a pipeline that used one failed when it was generated.
- `DAG` loses thirteen fields that nothing read: `timetable`, `catchup`, `default_timeout`, `params`, `template_searchpath`, `template_undefined`, `render_template_as_native_obj`, `max_active_tasks`, `max_active_runs`, `dagrun_timeout`, `sla_miss_callback`, `access_control` and `doc_md`. A `dag.py` that sets one fails with an error that names it: remove the line, since it never had an effect. A default time limit for every task is `execution_timeout` in `default_args`.
- A task may list at most 98 outlets, and each asset once. The decorator rejects an asset listed twice, and `polyris-deploy` refuses more outlets, or a task whose spec exceeds `run_task`'s 256 KB input (§8).
- Before the upgrade, the role that runs `polyris-deploy` needs `dynamodb:PutItem`, `dynamodb:BatchWriteItem`, `dynamodb:BatchGetItem` and `dynamodb:Query` on the new `pipeline-specs` table. The role that runs `polyris-up` needs `states:ListExecutions`, `dynamodb:GetItem` on `pipeline-tokens` and `dynamodb:Scan` on the registry for its checks, and `states:ListStateMachines` and `states:StopExecution` for its stop of 1.2's executions.

Under "Changed":
- Waiting limits, the decision timeout and the end of a `wait_before` countdown fire up to 5 minutes late, so a 30-second countdown can take over 5 minutes.
- A Stop of a waiting task writes `stopped`, like any other Stop; nothing writes `aborted` any more.
- Stop of a run and Restart with dependants run in the background: the console shows the run stopping, or its dependants resetting, until the dispatcher has done it. That takes seconds on a small run and longer on a large one, and the counts in the run lists can lag by seconds.
- A task that runs after an upstream that did not succeed gets that upstream's status from its own run, and its output only when an operator or the run skipped it. Reading the output of an upstream that failed raises `XComUpstreamFailedError`; before, a task could read an older run's output of the same date. An EMR step gets no statuses, since it gets no environment variables (ADR #123).
- A run whose only failure is a direct step shows `failed` in the console, as its execution does; before, the console showed it `success`. The failed step also shows in the bell.
- The console no longer checks a run's status against Step Functions, and the run statuses `timed_out` and `recovered` are gone (ADR #22). A run stopped in the AWS console shows `running` until the engine's next check of it, up to about 30 minutes.
- The bell shows every failure of its window and every open decision request, whatever the date of the run; before, only those of the last three logical dates.
- The task detail shows why a task waits: the reasons of the engine's last decision, with their time, the deadline of the wait, and each upstream's verdict.
- In the Step Functions console, a run shows one waiting state (`Start_Run`) instead of a branch per task, and a waiting task has no execution. The work of a task still shows as a `run_task` execution, with its graph, inputs and error. The polyris console shows every task of a run from its first second. This gives up what ADR #20 chose Step Functions for in a waiting task, and the claims in `README.md` and `docs/operations/UI.md` change with it.
- A run's task rows are kept a year and 90 days from their last write, its timeline events and task inputs 90 days, and the outputs of a date a year from their last write. Before, task rows, timeline events, outputs and inputs were kept 31 days, and the DAG snapshot 120 days (§5).
- A Restart is refused once its run's history expires within a day, since rows of the run may already be gone (§3).
- The Runs tab without a filter shows the last 90 days; before, 14. With a pipeline filter it shows the same 90 days. Both show, like the pipelines list, only runs started on 1.3. The Tasks tab keeps 14 days (§5).
- `polyris-deploy` refuses a pipeline above the declared maximum of about 2,000 tasks, and measures the definition as it sends it. Before, a definition of about 230 tasks passed the check and failed at deploy (§8).
- `polyris-output --json` prints the definition, which refers to the task specs instead of holding them, and the specs.

Under "Added":
- Restart with dependants: one action that restarts a task and the dependants that its final failure or its Stop decided, in the same run.

**Pipeline size.** A pipeline may hold about 2,000 tasks, the declared maximum (§8); in 1.2 the definition's 1 MB stopped one at about 200. For comparison, Azure Data Factory caps a pipeline at 80 activities, and Airflow has no hard cap but is slow on DAGs of thousands of tasks.

**Operations.** 13 state machines become 5, and the `query-subscriptions` function goes. `Start_Run`'s function is new: it lives in the coordination package (`sam/lambdas/coordination/`), with its own role and log group. The tasks table gets a stream, an event source mapping to the dispatcher, and one index, `run-start-index` (§5). The dispatcher keeps the names of `reconcile`, and its role grows as *Migration* lists.
- A new table, `pipeline-specs`, holds the task specs (§8). `Start_Run`, the dispatcher, the sweeper and the console may read it.
- The role that runs `polyris-deploy` needs four DynamoDB actions on `pipeline-specs` (§8), and the role that runs `polyris-up` needs the permissions of its checks and of its stop of 1.2's executions (*Migration*). `IAM_PERMISSIONS.md` documents both.
- The docs that count resources change with the template (`make test-docs`): 7 DynamoDB tables become 8. So do the DynamoDB Tables and State Machines tables of root `CLAUDE.md`.

**Cost.** These are estimates at on-demand prices in us-east-1. TR-0 and the build's two dev deploys measure them, and the measured numbers replace these.
- **Step Functions.** A task takes about 47 Standard transitions today, measured on the coordination harness; this design is estimated at 16–22. It makes one `StartExecution` call instead of three calls and an Express execution. A waiting task still costs nothing, because it is a row (ADR #19).
- **The total.** At 1,000 tasks a day, in daily pipelines of 10–50 tasks, 1.2 costs about $36 a month and 1.3 about $14–19, without the console.
- **A daily 2,000-task pipeline,** which 1.2 cannot deploy, costs about $46 a month. Step Functions takes about $28.5 of it, at 19 transitions a task. DynamoDB takes about $17, the figure of §8: about $15 for the reads of the full passes (below) and about $2 for the writes. The sweeper's checks add about $0.4, and the dispatcher's Lambda time about $0.4, for about 2,000 passes of 1–2 seconds at 256 MB. Its logs cost cents under the rule of §7.
  - Revision 6 gave $47–50 for the same whole, when its checks still rewrote every due row (*Alternatives*). §8's $17 is DynamoDB alone.
- **The full passes grow as N².** A run of N tasks makes up to about N full passes, one per batch of changes that wake the dispatcher, and each reads every row of the run with a consistent read.
  - A 2,000-task run therefore reads about 2,000 × 2,000 = 4 million rows, for about $0.5 per run. That is about $15 a month for a daily such pipeline, about $75 for 10,000 tasks a day as five of them, and about $360 for an hourly one.
  - It stays below 1.2's Step Functions cost for as many tasks: about $350 a month at 10,000 tasks a day, and about $1,700 for the hourly pipeline's 48,000. A run of 50 tasks reads 2,500 rows.
- **The console's polling.** The bell and the pipelines list poll every 30 seconds. For each console open 8 hours a day at 1,000 tasks a day, 1.2's bell costs about $30 a month in scans for backfill records and $1 in its read of three dates, and its pipelines list about $4. At 10,000 tasks a day the last two become about $10 and $40. 1.3 reads index entries and run rows only: cents a month at 1,000 tasks a day, and about $0.5–2.5 at 10,000, since the pipelines list grows with the number of runs.
- **What multiplies it**, to be measured:
  - the size of a task row, now bounded at about 1 KB because the spec stays off it (§8);
  - the passes per run (above), and the checks of a waiting run's run row, a full pass each, at most 30 minutes apart. A check writes only the due times that changed, and the run row's next one. A daily 2,000-task pipeline that waits most of the day pays about $0.4 a month for them, and 90 runs of 2,000 tasks that wait all month about $33, almost all of it reads;
  - the reasons of a `wait` decision. They hold no time, and a pass writes them only when they differ from the row's, which a test pins (§1). Reasons that changed at every pass, such as an age, would rewrite every waiting row at every pass, about 6 write units each with the index copies: up to about $15 per 2,000-task run;
  - the indexes: a task row's status write also reaches `pipeline-execution-index`, which projects every attribute, both date indexes and `attention-index`, five writes in all;
  - transactions: a decision's transaction bills both its items, the row's write and the `ConditionCheck` on the whole run row, each at twice the units of a plain write (§3);
  - the logs. A line per row per pass would be N² too: about 800 MB for a 2,000-task run at 200 bytes a line, about $0.4 a run and $12 a month for the daily pipeline. So a pass logs one line, and one per row it changed (§7);
  - point-in-time recovery, off by default, at $0.20 per GB-month on top of $0.25 for the storage;
  - the size of `output#`, up to 350 KB and kept a year, and of `input#`, up to 380 KB and kept 90 days.
- **Asset wakes.** A narrow pass reads about 80,000 items for the 90 facts of the example in §7, about a cent, instead of 16 million, about $2.
- **Spec storage.** With each text stored once, the example of §8 costs about $0.2 a month after a year, instead of $9.
- **The run row.** With a 100 KB run input on it, as revision 5 had it, a 2,000-task run would cost about $0.4 in the decisions' checks and the summary's writes; under 4 KB, under a cent (§3).
- **Smaller items.** `Start_Run` reads its pipeline's spec version once per run. The dispatcher reads a task's spec once per start, and the run's input once per pass that starts tasks. The Runs tab reads one page of run rows, instead of every task row of 14 dates. The longer retention costs about $0.7 a month in storage at 1,000 tasks a day, from 1.1 KB rows (§5), $0.3 of it for keeping the history after the run's end.

## Alternatives considered

- **M1, a thin wrapper.** `dependency_wrapper` would keep its ARN and become "write the row and end". The pipeline's state machine would keep a token per task, and the dispatcher would answer each one. M1 existed only to keep old pipelines working without a redeploy. With the redeploy mandatory, nothing needs it, and it would keep both the token per task and the run's end at the first final failure.
- **M3, a run as a row too.** The scheduler and the console would call the dispatcher directly, and a pipeline would have no state machine. Rejected: nobody has asked for it (Principle #18). `bulk_backfill` waits for each run through `startExecution.sync:2`, the direct steps would need a new home, and it would be a 2.0.
- **Polling in the wrapper:** its cost, and the 25,000-event history limit of an execution.
- **Compiling the DAG into ASL:** it cannot skip a task, mark one successful or pause a live run.
- **Lambda durable functions:** the same model of a process waiting on a token.
- **An explicit call to the dispatcher from `run_task` and the console instead of a stream:** every present and future writer would have to remember it.
- **An EventBridge rule on the end of `run_task` executions,** to fail the row of a `run_task` that died within seconds. The sweeper's check must exist anyway, a dead `run_task` is rare, and an event names an execution, not the row's current generation. It can be added if the delay proves to hurt.
- **The task specs from the registry or from the run's DAG snapshot.** The registry lacks the specs (no ARN, configuration, trigger rule or limits). Both are written best effort, by a state with the same 256 KB limit, and a redeploy changes the registry under a run that is still waiting.
- **Every spec in `Start_Run`'s arguments.** A state's input holds 256 KB: about 250–600 tasks, with room left for the token and the run's input.
- **`Start_Run` reads its own definition** (`DescribeStateMachineForExecution`). The 1 MB cap of a definition stays, the call refills at 1 per second per account and Region with a bucket of 200, and AWS documents it as eventually consistent and best effort.
- **The specs in chunks across several `Start_Run` states.** They stay under the 1 MB cap of a definition.
- **The specs as one object in S3.** It adds a second service to the path that starts a run, with a bucket and a cleanup of its own.
- **The full spec on every task row,** as revision 4 had it. Every full pass reads every row of its run, and DynamoDB bills the whole item, so the cost of each pass would grow with the size of the specs (§8).
- **What revision 5 did, and revision 6 replaced.** It copied every spec into each spec version (§8), kept the run's input on the run row (§3), and checked each direct upstream's generation, which a task with more than 99 upstreams could not fit in a transaction (§1). It also made a full pass for every asset wake (§7). Two of its steps repeated others: a first pass inside `Start_Run`, which the activation's wake already makes, and an hourly check of each new spec version, which the deploy already makes.
- **The run lists deriving each run's status from its task rows.** A page of 25 runs of 2,000 tasks would read about 50,000 rows.
- **The pipelines list as 1.2.1 has it:** every task row of 14 dates, and a `DescribeExecution` for each pipeline whose latest run is `running`. In 1.3 runs wait for days: 60 waiting pipelines in 5 open consoles would make about 10 calls a second, the smallest refill rate AWS documents for that call, shared with the dispatcher, and `stats_incomplete` would stay on.
- **A new index or kind of row for the bell.** `attention-index` already orders rows by when they need attention, as it does for warnings (§6).
- **A due time for every unfinished row, with the backoff,** as revision 6 first had it, after ADR #130 invariant 5. Each check of the sweeper rewrote every due row, about 5 write units each with the index copies, so a large pipeline that waits for its data paid for its size at every check. 90 runs of 2,000 tasks that wait all month would cost about $840 a month, against about $33 now (*Cost*).
- **A restart counter on the run row,** as revision 6 had it, raised only by a Restart and its resets. A Skip or a Mark Successful of a final failure, a new token and the answered mark left it as it was. A decision, or the removal of the run row's due time, could then stand on a read taken before them. A `one_failed` cleanup could run after an operator declared its upstream good, and a run's newer token could stay unanswered. `run_version` is that counter, raised by every such write (§3).
- **No due time of its own for a `running` row,** with its liveness judged at its run row's check, 30 minutes apart instead of 10. One full pass would then call `DescribeExecution` for every running row of the run at once, where the rows' own due times spread those calls over time.
- **Bookkeeping on each row that a pass left,** to tell it from a lost hint. The grace and one field on the run row, `left_at`, do the same with no write per row (§7).
- **Refusing a run whose logical date is after its start day,** so that a range of dates could walk start days from its first date. The console's Run dialog fills in the browser's local date, a day ahead of UTC after local midnight east of UTC, and a run for a later date is a valid request. A range walks the 90 start days instead (§5).
- **The pause flag as a row of its own,** as in 1.2: one more kind of row, with its own stream pattern, TTL and read per pass.
- **Facts written in batches before the status,** to lift the cap of 98 outlets. It has four failure modes:
  - a batch put takes no condition, so it overwrites another producer's fact of the same millisecond, and a Step Functions state counts `UnprocessedItems` as a success;
  - a retried batch with a new `$now()` writes a second fact, so OR and `cascade='all'` consumers run twice;
  - nothing fences the batches by generation, so a zombie `run_task` writes its facts after a Stop;
  - a crash between batches leaves the facts of a task that ends `failed`.
- **The status first, with a "facts owed" mark, and the facts written by the dispatcher** (an outbox). It lifts the cap without those failure modes, but changes invariant 12 and Rule 41. It is deferred until someone needs it, for example a whole dbt project run as one task, with one asset per model.
- **A run limit derived from the task limits, or kept alive by a heartbeat from the sweeper.** A pause without end, a restart and a retry after a decision all break a derived limit, and under the new defaults it comes out at a year for almost every DAG anyway.
- **A `wait_for` default derived from the cadence of the awaited assets.** The cadence says how often an asset is written, not how late a fact for a given date may arrive. Airflow's sensors and Azure Data Factory's window dependencies use a fixed 7 days by default too.
- **A run's history kept 31 days from each write, as in 1.2.** A run that lives longer loses its rows while it runs. The console's window of 90 days is as long as Step Functions keeps an execution's history; Azure Data Factory keeps 45 days. A monthly pipeline shows its last three runs. A history of years belongs in an export, not in the console.
- **Timeline events with the history's long TTL.** They would take most of the table's space. A run older than 90 days keeps its rows but not its timeline, which only the console shows.
- **Shortening the history's TTL to 90 days once the run is answered,** as revision 7 had it. It saves about $0.3 a month at 1,000 tasks a day, about 2% of the cost, and $2–3 at 10,000. It needs a reset in passes, its record on the run row, and the run row due until the record. It also needs a condition on `run_version`, so that a Restart in between leaves the reset unrecorded: one more class of races, with its harness scenarios.
- **A 1.2 run's graph drawn from its task rows** (the inferred step) instead of its snapshot: that graph lacks the task types, the trigger rules and the groups.
- **A deadline after which a stopped row becomes `aborted`.** None of the common orchestrators holds a run open while it waits for a restart, and a restart stays possible after the run has ended.
- **Ending a run at the first final failure, as the default or as an option:** this is what 1.2 does. The first final failure fails the run's `Parallel`, so the run shows `FAILED` while the other tasks' wrappers go on working.
- **A `dependency_wrapper` stub that fails with a hint:** a leftover to keep. The console's mark and the list from `polyris-up` show the same thing.
- **Adopting 1.2's waiting rows during the upgrade:** it needs code for 1.2 runs that no later release uses; a full drain needs none.
- **Writing run rows for 1.2 runs during the upgrade,** so that the run lists show them: it needs code that 1.4 removes, for a gap that closes by itself when their rows expire, within 31 days (*Migration*).
- **A direct upgrade from 1.1.4:** it would keep the 1.1.4 compatibility code for another release.
- **A 1.3 with only the removals, and rows in 1.4:** two upgrade windows instead of one.
- **2.0 instead of 1.3.0:** a major version spent on an internal rewrite before there are users. The "Breaking" block warns where the number does not.

## Out of scope

- An `on_failure` hook in the DSL that reacts to a failed row. The notify calls in `run_task` stay as they are.
- A fail-fast option.
- Branching, mapping over items and short-circuiting as features, which the removed step classes promised.
- Freshness checks on the assets that a task waits for.
- A sweeper interval under 5 minutes, if the precision of the limits proves too coarse.
- The other items ADR #130 deferred: queue-based fan-out, subscriptions keyed by partition, a producer run id on each fact, and a share of the sweeper's budget for each step.

## Risks

| Risk | What answers it |
|---|---|
| Logic moves from Step Functions `Retry` into Lambda code | Write first, then start; execution names derived from the row; a sweeper check for every status in between |
| Races move into the Lambda, where the harness cannot interleave them yet | The harness interleaves at the boto3 boundary ("the dispatcher read, the operator wrote, the dispatcher writes"), built before the code (*Tests*) |
| Every start goes through one Lambda; today a root task starts without the reconciler | The System status shows a stream consumer that stopped (§6), and a lost hint costs at most the run row's next check, up to 30 minutes (§7) |
| A burst of `StartExecution` calls on a wide fan-out | A throttled start leaves its row `deps_ready` and due, and one that keeps failing warns once and fails at its deadline (§5). Today a task makes three starts |
| A stream on a busy table | The mapping's filter, a batching window and one pass per run per batch; Rules 51 and 52 |
| The sweeper's queue holds a run row for every run with an unfinished row, and each check of it is a full pass over the run | One pass per run per sweeper run, earliest due first, and the oldest overdue row in the self-check; TR-0 measures 90 waiting runs of 2,000 tasks against the sweeper's budget |
| A row that waits only for other rows has no due time of its own, and a race can remove a row's due time | Every full pass judges each row from the row itself and puts back the due times that differ (§1). The run row stays due while a row is unfinished, its removal conditional on `run_version`; the harness checks invariant 9 in every scenario |
| One lost record, such as a Resume's, can leave thousands of rows to one check | The check decides them in one pass and writes one warning for the run, with their number (§7) |
| A full pass reads every row of its run | About N² row reads for a run of N tasks, on rows of about 1 KB (§8), and a narrow pass for an asset wake (§7); to be measured at the declared maximum |
| The run row has many writers, and passes write its summary | Each writer updates its own fields with a conditional write, and a pass writes the summary only when it changed and never over a newer one (§4). The removal and the moves of its due time are conditional on `run_version` (§3) |
| A date filter, and an opened run, still read task rows: those of one date, or of one run | TR-0 measures them at the declared maximum (§5) |
| Every decision's transaction checks the run row, so a run's decisions are written one at a time, and concurrent passes cancel each other's | A conflict is retried within the pass's budget, a row a pass leaves is no lost hint (§7), and TR-0 runs two passes over 2,000 roots. A raised `run_version` refuses the decisions read before it, which a pass takes again from a new read (§1) |
| Resume now starts tasks too | A time budget per pass, with the rest left to the next pass, which `left_at` wakes (§7) |
| One broken run fails its pass at every check | The sweeper moves its run row's due time by the backoff and warns, so it holds neither the sweeper's budget nor the System status. A sweep where most passes fail still turns the System status red (§7) |
| A record that keeps raising a transient error holds its shard for up to 24 hours | The lost-hint warnings and their count in the System status show it; `LastProcessingResult` does only now and then (TR-0). The runs it holds go on at their run rows' checks (§6) |
| A fact is recorded during the upgrade for a push consumer that waits for its redeploy | The consumer's run fails at once, and its deterministic name then keeps the catch-up from starting it again: re-run that date by hand |
| A definition refers to specs that were never written: after `polyris-down` and `polyris-up`, after items deleted by hand, or when other tooling applies the `--dry-run` template | Its runs fail at `Start_Run` with a warning. Run `polyris-deploy` again: it writes the items |
| Every run that starts on one day writes to one partition of `run-start-index`, at its start and at each change of its summary, and a throttled index throttles the table | The index holds run rows only, not task rows, and a pass writes a summary only when it changed (§4). TR-0 measures these writes under a schedule burst |

## Tests

The harness comes first, before the code it tests, and each test comes before the code that turns it green.

- **The harness interleaves at the boto3 boundary.** It wraps each component's client and pauses after a read returns or before a write is sent, with no hook in production code (Principles #11 and #14).
  - It can drop or delay a record of the tasks table's stream, expire TTL rows with their `REMOVE` records, return a partial `BatchGetItem`, and run two passes over one run at once.
  - It models `State.RetryCount`, `Execution.RedriveCount` and `States.DataLimitExceeded`.
- **Invariants are staged.** Each invariant is checked from the step of the build that adds its mechanism. A scenario that needs a later mechanism is skipped until then, with that reason (`needs_binding`). Every check is shown to flag a planted violation, and invariant 8 is checked in both directions (§2).
- **Liveness at the end of a scenario.** The harness moves the clock past every deadline and sweeps until nothing changes. Then every run has ended or is paused, or a row waits for a decision with a due time.

| Decision or invariant | Test | Where |
|---|---|---|
| Invariants 1–12, both directions of 8, liveness | a check each in `test_invariants.py` | harness |
| Transitions, writers and wake-ups (§3, §7) | `test_task_row_writers.py`, with every writer of the run row and whether it raises `run_version`; every transition through the template's `FilterCriteria` and the predicate, with fixture records | unit |
| Contracts and pure functions (§9) | a parity test per contract; a test per clause | unit |
| Due times (invariant 9, §9) | the due-time function, with no clock among its inputs: none for a row that waits only for other rows, nor in a pause for a held row or a limit. A decision deadline far in the future in a pause | unit |
| Judged from the row (§1, invariant 9) | a due time removed by a race: the limit still fires, and the due time comes back at the run row's next check. A pause's removal refused after a Resume; `wait` reasons with no time in them, written only when they changed | unit, harness |
| Trigger rules (ADR #138) | the truth table against `evaluate` | unit |
| The run row (§3) | the largest row under 4 KB; a Pause past ten intervals merges the two oldest | unit |
| Specs and limits (§8) | a version item or a text dropped or altered stops the deploy before `create_change_set`; a text written once; golden spec files; `1.0` read back as `1`; outlets, duplicates, and bytes as sent | unit |
| No reader of the registry's `tasks` or `dag` (§8) | a census, which exempts `stores.Registry` until the change that deletes it | unit |
| `Start_Run` (§1) | it returns once active, and the activation wakes the first pass; the stop mark refuses a late attempt; a mark written first survives; the token order; the `Catch` fails after the `Parallel` | harness |
| Passes (§7) | one pass per run per batch; a pass over a `creating` run returns at once; a dropped or delayed record. A pass out of budget wakes the next through `left_at`, and a run where no write gets through wakes at most one more; a summary written only when it changed. A pass over an unchanged run writes one log line, and a stream pass calls no `DescribeExecution` | harness |
| Lost hints (invariant 9, §7) | a dropped stream record, a lost activation and a lost Resume heal at the run row's next check, within 30 minutes, with one warning per run that counts the rows. On a busy run, rows within `LOST_HINT_GRACE`, rows found under `left_at` and rows whose countdown ended count for none | harness |
| Narrow passes and conflicts (§1, §7) | a narrow pass on an asset wake; two passes at once, with conflicts; a decision refused after a Restart, or after a Skip or a Mark Successful of a final upstream, and taken again from a new read | harness |
| A pass's own raises (§3) | an operator action between a pass's read and its own raise. A Mark Successful on another branch while a Restart with dependants resets: the next reset fails, and the pass decides the dependant from the success. A Restart between a read of every row final and the answered mark: no answer, and the run row stays due | harness |
| A start that keeps failing (§5) | throttled starts: one warning after 15 minutes, then `failed` after 6 hours; a denied start counts toward neither | harness |
| The bell (§6) and the date indexes (§5) | a failure of an old date and an open decision request show; a Restart removes the entry; one notification per run; a census of the date-index reads | unit, harness |
| Retention (§5) | the TTL of each kind of row, and the history's TTL left as it is after the answer | harness |
| Retention across a Restart (§5) | a Restart's renewal, which writes no row that TTL deleted; a Restart refused when the run row's TTL is less than a day away | harness |
| A missing row (invariant 11) | an expired upstream row fails its waiting dependants; a task row without a run row is stopped | harness |
| The template (*Migration*) | `pipeline-specs` without TTL, deletion protection or `Retain`; the `reconcile` names kept; the new outputs | unit |
| `run_task` (§1) | the success transaction and its retries; `States.DataLimitExceeded`; a timeout per attempt; the upstream statuses and `XComUpstreamFailedError` | harness |
| Operator actions (§3, §5) | each action's transaction; requests carried out in passes; Restart with dependants racing a pass, until a pass resets nothing; a Pause or a Stop of the run racing a pass. A Restart once the stop is done; the stop mark of an ended execution written only when no mark is set | harness |
| The run row's due time (§3) | a Restart, a Skip or a Mark Successful after the run's end makes the run row due again, also when it races the pass that removes that due time. A new token or a redrive racing the removal keeps it; a sweeper's move is refused after a write that raised `run_version`. A failed pass moves it by the backoff and rewrites one warning row. A sweep where most passes failed, and at least two, reports a failed step, and one broken run alone does not | harness |
| Limits in a pause (§5) | a limit that falls due during a pause loses its due time and fires after the Resume, moved by the paused time; a decision deadline in a pause falls due far in the future | harness |
| Run lists (§5) | `run-start-index`, the 90-day window, the early stop, a rerun of an old date first, a pipeline filter, the summary; a range that finds a run started a day before its logical date; the pipelines list's contract and `stats_incomplete` | e2e |
| The snapshot reader of 1.2 runs (*Migration*) | a guard that fails from version 1.4.0 while the reader exists | unit |
| The upgrade (*Migration*) | from 1.2 with `polyris-up --archive`; each refusal of the checks; a forgotten pipeline; a deploy role without the new permissions; a forced rollback | live |
| Scale (§8) | about 200 tasks in the harness; then the declared maximum, live | harness, live |
| Game day | `run_task` stopped in the AWS console, the mapping disabled, a record that keeps raising a transient error, `StartExecution` denied, throttling, a pause with a fact, a restart during a start | live |

## To verify in TR-0

These AWS behaviours were reasoned, not observed, until TR-0 probed each on a scratch stack on 2026-11-18, with throwaway stubs where the probe needed code. Each item ends with what the probe showed, and the places that rely on it say what follows. This is the only list of probes. The *Scale* and *Cost* items need the dispatcher, the sweeper, `run_task` and the console, so the first test deploy (TR-7) measures them against thresholds the plan sets, and the release rehearsal (TR-11) measures them again. Until TR-7, the declared maximum of 2,000 tasks is provisional.
- **The upgrade's change set:**
  - a new index and a stream on `pipeline-tokens` in one CloudFormation update, together with the new table and the deletion of eight state machines, and the rollback of that update: held, and the rollback restored the table;
  - the deletion of a Standard state machine that has `RUNNING` executions, which fixes the moment `polyris-up` stops them: the cleanup waits for them, and a stop works on a state machine being deleted (*Migration and upgrade*);
  - how long DynamoDB takes to build `run-start-index` on a table of 1.2's size: about 8 minutes for 100,000 rows, after CloudFormation reported the table updated (§5).
- **Step Functions:**
  - what a failed attempt of a `lambda:invoke.waitForTaskToken` state does to the state: each attempt brings a new token, `RetryCount` counts them, and an earlier token is refused with `TaskTimedOut`;
  - `States.DataLimitExceeded` against a `Catch` on `States.ALL`, and whether a `Catch` that names it takes it when the state's arguments are literal: both took it from an oversized result;
  - `DynamoDb.TransactionCanceledException` and the payload size of the success transaction through the SDK integration: that error name, with the reasons in its cause, and a 200 KB item went through;
  - a redrive after a failed state, and one of an aborted execution: whether it runs `Start_Run` again, and the order of `Start_Run`'s tokens by `Execution.RedriveCount`, then `State.RetryCount`: a failed run's redrive runs no `Start_Run`, an aborted run's does (§1);
  - whether a work state of `run_task` that times out on a `.sync` integration stops its job: Glue, ECS, Batch, Athena and nested executions stopped within 60 seconds; EMR was not probed;
  - how long `DescribeExecution` takes to show a new execution: under 250 milliseconds.
- **Streams and DynamoDB:**
  - every filter pattern of §7 on live records: an insert without an `OldImage`, a change from one final status to another, and a TTL `REMOVE` record: exactly the expected records passed and no deletion did; a deletion by TTL itself was not waited for;
  - that an `anything-but` on the old image's status matches no insert, whose old image is absent, so `Start_Run`'s task rows wake nothing: held;
  - the four filters of §7 against the five that Lambda allows per mapping by default, and how many run-row records the predicate drops: accepted, and three or four run-row records per run;
  - what the mapping's `LastProcessingResult` shows while one shard retries a record that keeps raising, among healthy shards: OK again at the next healthy batch, so the lost hints carry the signal (§6);
  - DynamoDB storing the number `1.0` as `1` on a live table: it does;
  - the write throughput of `run-start-index` under a schedule burst, with the summary writes of the day's runs on the same partition: about 350 writes a second on one start day, without throttling.
- **Scale:**
  - the start of a run of about 2,000 tasks, with its time, its throttling and the read of `pipeline-specs`;
  - one dispatcher pass over 2,000 rows against the function's timeout, and two passes at once over a run of 2,000 root tasks, whose transactions conflict on the run row;
  - the sweeper's checks of 90 waiting runs of 2,000 tasks, a full pass each, against its 200-second budget;
  - the stream's backlog when 2,000 rows finish at once;
  - the sweeper's `DescribeExecution` calls when about 2,000 `running` rows fall due in one check, against the API's rate;
  - the Runs tab's walk with a pipeline filter over 90 start days, for a pipeline that ran once among others that run hourly;
  - a pipeline page's range, which walks all 90 start days, timed in the console's 256 MB function against its 30 seconds;
  - the reads of task rows that remain, on a pipeline at the declared maximum: a date filter, and an opened run.
- **Cost.** These numbers replace the estimates under *Cost*:
  - transitions per task, and passes per run with their reads, the N² of *Cost*;
  - the size of a task row at p50 and p95, and the sizes of `output#` and `input#`;
  - read and write units per task, with the sweeper's checks and the writes to the indexes;
  - the idle sweeper with its self-check;
  - the cost per 1,000 tasks.
