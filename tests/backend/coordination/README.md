# Coordination test harness

The coordination layer (ADR #130) spans generated pipeline ASL, the SFN templates in `sam/sfn_templates/`, the coordination Lambdas and DynamoDB. A bug can sit in any of them or between them, so the tests here run them together: the real templates, executed state by state, against moto tables built from `sam/template.yaml`, with the real Lambda code in-process.

Run them with the backend suite:

```bash
python -m pytest tests/backend/coordination
```

## Layers

| Layer | Where | Proves | Does not prove |
|---|---|---|---|
| Pure logic: `decide`, `trigger_rules`, `asset_conditions`, `partition`, `run_name` | `tests/sdk/test_coordination_*.py` | every decision, exhaustively, at 100% coverage | integration |
| Coordination Lambdas | `sam/lambdas/coordination/tests/` | handlers on moto tables from the template; error classification; the IAM role allows exactly the calls the code makes | Step Functions behaviour |
| Templates and Lambdas together | this directory | the real graphs, JSONata expressions, writes and token flows | AWS's JSONata dialect, timing, quotas |
| AWS itself | live smoke on a dev stack (`tests/e2e/`) | the dialect, IAM, streams, real timing | — |

## The interpreter (`sfn_runtime.py`)

`Runtime` runs JSONata-mode state machines and is, at the same time, the Step Functions client the Lambdas call (`send_task_success`, `describe_execution`, ...).

- **Modelled:** Pass, Task, Choice, Parallel, Map, Wait (instant), Succeed, Fail; Arguments, Output, Assign, Items, ItemSelector, Condition; variables copied into Parallel branches and Map iterations; Retry (immediate) and Catch; task tokens; `.sync` and `.sync:2` children; express `startSyncExecution`; fire-and-forget starts; stop; `$parse`, `$hash`, `$now`, `$uuid`.
- **Integrations:** DynamoDB calls go to moto. A Lambda invoke goes to the in-process handler bound with `Stack.bind_lambda`; an unbound function fails its state with `Lambda.ResourceNotFoundException`. A `.sync` call to anything outside the platform (the user's own task) becomes an `ExternalJob` that the test finishes with `Runtime.finish_job`.
- **Test controls:** `run()` runs until every execution waits or ends; `time_out(token)` fires a task timeout; `stop_execution` stops an execution as an operator would.
- **As TR-0 observed on AWS (ADR #137):** `$states.context.State.RetryCount` counts a state's retries, and `State.EnteredTime` stays the same across them; each attempt of a token state brings a new token, and the old one answers `TaskTimedOut`. `redrive_execution` takes up a stopped execution at the states in progress, with `Execution.RedriveCount` raised and the retry count back at 0, and a failed one at the top-level state where it failed. A state's arguments, result or output over 256 KB fail it with `States.DataLimitExceeded`, which a `Catch` on `States.ALL` takes too.
- **Undefined fails, as on AWS:** a `{% %}` value that evaluates to undefined fails its state with `States.QueryEvaluationError`, since JSON cannot represent undefined. JSON `null` stays `null`, and an object constructor drops a key whose value is undefined, as JSONata does. Every such failure is recorded in `Runtime.expression_errors`, caught or not.
- **Not modelled:** real time, the rest of AWS's JSONata dialect, quotas. The live smoke covers them.

`test_sfn_runtime.py` pins each of these semantics on small synthetic machines. A scenario is only as good as the interpreter under it.

## The stack (`platform_stack.py`)

`Stack` loads `sam/template.yaml`:
- every DynamoDB table becomes a moto table with its declared keys and indexes;
- every state machine with a `DefinitionUri` is loaded with its `DefinitionSubstitutions` resolved the way CloudFormation resolves them. A template wired to the wrong table therefore writes to the wrong moto table, and a substitution the template does not define fails the load;
- `deploy_pipeline(dag)` generates a pipeline's ASL with the real generator and loads it.

`test_platform_stack.py` runs a one-task pipeline end to end: pipeline execution → `dependency_wrapper` → registration helper → `run_task` → the user's task → success.

## Scenarios (`test_scenarios*.py`)

Scenarios speak only the `World` API in `world.py` and build pipelines only through `dags.py`, which uses the real SDK. A scenario therefore keeps its text while the templates and Lambdas under it change. Each one checks both sides of a wake: the task starts once its conditions hold, and never before.

| World verb | What it does |
|---|---|
| `deploy(dag, before_upgrade=)`, `start_run(pipeline, date, **inputs)` | generate the pipeline's ASL and start a run; `before_upgrade=True`, a pipeline generated before 1.3, raises `NeedsBinding("rows-migration")` until the upgrade from 1.2 is built |
| `fire_schedule(pipeline, scheduled_time)`, `start_bare(pipeline)` | start a run as EventBridge Scheduler does, with the input `polyris-deploy` gives the schedule; or with an empty input |
| `complete(run, task, status=, output=)` | finish the user's work for a task, a success with `output` |
| `write_event(asset, partition, writer=)`, `deliver_hints(asset=)` | record a fact, by `run_task` or as an older version stored it (`writer="older version"`); deliver its stream record separately, so tests choose the interleaving |
| `pause`, `resume`, `restart`, `skip`, `mark_success`, `fail_task`, `stop_task`, `stop_run` | operator actions through the console routes |
| `time_out_registration`, `time_out_pause_wait`, `stop_wrapper_externally`, `inject_failure`, `fail_starts`, `fail_signals`, `fail_invokes` | faults from outside: a timeout, a stop, a refused DynamoDB call (or, with `after=True`, an applied call whose answer is lost), a Step Functions start (a Lambda's or a state machine's) or callback (`call=` SendTaskSuccess or SendTaskFailure, `when=` to pick one), a Lambda that keeps failing (transiently, or with `error="Sandbox.Timedout"` as a timeout) |
| `hold(machine)`, `release(machine)`, `release_newest(machine)` | new executions of a platform state machine wait to take a step, to order a race (B-P3, B-R2) |
| `sweep()`, `clock.advance(...)`, `clock.jump_to(when)` | the safety net and the passing of time; every reading of the clock, by the stack, the Lambdas or the console, moves it on 1 ms |
| `expire_ttl(table=)` | DynamoDB's TTL deletes the rows whose `ttl` has passed; a stream on that table marks their `REMOVE` records as TTL's |
| `deliver_task_hints`, `lose_task_hint`, `delay_task_hint`, `release_task_hints`, `two_passes_at_once` | the tasks table's stream and the dispatcher's passes of ADR #137 §7 |
| `delete_row(run, task)`, `delete_output(run, task)` | a task's row, or its date's `output#`, goes missing (ADR #137 invariant 11, D109) |
| `delete_run_row`, `hold_start_run_retry`, `release_start_run_retry`, `shrink_pass_budget`, `restart_with_dependants`, `upgrade_platform` | 1.3's run row, a late `Start_Run` attempt, a pass's time budget, Restart with dependants and the upgrade from 1.2; the last two raise `NeedsBinding` until their stage |
| `run_to_the_end()` | the liveness check of ADR #137 *Tests*, which ends every scenario once `rows-core` is bound: it heals the world (the injected failures end; held executions, `Start_Run` retries, stream records and interleavings go on; a lost record stays lost), finishes the user's work still running, moves the clock to each due entry and sweeps until nothing changes, then `row_invariants.liveness`; it returns the violations, which fail the scenario |
| `plant(violation)` | a violation the test makes on purpose, to see a check find it: the end of the scenario does not count it |
| `status`, `row`, `running_tasks`, `task_runs`, `signals`, `warnings`, `pipeline_status`, `push_runs`, `push_subscribers` | what a scenario asserts on |

After every step the World checks ADR #137's invariants of the bound stages (below), and that no template expression failed, caught or not (D107): such a failure is always a template bug, and a `Catch` can hide it. `KNOWN_FAILING_EXPRESSIONS` (`invariants.py`) names the few that fail in templates 1.3 deletes, and `test_meta.py` keeps that list to templates ADR #137 removes. A scenario that breaks one fails, whatever it asserts itself; `test_rows_view.py` tests the check. ADR #130's invariants retired with its engine (D115).

### ADR #137's invariants (`row_invariants.py`)

The checks of 1.3's invariants are pure functions over a `RowsView`: the runs and task rows, the `run_task` executions and their starts, and the answers of the runs' tokens, in the ADR's terms. The view that reads them from the stack comes with the contracts of TR-2, which name the table's fields; the checks stay as they are. `test_row_invariants.py` plants a violation of each check and a legal near miss.

Each check runs from the stage that builds its mechanism (`STAGE_OF`): `rows-core` (Start_Run, the dispatcher, the stream, the sweeper; TR-4), `rows-run-task` (TR-5), `rows-ops` (TR-6) and `rows-migration` (TR-8). Invariant 8 is checked both ways, and `liveness` judges a scenario's end. Invariants 2, 5, 6 and 10 are judged from the boundary's log (below), as writes of task rows and passes of the dispatcher; invariant 7 lives in `tests/sdk/test_task_row_writers.py`. `test_meta.py` keeps every invariant from 1 to 12 in exactly one place.

A verb whose code path has not landed raises `NeedsBinding`. A scenario that uses one carries `@pytest.mark.needs_binding(stage)` and is skipped until its stage is listed in `stages.BOUND_STAGES`.

The scenarios of `test_scenarios.py` ran on ADR #130's engine. TR-4 generates pipelines with `Start_Run` (D115), so the stages of that engine retired with it, and each of those scenarios now waits for the stage that ports it to ADR #137's engine: `rows-run-task` (TR-5), `rows-ops` (TR-6) or `rows-migration` (TR-8). A scenario that tests machinery 1.3 removes goes with that machinery, with the reason.

### Red scenarios of tasks as rows (`test_scenarios_rows.py`)

Each class of bug ADR #137 makes impossible has one scenario or two, written before the code. The classes are a lost hint, a pass without a time budget, a late `Start_Run` attempt, a start that keeps failing, and Restart with dependants racing a pass. Then two passes at once, a Pause or a Stop racing a pass, a fail-open read and a lost answer. Last, a missing row or output, an output at the payload limit, a limit in a pause and a 1.2 row left in flight. R-S1 to R-S4 are the four stability blockers of the plan's review. Each is skipped until its stage is bound; from then on the stage's invariants run under it, and `test_meta.py` asks for a probe that brings its bug back.

## Streams (`streams.py`, `stream_filters.py`)

`StreamAdapter.for_mapping` delivers a table's stream as one of the template's event source mappings does: its `FilterCriteria`, its `BatchSize`, and shards by partition key. A batch the handler reports partly failed goes on from its first failed record, as Lambda checkpoints it; with `hold_on_error`, a handler that raises holds its batch and its shard, as a mapping that retries does. `drop`, `delay` and `release` lose or hold records, and `mark_expired` gives a TTL deletion TTL's `userIdentity`. `test_streams.py` pins each.

`stream_filters.matches` applies a filter pattern with EventBridge's syntax. `test_stream_filters.py` runs the four filters of ADR #137 §7 over the 25 writes and 4 deletions of TR-0's live probe and gets exactly the 11 records AWS delivered.

## The boto3 boundary (`boundary.py`)

Every component reaches DynamoDB through one of two shared clients, and `Boundary` hooks both with botocore's events, so no production code carries a hook. `World.boundary` logs each call with the component that made it: `lambda:reconcile`, `lambda:sweeper`, `console`, or `sfn:{machine}:{execution}` for a state machine's integration. A call is `sent` once it reached DynamoDB and `answered` once its caller got the answer, so a write whose answer is lost shows as sent and unanswered.

| Boundary verb | What it does |
|---|---|
| `between(after_read=, then=)` | run `then` after a matching read returns and before its caller goes on, to put an action between a pass's read and its write |
| `between(before_write=, then=)` | run `then` before a matching write is sent |
| `unprocess(table=, keys=, keep=)` | answer the next BatchGetItem on `table` with `keep` items and the rest as `UnprocessedKeys` |

`test_boundary.py` pins each.

## Defects and mutation probes

`defects.py` lists the defects by the ids in ADR #130's defects table (B-D1 is ADR #133's; the ids from B-RN1 on come from the external review of 1.2.0; B-Q1 is D107's, the harness's strictness about undefined; the ids from R-L1 on are the classes of bug of ADR #137), and each scenario that pins one carries `@pytest.mark.defect(id)`. `probes.py` holds the mutation probes: each re-introduces one defect in the new code, and `run_probes.py` applies it, runs its scenario, expects the scenario to fail and restores the file. A probe whose scenario waits for an unbound stage is reported `SKIPPED`, as its scenario is. `scenario_marks.py` reads the marks from the scenarios' source for both. The runner edits source files while it runs, so run no other suite at the same time: a suite that loads a template or module then sees the defect. CI runs it after the other suites (`make test-probes`).

`test_meta.py` keeps these consistent:
- every defect has a scenario, and every scenario names a known defect;
- a defect whose scenarios run has a probe on one of them;
- a `needs_binding` marker never names a bound stage;
- skipped scenarios call only verbs the World has;
- the expression check's exceptions name only templates ADR #137 removes;
- scenarios never write to a table directly.
