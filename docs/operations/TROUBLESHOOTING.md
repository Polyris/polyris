# Troubleshooting Guide

Symptoms you may meet when you deploy and run pipelines, what to check and how to fix each. Start from the console: the task modal, the bell and the **System** status name most causes.

> **Note:** the checks below use the AWS CLI with credentials for the platform's account. Set these once:
> ```bash
> export NAMESPACE=mycompany   # the namespace you gave polyris-up
> export STAGE=dev             # the stage you gave polyris-up
> export API_URL=$(aws cloudformation describe-stacks --stack-name "${NAMESPACE}-${STAGE}" --query "Stacks[0].Outputs[?OutputKey=='ConsoleApiUrl'].OutputValue" --output text)   # ends with /dev
> ```
> API calls need `-H "Authorization: Bearer $TOKEN"`; how to get a token: [API.md → Authentication](API.md#authentication). A 401 means a missing or expired token.

---

## Pipeline issues

### Runs fail at once: "Redeploy needed"

**Symptoms:** the pipeline shows **redeploy** in the sidebar and **Redeploy needed** on its page, and each of its runs fails at once without starting a task. The run's Step Functions execution names the cause, for example `this platform runs pipelines of format …, and this pipeline was deployed with format …: redeploy it with polyris-deploy`.

**Cause:** the pipeline was deployed by an older polyris than the platform runs, typically after a `polyris-up` upgrade.

**Fix:** deploy the pipeline again with the polyris that matches the platform:
```bash
cd pipelines/my-pipeline && polyris-deploy --stage dev
```
Then start a run from the console. After an upgrade, redeploy every pipeline.

---

### Pipeline not in the console after deploy

**Symptoms:** `polyris-deploy` finished, but the pipeline does not appear in the sidebar.

**Check:**
1. The output ends with `✅ my-pipeline deployed successfully!`. If it printed `❌ Could not record deploy time`, the pipeline was deployed but not recorded: run `polyris-deploy` again.
2. The pipeline is in the registry:
   ```bash
   aws dynamodb get-item --table-name "${NAMESPACE}-${STAGE}-polyris-pipeline-registry" --key '{"pipeline_name": {"S": "my-pipeline"}}'
   ```

**Fix:** run `polyris-deploy` again, or `polyris-register --name my-pipeline` ([REGISTRATION.md](../features/REGISTRATION.md#manual-registration-cli)). Each run also registers its pipeline as its first step, so a run started in the AWS console brings it back too.

---

### Pipeline doesn't start on schedule

**Symptoms:** the pipeline has a schedule but no run starts.

**Check:** the schedule exists and is enabled:
```bash
aws scheduler get-schedule --name "${NAMESPACE}-${STAGE}-polyris-my-pipeline-schedule"
```
`State` should be `ENABLED`. `DISABLED` means it was paused, by the AWS console or by `is_paused_upon_creation=True` on the DAG.

**Fix:** enable it in the AWS console, or remove `is_paused_upon_creation=True` and redeploy. Every `polyris-deploy` sets the schedule's state from the DAG again, so a pause made in the AWS console ends at the next deploy. See [schedule-and-redeploy.md](../how-to/schedule-and-redeploy.md#pause-and-resume-a-schedule).

---

### Run stuck in "running"

**Symptoms:** a run shows running, but none of its tasks is active.

**Check:**
1. **The bell:** a `coordination` warning about the run names what keeps it from ending. polyris tries again every few minutes.
2. **Paused or waiting tasks:** a task in `waiting_paused` or `waiting_decision` keeps the run open until you resume the run or decide the task.

**Fix:** if the cause stays, stop the run from the console, or:
```bash
curl -X POST "${API_URL}/api/execution-stop?pipeline_execution=my-pipeline-run-2026-01-15-ab12cd34" -H "Authorization: Bearer $TOKEN"
```

---

## Task issues

### Task stuck in "waiting"

**Symptoms:** a task shows "waiting" although you expect its upstream tasks or assets to be in place.

**Check:**
1. **Why it waits**, in the task modal's Details tab: the reasons polyris recorded (the upstream tasks or the asset data it waits for), what its trigger rule makes of its upstream tasks, and its deadline.
2. **Assets:** a `wait_for` asset needs new data for the run's date ([ASSETS.md → Granularity](../features/ASSETS.md#granularity)). Data for another date, today's for instance, does not count.
3. **Paused run:** a ready task of a paused run waits as `waiting_paused` until you resume the run.
4. **System status:** an amber **System** pill in the header means waiting tasks may not be re-checked; see [The System pill says "needs attention"](#the-system-pill-says-needs-attention).

**Fix:**
- An upstream task or asset is missing: run its producer for that date, or mark the upstream task successful in the console.
- Everything is in place: polyris re-checks a waiting task after 10 and 20 minutes, then every 30 minutes, and starts it at the next check. A `sweeper` warning in the bell means the instant wake-up was lost.
- The task should not wait at all: skip it or mark it successful from the task modal.

---

### Task fails immediately

**Symptoms:** a task goes to "failed" within seconds. The task modal's Details tab shows the error.

**Check:**
1. **The task's ARN** exists:
   ```bash
   aws stepfunctions describe-state-machine --state-machine-arn "arn:aws:states:us-east-1:123456789012:stateMachine:my-etl-job"
   ```
2. **IAM permissions:** the role the task's work starts as may start it. That is the platform's default task role, or the role the task names in `role=`; the orchestration role only assumes it. For the default task role:
   ```bash
   aws iam simulate-principal-policy --policy-source-arn "arn:aws:iam::123456789012:role/${NAMESPACE}-${STAGE}-polyris-default-task-role" --action-names "states:StartExecution" --resource-arns "arn:aws:states:us-east-1:123456789012:stateMachine:my-etl-job"
   ```
   For a task with `role=`, pass that role's ARN as `--policy-source-arn`, with credentials for the account it is in. An error about assuming the role means its trust policy does not name the orchestration role ([give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md)).
3. **The work's own logs** in CloudWatch, for an error inside the job.

**Fix:** correct the ARN or the permissions, then restart the task.

---

### Task shows "upstream_failed"

**Symptoms:** a task did not run because an upstream task failed.

This is expected when `trigger_rule="all_success"` (the default). Choose one:
1. Fix the cause, then restart the failed upstream task and tick *Also rerun the dependants its failure or stop decided*: the task waits for the upstream task's new outcome.
2. Skip the failed upstream task, then restart this task: it decides again from its upstream tasks.
3. Change `trigger_rule` to `all_done` and redeploy, for later runs.

---

### Task shows "skipped" with no failure anywhere

**Symptoms:** a task with a conditional `trigger_rule` (such as `all_skipped` or `none_skipped`) ends `skipped`, and the run still shows `success`.

This is expected: the task's condition never occurred. An `all_skipped` task with no skipped upstream task has nothing to react to.

---

### An `all_success` task shows "skipped" because its upstream was skipped

**Symptoms:** an `all_success` task (the default) ends `skipped` instead of running, and its upstream task is `skipped`, not failed.

A skip by a trigger rule passes on through `all_success`; a skip by an operator, or by the run's `skip_tasks`, does not. So the skip came from a trigger rule further upstream in the same chain. To run the task whatever its upstream task did, set its `trigger_rule` to `all_done`.

---

## Asset issues

### Asset-triggered pipeline doesn't run

**Symptoms:** an asset got new data, but the pipeline scheduled on it did not start.

**Check:**
1. **The pipeline is subscribed** to the asset:
   ```bash
   aws dynamodb query --table-name "${NAMESPACE}-${STAGE}-polyris-asset-subscriptions" --key-condition-expression "asset_name = :asset" --expression-attribute-values '{":asset": {"S": "raw/orders"}}'
   ```
2. **The asset's new data was recorded:**
   ```bash
   aws dynamodb query --table-name "${NAMESPACE}-${STAGE}-polyris-asset-events" --key-condition-expression "asset_name = :asset" --expression-attribute-values '{":asset": {"S": "raw/orders"}}' --no-scan-index-forward --limit 5
   ```
3. **The bell** has a `push` warning when polyris could not start the pipeline; it tries again.

**Fix:**
- No subscription: redeploy the consumer pipeline.
- No recorded data: run the producer again. A producer whose success could not be recorded waits for a decision (`waiting_decision`) with the cause; fix it, then restart the task.
- The schedule names several assets: the pipeline starts once every asset has data for the same date (for a weekly or monthly asset, that week or month, or the one before). Produce the missing asset; the run starts once its data is recorded.

---

## The System pill says "needs attention"

The **System** pill in the console's header turns amber when task coordination or the console's own checks need attention, and a toast names each new problem once. Click the pill: the **System status** panel lists each problem with the resource it concerns, what still works, the fix and a command you can copy. After a fix, the next check clears it within 5 minutes. `GET /api/health` with a token returns the same list in `findings` ([API.md → Health](API.md#health)).

Each problem in the panel says what still works and what to do. Until it clears, tasks may start up to 30–40 minutes late. "No check has run yet" is normal for the first 5 minutes after an install or upgrade.

A grey **System · status unknown** means the console could not read `/api/health`. An amber **System · no connection** means the console cannot reach its API: check your connection, and see [Console shows "Loading..." forever](#console-shows-loading-forever). An amber **System · API error** means the API answers with an error, such as an expired sign-in or a 500: the panel says which. The console retries on its own; **Retry now** retries at once.

---

## Deployment issues

### "Stack … is in ROLLBACK_COMPLETE" from polyris-deploy

**Symptoms:** `polyris-deploy` stops with `Stack mycompany-dev-polyris-my-pipeline is in ROLLBACK_COMPLETE (its first create failed) and can't be updated. Delete it, then deploy again.`

The pipeline stack's first create failed, and CloudFormation cannot update such a stack. Find the cause in the stack's events in the CloudFormation console, remove the stack with `polyris-destroy`, fix the cause, and run `polyris-deploy` again.

---

### "Platform stack '…' is missing required outputs"

**Symptoms:** `polyris-deploy` stops with this message and names the missing outputs:
```text
❌ Platform stack 'acme-dev' is missing required outputs.
   Missing outputs: PipelineScheduleRoleArn
   The platform is older than this polyris (1.4.0). Ask whoever installs the platform to run polyris-up 1.4.0.
```

**Cause:** the platform is older than your polyris. `PipelineScheduleRoleArn` alone means a platform of 1.3 and a scheduled pipeline: a pipeline without a schedule still deploys to it. **Fix:** the infrastructure engineer upgrades the platform ([manage-the-platform.md](../how-to/manage-the-platform.md#upgrade-to-140)).

Also check that `--stack`, or `stack_name` in `config.py`, names the platform stack (`<namespace>-<stage>`, the one `polyris-up` installed), not a pipeline stack:
```bash
aws cloudformation describe-stacks --stack-name "${NAMESPACE}-${STAGE}" --query "Stacks[0].Outputs[*].OutputKey" --output table
```

---

### "The role that runs polyris-deploy may not pass the platform's roles"

**Symptoms:** `polyris-deploy` stops after CloudFormation refused an `iam:PassRole`:
```text
❌ CloudFormation deploy failed: …
   The role that runs polyris-deploy may not pass the platform's roles. Attach the platform's deploy policy to it: arn:aws:iam::123456789012:policy/acme-dev-polyris-deploy-policy.
```

**Fix:** the infrastructure engineer attaches that policy to your role or CI role ([IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#the-deploy-policy)). To check a role before you deploy, simulate the role that runs `polyris-deploy`, not the platform's:
```bash
aws iam simulate-principal-policy --policy-source-arn "arn:aws:iam::123456789012:role/pipeline-deployer" --action-names "iam:PassRole" --resource-arns "arn:aws:iam::123456789012:role/${NAMESPACE}-${STAGE}-polyris-orchestration-role" --context-entries "ContextKeyName=iam:PassedToService,ContextKeyValues=states.amazonaws.com,ContextKeyType=string"
```

---

### `not authorized to perform: iam:CreateRole` on a scheduler role

**Symptoms:** the deploy of a scheduled pipeline fails, and the stack's event for `PipelineSchedulerRole` says the caller is not authorized to perform `iam:CreateRole`.

**Cause:** a `polyris-deploy` older than 1.4 runs under the deploy policy of 1.4, which creates no IAM role. **Fix:** install polyris 1.4 for the deploy, and pin it in CI.

---

### "role … is in the platform's account"

**Symptoms:** `polyris-validate` or `polyris-deploy` refuses a task:
```text
❌ Task 'export_daily': role arn:aws:iam::123456789012:role/finance-writer is in the platform's account, and polyris may use only roles of this account under the path /polyris-tasks/. …
```

**Fix:** ask the infrastructure engineer to create the role under `/polyris-tasks/`, and use its new ARN: [give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md). A role in another account needs no path.

---

### "its state machine would be named like the platform's own"

**Symptoms:** `polyris-validate` refuses a DAG whose `dag_id` ends like one of the platform's state machines, such as `bulk-backfill` or `test-quick`:
```text
❌ DAG 'bulk-backfill': its state machine would be named like the platform's own 'bulk-backfill' (<namespace>-<stage>-polyris-bulk-backfill), which no pipeline may start. Pick another dag_id.
```

**Fix:** rename the DAG. The platform's roles and policies refuse to start a state machine of that name, so its runs could never start.

---

### "The template of change set … is not the one uploaded"

**Symptoms:** `polyris-deploy` or `polyris-up` stops with `The template of change set … is not the one uploaded for …: the object was changed between its upload and the change set. Nothing was deployed; the change set is deleted.`

**Cause:** something wrote the template's S3 object between its upload and the change set. **Fix:** check who may write the bucket named in the message (the platform's results bucket for `polyris-deploy`, the artifacts bucket for `polyris-up`), then run the command again.

---

### `Unknown stage 'dev'`

`polyris-deploy` stops with `Unknown stage 'dev'. Configured stages: [...]` when your `config.py` has no such stage. Add it to `ENVIRONMENTS`; the key is the stage name:

```python
ENVIRONMENTS = {
    "dev": {
        "stack_name": "myorg-dev",
        "namespace": "myorg",
        "region": "us-east-1",
    },
}
```

See [CONFIGURATION.md](../reference/CONFIGURATION.md).

---

### `polyris-deploy: command not found` or `No module named 'polyris'`

**Fix:** the `polyris-*` commands come from install.sh, which installs polyris in an environment of its own ([README → Install](../../README.md#install)). Run it again, and add `~/.local/bin` to `PATH` if it says so.

To import polyris in your own Python (tests, CI):
```bash
pip install "polyris @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"
```
Use the tag of your platform's version.

---

## Console issues

### Console shows "Loading..." forever

**Check:**
1. Open `<console URL>/config.js`: its `API_URL` should be the `ConsoleApiUrl` stack output followed by `/api`.
2. The browser's console (F12 → Console) for errors.

**Fix:** run `polyris-up` again; it rewrites `config.js` from the stack's outputs.

---

### Run answers "Pipeline '…' is not registered on this platform"

**Symptoms:** **Run** on a pipeline's page fails with `Pipeline 'my-pipeline' is not registered on this platform. A pipeline registers within a minute of polyris-deploy; deploy it, then run it.`

**Fix:** deploy the pipeline, or wait a minute after its deploy; [Pipeline not in the console after deploy](#pipeline-not-in-the-console-after-deploy) has the checks.

---

### Console doesn't update on its own

**Check:** the **Auto** toggle in the header is on. It refreshes every 30 seconds, and every 15 seconds while a task counts down its `wait_before`. If it is on, the browser's console shows the failing requests.

---

## Emergency procedures

### Stop a runaway run

In the console, press **Stop** on the run. Or:

```bash
curl -X POST "${API_URL}/api/execution-stop?pipeline_execution=my-pipeline-run-2026-01-15-ab12cd34" -H "Authorization: Bearer $TOKEN"
```

A run stopped with `aws stepfunctions stop-execution` instead has its tasks stopped within seconds, when the end of its execution reaches polyris through EventBridge. If that event is lost, they stop at polyris's next check of the run, up to about 35 minutes later. A run that is still starting is stopped only after its start's retry window.

### Mark a stuck task successful

Use when the work is done (you checked its logs or its output) but the task did not record it. In the task modal, press **Mark Successful**. Or:

```bash
curl -X POST "${API_URL}/api/task-success?name=load-2026-01-15--2026-01-15-ab12cd34" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"reason": "Manually verified completion"}'
```

### Run a pipeline for a past date

In the console, press **Run** and set `current_date` in the parameters. Or:

```bash
curl -X POST "${API_URL}/api/pipeline-run?name=my-pipeline" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"input": {"current_date": "2026-01-15"}}'
```
