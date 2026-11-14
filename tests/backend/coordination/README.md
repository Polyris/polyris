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
- **Not modelled:** real time, AWS's JSONata dialect, quotas. The live smoke covers them.

`test_sfn_runtime.py` pins each of these semantics on small synthetic machines. A scenario is only as good as the interpreter under it.

## The stack (`platform_stack.py`)

`Stack` loads `sam/template.yaml`:
- every DynamoDB table becomes a moto table with its declared keys and indexes;
- every state machine with a `DefinitionUri` is loaded with its `DefinitionSubstitutions` resolved the way CloudFormation resolves them. A template wired to the wrong table therefore writes to the wrong moto table, and a substitution the template does not define fails the load;
- `deploy_pipeline(dag)` generates a pipeline's ASL with the real generator and loads it.

`test_platform_stack.py` runs a one-task pipeline end to end: pipeline execution → `dependency_wrapper` → registration helper → `run_task` → the user's task → success.

## Scenarios (`test_scenarios.py`)

Scenarios speak only the `World` API in `world.py` and build pipelines only through `dags.py`, which uses the real SDK. A scenario therefore keeps its text while the templates and Lambdas under it change. Each one checks both sides of a wake: the task starts once its conditions hold, and never before.

| World verb | What it does |
|---|---|
| `deploy(dag, before_upgrade=)`, `start_run(pipeline, date, **inputs)` | generate the pipeline's ASL and start a run; `before_upgrade=True` starts its tasks on the v1.1.4 wrapper (`legacy/`), as runs in flight across the upgrade do |
| `fire_schedule(pipeline, scheduled_time)`, `start_bare(pipeline)` | start a run as EventBridge Scheduler does, with the input `polyris-deploy` gives the schedule; or with an empty input |
| `complete(run, task, status=)` | finish the user's work for a task |
| `write_event(asset, partition, writer=)`, `deliver_hints(asset=)` | record a fact, by `run_task` or as an older version stored it (`writer="older version"`); deliver its stream record separately, so tests choose the interleaving |
| `pause`, `resume`, `restart`, `skip`, `mark_success` | operator actions through the console routes |
| `time_out_registration`, `time_out_pause_wait`, `stop_wrapper_externally`, `inject_failure`, `fail_starts`, `fail_signals`, `fail_invokes` | faults from outside: a timeout, a stop, a refused DynamoDB call (or, with `after=True`, an applied call whose answer is lost), a Step Functions start or callback, a Lambda that keeps failing (transiently, or with `error="Sandbox.Timedout"` as a timeout) |
| `hold(machine)`, `release(machine)`, `release_newest(machine)` | new executions of a platform state machine wait to take a step, to order a race (B-P3, B-R2) |
| `sweep()`, `clock.advance(...)`, `clock.jump_to(when)` | the safety net and the passing of time; every reading of the clock, by the stack, the Lambdas or the console, moves it on 1 ms |
| `status`, `row`, `running_tasks`, `task_runs`, `signals`, `warnings`, `pipeline_status`, `push_runs`, `push_subscribers` | what a scenario asserts on |

After every step the World checks invariants 1, 3, 4 and 5 of ADR #130 (`invariants.py`): one accepted dependency signal per wrapper; no status moving back under one wrapper execution; a waiting row keeps its pointers until its signal is accepted; a waiting row is under `waiting#` and a row held in its pause wait under `paused#`. A scenario that breaks one fails, whatever it asserts itself. `test_invariants.py` tests the check.

A verb whose code path has not landed raises `NeedsBinding`. A scenario that uses one carries `@pytest.mark.needs_binding(stage)` and is skipped until its stage is listed in `stages.BOUND_STAGES`.

## Defects and mutation probes

`defects.py` lists the defects by the ids in ADR #130's defects table (B-D1 is ADR #133's), and each scenario that pins one carries `@pytest.mark.defect(id)`. `probes.py` holds the mutation probes: each re-introduces one defect in the new code, and `run_probes.py` applies it, runs its scenario, expects the scenario to fail and restores the file. The runner edits source files while it runs, so run no other suite at the same time: a suite that loads a template or module then sees the defect. CI runs it after the other suites (`make test-probes`).

`test_meta.py` keeps these consistent:
- every defect has a scenario, and every scenario names a known defect;
- a defect whose scenarios run has a probe on one of them;
- a `needs_binding` marker never names a bound stage;
- skipped scenarios call only verbs the World has;
- scenarios never write to a table directly.
