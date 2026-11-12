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
