# ADR #106 — Parameter parity for `@task` decorators (the `task_config` ↔ `Run_Task_<X>` contract)

> **Status:** ACCEPTED — implemented. Hardens the wrapper-routed task model
> introduced earlier; does not change the SFN *flow* (retries are a separate
> change, ADR #107). No IAM changes (the SAM template already grants every
> dispatched service + `iam:PassRole`).

## Context

The wrapper-routed `@task` types — `lambda`, `glue`, `ecs`, `athena`, `emr`,
`batch` — all compile to the **same** per-task pipeline state: a
`startExecution.waitForTaskToken` call into the shared `run_task` wrapper state
machine, passing `task_type` plus a per-type **`task_config`** dict. The wrapper
(`sam/sfn_templates/helpers/run_task/sfn.tpl.json`) routes on `task_type` to a
`Run_Task_<X>` state that reads `task_config` and calls the real service
(`lambda:invoke`, `glue:startJobRun.sync`, `ecs:runTask.sync`,
`athena:startQueryExecution.sync`, `elasticmapreduce:addStep.sync`,
`batch:submitJob.sync`).

This makes `task_config` a **contract** with two sides that must agree exactly:
the SDK writer (`generators.py::_build_task_branch`) and the wrapper reader
(`Run_Task_<X>.Arguments`). `sfn` is the degenerate case — it carries **no**
`task_config` (`Run_Task_SFN` reads `task_arn` directly), which is why it never
drifted.

The other types had drifted. Audited gaps:

1. **emr (hard break).** SDK wrote `task_config = {cluster_id, step}`; the wrapper
   read flat `step_name`/`jar`/… → `HadoopJarStep.Jar` resolved empty → every EMR
   task failed at `addStep`.
2. **glue.** `worker_type`/`number_of_workers`/`allocated_capacity` were dropped
   by the wrapper — Glue job sizing silently ignored.
3. **ecs.** `assign_public_ip` was not even a decorator parameter (swallowed by
   `**kwargs`); `NetworkConfiguration` was emitted unconditionally, so EC2
   bridge/host tasks (no subnets) got an invalid `AwsvpcConfiguration`.
4. **lambda.** A user-supplied `payload` was ignored — the wrapper built its own.
   `invocation_type` accepted `'Event'`, which breaks the `waitForTaskToken`
   wait-model (the task token is never returned).
5. **role.** Cross-account `Credentials.RoleArn` existed **only** on
   `Run_Task_SFN`; for the six service types `role=` was silently a no-op.
6. **athena.** With no `output_location` the wrapper emitted
   `ResultConfiguration.OutputLocation: ''`, which `StartQueryExecution` rejects
   for workgroup-enforced output.

The root cause of class (1)–(2) is that the contract was **undocumented**: the
`add-aws-service` skill described only the direct `_gen_<service>_state` path and
treated the `task_config` branch as opaque metadata, so a new service could (and
did) write keys the wrapper never reads.

## Decision

Make every wrapper-routed type pass **all** of its parameters end-to-end, and
**document and test the contract** so it cannot silently drift again.

1. **Align all six types.** For each parameter, the five touch points move
   together: the `Task` dataclass field, the `_create_task` signature + forward,
   the `@task.<type>` signature + passthrough, the `_build_task_branch`
   `task_config` entry, and the `Run_Task_<X>` read. emr now passes the user's
   `emr_step` through verbatim as the `addStep` `Step`; glue passes its sizing;
   ecs gains `assign_public_ip` (a real parameter); lambda merges the user
   `payload` (orchestration context wins on collision — see below); `role`
   resolves to `Credentials.RoleArn` on **all six** dispatch states (the SDK
   already threads `cross_account_role` for every type).

2. **Validate at the decorator boundary.** Required/structural constraints are
   enforced where the user types them, not deep in codegen: emr (`emr_step` must
   be a dict with `HadoopJarStep.Jar`; reject plural `Steps`), glue (worker pair
   together; `allocated_capacity` mutually exclusive with the worker pair), ecs
   (Fargate requires subnets). athena/batch/lambda required fields are enforced by
   Python (required keyword args; `function_name | arn`).

3. **Conditional service blocks.** Where an empty value would make the AWS call
   invalid, the wrapper emits the block only when populated, via JSONata
   `$merge`: ecs `NetworkConfiguration` (only with subnets), athena
   `ResultConfiguration` (only with `output_location`).

4. **Remove the async footgun.** `invocation_type` is removed from the lambda
   decorator and `task_config`; `waitForTaskToken` requires the synchronous
   `RequestResponse` shape. (The unrelated internal `InvocationType='Event'`
   pager invoke in `console_api` is untouched.)

5. **Lambda payload precedence.** The user `payload` is the **lowest-priority**
   layer; orchestration context (`current_date`, `PARTITION_ARG`, `variables`,
   `upstream`) is merged on top. A stray `current_date` in a user payload cannot
   corrupt backfill, and XCom-in (`upstream`) is never clobbered.

6. **Pin the contract with tests.** `tests/sdk/test_run_task_template.py`
   evaluates each `Run_Task_<X>.Arguments` JSONata against the **exact**
   `task_config` the SDK emits (with `$states` bound — jsonata-python does not
   bind it from the evaluated root, so each test also asserts a known field as a
   binding guard). A schema mismatch now fails here, at build time, instead of at
   runtime in AWS.

## Consequences

- **No silent drift.** Adding a service now follows a documented procedure that
  includes the `task_config` ↔ `Run_Task_<X>` contract and a contract test; the
  emr-class bug cannot recur from a missing wrapper read.
- **Tests that encoded the bugs were corrected, not weakened** — several
  scenarios passed invalid input (emr step without `Jar`, glue
  `worker_type`+`allocated_capacity` together, ecs Fargate without subnets, a
  non-existent `retry_exponential_backoff`); each was fixed to valid usage.
- **Breaking, intentionally:** unknown/typo'd decorator kwargs now raise
  `TypeError` instead of being swallowed (`**kwargs` removed from the variant
  decorators); `invocation_type` is gone.

## Verification

`python -m pytest tests/sdk/ tests/backend/` (incl. the new contract tests),
100% core coverage (`--cov=polyris`), `cfn-lint sam/template.yaml`, and ASL
snapshot regeneration. Real-service behavior (each dispatch, cross-account role,
athena without an explicit output location) still requires a dev smoke run.
