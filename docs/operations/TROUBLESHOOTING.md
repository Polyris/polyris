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
2. **IAM permissions:** the orchestration role, which starts the task's work, may start it:
   ```bash
   aws iam simulate-principal-policy --policy-source-arn "arn:aws:iam::123456789012:role/${NAMESPACE}-${STAGE}-polyris-orchestration-role" --action-names "states:StartExecution" --resource-arns "arn:aws:states:us-east-1:123456789012:stateMachine:my-etl-job"
   ```
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

**Symptoms:** `polyris-deploy` stops with this message and names the missing outputs (`OrchestrationRoleArn`, `PipelineSpecsTable`, `StartRunFunctionArn`).

**Cause:** the platform is older than your polyris. **Fix:** upgrade it with `polyris-up`.

Also check that `--stack`, or `stack_name` in `config.py`, names the platform stack (`<namespace>-<stage>`, the one `polyris-up` installed), not a pipeline stack:
```bash
aws cloudformation describe-stacks --stack-name "${NAMESPACE}-${STAGE}" --query "Stacks[0].Outputs[*].OutputKey" --output table
```

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

### Console doesn't update on its own

**Check:** the **Auto** toggle in the header is on. It refreshes every 30 seconds, and every 15 seconds while a task counts down its `wait_before`. If it is on, the browser's console shows the failing requests.

---

## Emergency procedures

### Stop a runaway run

In the console, press **Stop** on the run. Or:

```bash
curl -X POST "${API_URL}/api/execution-stop?pipeline_execution=my-pipeline-run-2026-01-15-ab12cd34" -H "Authorization: Bearer $TOKEN"
```

A run stopped with `aws stepfunctions stop-execution` instead has its tasks stopped within seconds, when the end of its execution reaches polyris through EventBridge. If that event is lost, they stop at polyris's next check of the run, up to 30 minutes later.

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
