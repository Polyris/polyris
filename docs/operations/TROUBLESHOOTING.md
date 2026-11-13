# Troubleshooting Guide

Common issues and solutions for polyris operations.

> **API auth (`AUTH_ENABLED=true`):** every API call below except `/api/health*`
> and `/api/metrics` needs `-H "Authorization: Bearer $TOKEN"` where `$TOKEN`
> is a Cognito ID token. Grab one from the Console (browser devtools → Network
> → any authenticated request → copy `Authorization` header). A **401
> Unauthorized** means a missing/expired token — sign out and sign in again.
> See [authentication.md](../features/authentication.md).

---

## Pipeline Issues

> **Note:** Shell variables used in this guide — set once before running any commands:
> ```bash
> export API_URL=<your-console-api-url>  # e.g. https://abc123.execute-api.us-east-1.amazonaws.com
> export NAMESPACE=<your-namespace>  # e.g. mycompany
> export STAGE=<your-stage>  # e.g. dev
> ```
> Table names follow the pattern `${NAMESPACE}-${STAGE}-polyris-{table}`.


### Pipeline doesn't start on schedule

**Symptoms:** Pipeline should run daily but doesn't trigger.

**Check:**
1. EventBridge Scheduler schedule exists and is enabled:
   ```bash
   # Schedule name pattern: {namespace}-{stage}-polyris-{dag_id}-schedule
   aws scheduler list-schedules --name-prefix "${NAMESPACE}-${STAGE}-polyris-"
   aws scheduler get-schedule --name "${NAMESPACE}-${STAGE}-polyris-your-pipeline-schedule"
   ```

   The `State` field should be `ENABLED`. If it's `DISABLED`, the pipeline was
   paused — resume via Console (pipeline → **Resume**) or set
   `is_paused_upon_creation=False` on the DAG and redeploy. See
   [how-to/schedule-and-redeploy.md](../how-to/schedule-and-redeploy.md#pause-and-resume-a-schedule).

2. Pipeline is registered:
   ```bash
   aws dynamodb get-item \
     --table-name ${NAMESPACE}-${STAGE}-polyris-pipeline-registry \
     --key '{"pipeline_name": {"S": "your-pipeline"}}'
   ```

**Fix:** Re-deploy pipeline with `polyris-deploy`, or re-enable the schedule via
Console. `polyris-deploy` re-applies the DAG's declared schedule state on every
deploy, so a manual console pause reverts the next time you deploy — set
`is_paused_upon_creation=True` on the DAG if you want the pause to stick.

polyris uses `AWS::Scheduler::Schedule` (EventBridge Scheduler), not the legacy
`AWS::Events::Rule`. If your account was set up before v0.90-ish and you see
`aws events list-rules` returning polyris rules, they're leftovers — safe to
delete via console; polyris-deploy creates only Scheduler resources now.

---

### Pipeline stuck in "running" state

**Symptoms:** Pipeline shows running but no tasks are active.

**Check:**
1. Step Functions execution status:
   ```bash
   aws stepfunctions list-executions \
     --state-machine-arn "arn:aws:states:..." \
     --status-filter RUNNING
   ```

2. Check for orphaned executions in DynamoDB.

**Fix:** Stop the stuck execution via Console UI or API:
```bash
curl -X POST ${API_URL}/api/execution-stop?id={arn} \
  -H "Authorization: Bearer $TOKEN"
```

---

## Task Issues

### Task stuck in "waiting"

**Symptoms:** a task shows "waiting" although you expect its dependencies or assets to be in place.

**Check:**
1. **Why it waits.** Each check of the task logs one line with its `verdict` and `reasons`, such as `waiting for 1 dependencies` or the asset dates still missing:
   ```bash
   aws logs tail /aws/lambda/${NAMESPACE}-${STAGE}-polyris-reconcile --since 2h \
     --filter-pattern '"<execution_name>"'
   ```
   The execution name is in the task modal: `{task}-{date}-{run suffix}`.
2. **Dependencies tab** in the task modal: are the upstream tasks finished, in a state the trigger rule accepts?
3. **Assets:** a `wait_for` asset needs a fact that serves the run's date (see [ASSETS.md § Granularity](../features/ASSETS.md#granularity)). A fact for another date, today's for instance, does not count.
4. **Engine health:** a banner in the console, or `GET /api/health` → `checks.coordination.problems`, means waiting tasks are not being re-checked; see [The console shows "Task coordination needs attention"](#the-console-shows-task-coordination-needs-attention).
5. **Paused run:** a ready task of a paused run waits as `waiting_paused` until you resume the run.

**Fix:**
- A dependency or asset is missing: run its producer for that date, or mark the upstream task successful in the console; its facts are dated by its own run.
- Everything is in place: the sweeper re-checks a waiting task 10, 20, then every 30 minutes and starts it at the next check. A `sweeper` warning in the bell says the regular wake-up was lost on the way.
- The task should not wait at all: skip it or mark it successful from the task modal.

---

### Task stuck in "waiting_delay"

**Symptoms:** Task shows countdown but never proceeds.

**Check:**
1. `wait_delay_until_ms` timestamp — when should it end?
2. Server time sync — any clock skew?
3. Wrapper SFN execution — is Wait state active?

**Fix:** Usually resolves itself. If not, restart the task.

---

### Task fails immediately

**Symptoms:** Task goes to "failed" within seconds.

**Check:**
1. **Task ARN** is correct:
   ```bash
   aws stepfunctions describe-state-machine --state-machine-arn "your-task-arn"
   ```

2. **IAM permissions** — wrapper role can invoke task:
   ```bash
   aws iam simulate-principal-policy \
     --policy-source-arn "wrapper-role-arn" \
     --action-names "states:StartExecution" \
     --resource-arns "task-arn"
   ```

3. **Task logs** — check CloudWatch for the actual error.

**Fix:** Fix IAM permissions or task ARN, then restart.

---

### Task shows "upstream_failed"

**Symptoms:** Task blocked due to upstream failure.

**This is expected behavior** when `trigger_rule="all_success"` (default) and an upstream failed.

**Options:**
1. Fix and restart the failed upstream
2. Skip the failed upstream (task will re-evaluate)
3. Change `trigger_rule` to `all_done`

---

### Task shows "skipped" with no failure anywhere in the pipeline

**Symptoms:** A task using a conditional `trigger_rule` (e.g. `all_skipped`,
`none_skipped`) resolves `skipped`, and the run still shows `success`, even though
nothing upstream failed.

**This is expected behavior (ADR #115).** The task's trigger condition never
occurred — e.g. an `all_skipped` task with no skipped upstreams has nothing to react to.
Before this fix, this case incorrectly showed `upstream_failed` (red) and marked the
whole run `aborted`; it now correctly resolves as a no-op. No action needed — this is
not an error.

---

### An `all_success` task shows "skipped" even though its upstream ran fine

**Symptoms:** An `all_success` task (the default) resolves `skipped` instead of
running, and its upstream dependency is itself `skipped` — not failed.

**This is expected behavior (ADR #115).** A skipped upstream now cascades through
`all_success`, instead of silently counting as "OK to continue."
Check *why* the upstream was skipped:
1. If a human explicitly skipped it (task action) — a **manual** skip does **not**
   cascade, so if you're seeing this, the skip likely came from a trigger_rule
   resolving `skipped` further upstream in the same chain (a **rule**-triggered skip
   does cascade).
2. If you want this task to proceed regardless of an upstream skip, change its
   `trigger_rule` to `all_done`.

---

## Asset Issues

### Pipeline not in UI after deploy

**Symptoms:** Deployed with `polyris-deploy` but pipeline doesn't appear in sidebar.

**Check:**
1. `polyris-deploy` output ends with `✅ Pipeline deployed successfully!` and shows a registration step. If the deploy printed `❌ Could not record deploy time`, the pipeline was deployed but `registered_at` was not stamped — re-run `polyris-deploy`.

2. Pipeline exists in registry:
   ```bash
   aws dynamodb get-item \
     --table-name ${NAMESPACE}-${STAGE}-polyris-pipeline-registry \
     --key '{"pipeline_name": {"S": "my-pipeline"}}'
   ```

**Fix:**
- If the DDB row is missing: `polyris-deploy` again (retry) or `polyris-register --name my-pipeline`. See [REGISTRATION.md](../features/REGISTRATION.md#manual-registration-cli).
- Every pipeline run also self-heals registration as its first state, so if you can trigger a run manually the pipeline reappears in the sidebar automatically.

---

### Asset-triggered pipeline doesn't run

**Symptoms:** Asset updated but consumer pipeline didn't start.

**Check:**
1. **Asset subscription exists**:
   ```bash
   aws dynamodb query \
     --table-name ${NAMESPACE}-${STAGE}-polyris-asset-subscriptions \
     --key-condition-expression "asset_name = :asset" \
     --expression-attribute-values '{":asset": {"S": "your-asset"}}'
   ```

2. **Asset event was emitted**:
   ```bash
   aws dynamodb query \
     --table-name ${NAMESPACE}-${STAGE}-polyris-asset-events \
     --key-condition-expression "asset_name = :asset" \
     --expression-attribute-values '{":asset": {"S": "your-asset"}}' \
     --scan-index-forward false \
     --limit 5
   ```

3. **The fan-out handled the event** — asset-fanout logs every fact it reads and every pipeline it starts:
   ```bash
   aws logs tail /aws/lambda/${NAMESPACE}-${STAGE}-polyris-asset-fanout --since 1h \
     --filter-pattern '"your-asset"'
   ```

**Fix:**
- If subscription missing: re-deploy consumer pipeline
- If event missing: re-run the producer
- If the schedule names several assets: an AND schedule starts once every asset has a fact for the same date (weekly and monthly assets: for that week or month, or the one before). Materialise the missing asset; the run starts with its fact.

---

## Coordination Issues

### The console shows "Task coordination needs attention"

The banner lists what is wrong; `GET /api/health` returns the same list in `checks.coordination.problems`. Waiting tasks are not re-checked while it shows.

| Problem | What to do |
|---------|------------|
| The sweeper has not reported yet | Normal for the first 5 minutes after an install or upgrade. If it stays, check that the EventBridge schedule `${NAMESPACE}-${STAGE}-polyris-sweeper` exists and is enabled |
| The sweeper last reported N minutes ago | Read the sweeper's logs: `aws logs tail /aws/lambda/${NAMESPACE}-${STAGE}-polyris-sweeper --since 1h`. A run that fails on every attempt shows its error there |
| Asset events are not delivered: the stream mapping is … | Enable the event source mapping of `${NAMESPACE}-${STAGE}-polyris-asset-fanout` (Lambda console → the function → Configuration → Triggers) |
| Asset events are not recorded for delivery: the table stream is … | The stream of `${NAMESPACE}-${STAGE}-polyris-asset-events` was turned off outside polyris. Turn it on again (view type `NEW_IMAGE`) and point asset-fanout's trigger at the new stream. Meanwhile the sweeper still wakes waiting tasks at their checks, and its hourly catch-up starts asset-scheduled pipelines |
| Waiting tasks are N minutes behind their checks | The sweeper runs but cannot keep up, or fails on some rows: its logs name them |

---

## Deployment Issues

### "SAM stack '…' is missing required outputs" error

**Symptoms:** `polyris-deploy` fails with the SAM stack missing outputs
(`DependencyWrapperArn`, `OrchestrationRoleArn`, …). Historically this
error was "SSM parameter not found"; polyris-deploy no longer reads SSM,
it reads the SAM stack's CloudFormation Outputs directly.

**Check:**
1. Shared infrastructure is deployed and the outputs are present:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name "$STACK_NAME" \
     --region "$AWS_REGION" \
     --query "Stacks[0].Outputs[*].OutputKey" \
     --output table
   ```
   You should see `DependencyWrapperArn`, `OrchestrationRoleArn`,
   `PipelineRegistryTable`, `PipelineTokensTable`,
   `AssetSubscriptionsTable`, `ResultsBucket`.

2. The stack you're pointing at is the SAM stack, not a pipeline stack.
   `config.py`'s `stack_name` must match `sam/samconfig.toml`'s
   `stack_name`.

3. If outputs are missing, the stack was built from an older polyris/SAM
   version. Redeploy `sam/` from the current repo.

---

### "Stack already exists" from polyris-deploy

**Symptoms:** `polyris-deploy` fails with `AlreadyExistsException — stack {name} already exists`.

This means a pipeline stack of the same name was created by an earlier
`polyris-deploy` (or by hand via CloudFormation console) and CloudFormation
refuses to create it again.

**Check:**
```bash
# List polyris pipeline stacks
aws cloudformation list-stacks \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query 'StackSummaries[?contains(StackName, `polyris-`)].[StackName,StackStatus]' \
  --output table
```

**Fix — pick one:**

- **If the stack is yours and you want to update it.** `polyris-deploy` already
  performs an UpdateStack when the stack exists; if you're seeing this error it
  usually means a prior create failed halfway and the stack is in `ROLLBACK_COMPLETE`.
  Delete it, then re-deploy:
  ```bash
  aws cloudformation delete-stack --stack-name {namespace}-{stage}-polyris-{dag_id}
  aws cloudformation wait stack-delete-complete --stack-name {namespace}-{stage}-polyris-{dag_id}
  polyris-deploy
  ```

- **If the stack is someone else's** (namespace collision), change your
  `namespace` in `pipelines/config.py` and redeploy — every polyris pipeline
  stack is named `{namespace}-{stage}-polyris-{dag_id}`, so distinct namespaces
  don't collide.

---

## Console UI Issues

### UI shows "Loading..." forever

**Check:**
1. API URL is correct in `public/config.js` or `.env.local`
2. CORS is configured on API Gateway
3. Browser console for errors (F12 → Console)

**Fix:** Run `./ui/deploy.sh` to regenerate `config.js`. For local dev, check `ui/public/config.js` or `ui/.env.local`.

---

### UI not updating automatically

**Symptoms:** Need to manually refresh to see changes.

**Check:** Polling is enabled (default 30s, 3s when active tasks).

**Fix:** Check browser console for polling errors. May indicate API issues.

---

## Lambda Issues

### console_api 502 errors

**Symptoms:** API returns 502 Bad Gateway.

**Check:**
1. Lambda execution logs in CloudWatch
2. Lambda memory/timeout settings
3. API Gateway integration timeout (29s max)

**Fix:** Increase Lambda memory (faster cold starts) or optimize slow routes.

---

## Emergency Procedures

### Stop runaway execution

```bash
# Via Console UI: Stop button

# Via API:
curl -X POST ${API_URL}/api/execution-stop?id={arn} \
  -H "Authorization: Bearer $TOKEN"

# Via AWS CLI:
aws stepfunctions stop-execution --execution-arn "arn:aws:states:..."
```

---

### Manually complete stuck task

**Use when:** Task is stuck but you verified work is done (checked logs, S3, etc.)

```bash
# Via Console UI: Task modal → "Mark Success" button

# Via API:
curl -X POST ${API_URL}/api/task-success?name={execution_name} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Manually verified completion"}'
```

---

### Force restart pipeline for date

```bash
# Via Console UI: Pipeline → Run button → select date

# Via API:
curl -X POST ${API_URL}/api/pipeline-run?name={name} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"variables": {"current_date": "2026-01-15"}}'
```

---

### Clear all paused pipelines

```bash
# List paused
aws dynamodb scan \
  --table-name ${NAMESPACE}-${STAGE}-polyris-pipeline-registry \
  --filter-expression "paused = :true" \
  --expression-attribute-values '{":true": {"BOOL": true}}'

# Resume via API
curl -X POST ${API_URL}/api/execution-resume?id={id}
```

---

## Common Setup Errors

### `ModuleNotFoundError: No module named 'polyris'`

**Symptoms:** `polyris-deploy` or `polyris-validate` fails with
`ModuleNotFoundError: No module named 'polyris'`, or `python -c "from polyris import DAG"` fails.

**Fix:** Install the SDK into the Python environment your shell is using.
Pick a git tag from [github.com/Polyris/polyris/tags](https://github.com/Polyris/polyris/tags)
(e.g. `v1.0.1`) and substitute it for `<VERSION>` below.

```bash
# Option A — from a git tag (recommended for pipeline repos)
pip install "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"

# Option B — from a local checkout (for development)
cd /path/to/polyris && pip install -e .

# Verify
python -c "from polyris import DAG, task; print('✓ polyris installed')"
```

If you're using a virtualenv, activate it first (`source .venv/bin/activate`),
then run the install command. `which python && which polyris-deploy` should
point into the same `bin/` directory as the venv.

### `KeyError: 'dev'` or stage not found

Your `config.py` is missing the stage. Add it to `ENVIRONMENTS`:

```python
ENVIRONMENTS = {
    "dev": {
        "namespace": "myorg",
        "stage": "dev",
        "region": "us-east-1",
    },
}
```

