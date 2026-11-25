# polyris REST API Reference

The console's REST API: every route this build serves, its parameters and its answer. Use it from scripts and CI; the console uses the same routes.

## Base URL and a first call

The base URL is the platform stack's `ConsoleApiUrl` output, which ends with the stage:

```
https://{api-gateway-id}.execute-api.{region}.amazonaws.com/{stage}
```

Every route below goes after it, for example `https://abc123.execute-api.us-east-1.amazonaws.com/dev/api/pipelines`.

```bash
API_URL=$(aws cloudformation describe-stacks --stack-name mycompany-dev --query "Stacks[0].Outputs[?OutputKey=='ConsoleApiUrl'].OutputValue" --output text)
curl -H "Authorization: Bearer $TOKEN" "$API_URL/api/pipelines"
```

The stack name is `<namespace>-<stage>`, the one `polyris-up` installed. Routes take their identifiers as query parameters, never as path segments: `/api/pipeline-status?name=my-pipeline`.

## Authentication

Every route except `/api/health*` and `/api/metrics` needs a Cognito token of a console user in the `Authorization: Bearer <token>` header; without one it answers 401. To get a token for a script, sign in with a console user's email and password (the user must have chosen its own password at its first sign-in):

```bash
TOKEN=$(aws cognito-idp admin-initiate-auth --user-pool-id <CognitoUserPoolId> --client-id <CognitoClientId> --auth-flow ADMIN_USER_PASSWORD_AUTH --auth-parameters 'USERNAME=you@example.com,PASSWORD=your-password' --query AuthenticationResult.AccessToken --output text)
```

`CognitoUserPoolId` and `CognitoClientId` are outputs of the platform stack. The call needs IAM permission `cognito-idp:AdminInitiateAuth`. A token lasts one hour. Users and sign-in: [authentication.md](../features/authentication.md). The `http` examples below omit the header.

---

## Pipelines

### List pipelines

```http
GET /api/pipelines?stats=true
```

```json
{
  "pipelines": [
    {
      "name": "acme-daily",
      "arn": "arn:aws:states:...",
      "description": "Daily pipeline",
      "group": "acme",
      "schedule": "cron(0 10 ? * MON *)",
      "asset_schedule": null,
      "registered_at": "2026-02-10T09:14:22.031Z",
      "needs_redeploy": false,
      "status": "running",
      "waiting_on": "waiting_decision",
      "failed_tasks": 1,
      "sla": 85,
      "recent_runs": [
        { "date": "2026-02-19", "started_at": "2026-02-19T06:00:01.204Z", "exec": "a1b2c3d4", "status": "running", "failed_tasks": 1 },
        { "date": "2026-02-18", "started_at": "2026-02-18T06:00:00.871Z", "exec": "e5f6g7h8", "status": "failed", "failed_tasks": 2 }
      ]
    }
  ],
  "stats_incomplete": false,
  "index_building": false
}
```

`recent_runs`, `sla`, `waiting_on` and `failed_tasks` are filled only with `stats=true`, from the runs started in the last 14 days.

- `recent_runs`: the last 10 of those runs whose date is within the 14 days, newest first.
- `status`: the latest run's status, `idle` when there is none. It is `running` until every task of the run has finished, then `success`, `failed` or `aborted`. A run stopped in the AWS console shows `running` until polyris next checks it, up to about 30 minutes.
- `waiting_on`: `waiting_decision` or `waiting_paused` when the latest run waits for an operator, else `null`.
- `needs_redeploy`: `true` when the pipeline was deployed by an older polyris; its runs fail until you run `polyris-deploy` for it again.
- `stats_incomplete`: `true` when some runs could not be read, so some statuses may be out of date.
- `index_building`: `true` while DynamoDB builds the run index after a platform upgrade (minutes); the statuses show once it is ready.

### Pipeline status

```http
GET /api/pipeline-status?name={pipeline_name}&date={YYYY-MM-DD}&pipeline_execution={pipeline_execution}
```

One run's tasks: the run named by `pipeline_execution`, or else the latest run of `date` (today by default). `selected_execution` names the run, `null` when the date has none. `run` holds `paused` (a pause holds the run) and `stopping` (a Stop of it is in progress). Each task names its latest work execution in `run_task_arn`.

A task that waits (`waiting`, `waiting_paused`, `waiting_decision`) also says why:
- `reasons`, and `reasons_at`, since when;
- `countdown_end`, when its `wait_before` countdown ends and it starts;
- `deadline`, when its wait or its decision fails; `null` while a pause holds it or it counts down;
- while it waits for its upstream tasks, `dependency_verdict`: what its trigger rule makes of them (`verdict`, `reason`, and per upstream task its `name`, `status` and what it `counts_as`).

### Pipeline runs

```http
GET /api/pipeline-executions?name={pipeline_name}&page_size=15&before={cursor}
GET /api/pipeline-executions?name={pipeline_name}&date={YYYY-MM-DD}
GET /api/pipeline-executions?name={pipeline_name}&start_date={YYYY-MM-DD}&end_date={YYYY-MM-DD}
```

A pipeline's runs, newest first, each as `/api/runs` shows it:
- without a date: its runs started in the last 90 days, a page at a time; pass `next` back as `before` (`null` once nothing older exists);
- with `start_date` and `end_date`: the runs whose date lies in that range, among the runs started in the last 90 days;
- with `date`: that date's runs.

### Pipeline DAG

```http
GET /api/pipeline-dag?name={pipeline_name}&pipeline_execution={pipeline_execution}&date={YYYY-MM-DD}
```

The pipeline's graph. `dag_source` says where it comes from:
1. `run`: the version the run named by `pipeline_execution` ran with;
2. `registry`: the version deployed now;
3. `inferred`: rebuilt from the run's tasks, or the latest run's of `date`, when neither version can be read.

### Run a pipeline

```http
POST /api/pipeline-run?name={pipeline_name}
```

Body (optional): `{"input": {"current_date": "2026-01-15", "skip_tasks": ["load_inventory"], "custom_var": "value"}}`. `current_date` is the run's date (today when absent); `skip_tasks` names tasks the run starts as skipped; the other keys become the run's variables.

```json
{"execution_arn": "arn:aws:states:us-east-1:123456789012:execution:mycompany-dev-polyris-acme-daily:acme-daily-run-2026-01-15-ab12cd34", "started_at": "2026-01-15T09:00:01.204000+00:00"}
```

## Tasks

A task is named by its `execution_name`, `{task}-{date}-{last 20 characters of the run's name}`, which `/api/pipeline-status`, `/api/tasks` and `/api/execution-children` return.

### List tasks

```http
GET /api/tasks?pipeline={name}&status={status}&date={YYYY-MM-DD}&limit=100&before={cursor}
```

Every parameter is optional. Tasks matching the filters, newest first, one page at a time:

```jsonc
{
  "tasks": [ /* … */ ],
  "count": 100,
  "next": "2026-07-14T09:12:03.114Z",   // cursor for the older page; null = nothing older
  "filters": { "status": null, "date": null, "pipeline": null }
}
```

Pass `next` back as `before` for the older page. `next` is opaque: never build one. `next: null` is the only end-of-list signal, since a full page is not one. A page holds whole runs, so it may return more than `limit` tasks.

- Without `date`: the tasks of the runs started in the last 90 days. With `status`, a page may come back short or empty with a `next`: each page reads at most about 20,000 tasks.
- With `date`: the tasks of runs of that date. A day (`YYYY-MM-DD`) also finds the runs of hourly, weekly and monthly pipelines whose hour, week or month falls on it.

### Task config, output and events

```http
GET /api/task-config?name={execution_name}
GET /api/task-output?name={execution_name}
GET /api/task-events?name={execution_name}
```

- `task-config`: the task's `timeout_seconds` (`null` when the work has no limit) and `max_retries`, from the version its run ran with; 503 with the cause when that version cannot be read.
- `task-output`: the task's stored return value in `output`, and its `input`. Large values stored in S3 are read back for you ([DATA_PASSING.md](../features/DATA_PASSING.md)).
- `task-events`: the task's timeline in `events`, oldest first, each with `event_type`, `event_time`, `status` and `reason`.

### Task actions

```http
POST /api/task-skip?name={execution_name}       # Skip a task that has no outcome yet
POST /api/task-fail?name={execution_name}       # Mark as failed
POST /api/task-success?name={execution_name}    # Mark as successful, with the data of its outlets
POST /api/task-stop?name={execution_name}       # Stop an unfinished task: it ends stopped
POST /api/task-restart?name={execution_name}    # Run a finished or undecided task again
```

Every action takes an optional body `{"reason": "Data fixed upstream"}`; `task-restart` also takes `{"with_dependants": true}`, which reruns the dependants that a failure or a stop decided. An action on a running task stops its work too.

```json
{"message": "Skip triggered for extract-2026-01-15--2026-01-15-ab12cd34", "execution_name": "extract-2026-01-15--2026-01-15-ab12cd34"}
```

The `message` of each action:

| Route | `message` |
|---|---|
| `task-skip` | `Skip triggered for {execution_name}` |
| `task-fail` | `Task {execution_name} marked as failed` |
| `task-success` | `Task {execution_name} marked as successful` |
| `task-stop` | `Task {execution_name} stopped. Use Restart to run it again.` |
| `task-restart` | `Task {execution_name} restarted`, plus ` with its dependants` |

An action answers 404 when no task has that name, and 409 with `error` and `current_status` when the task's status allows no such action. A Restart also answers 409 while a Stop of its run is in progress, and once its run started more than nine months ago.

---

## Runs

### List runs

```http
GET /api/runs?pipeline={name}&status={status}&date={YYYY-MM-DD}&limit=50&before={cursor}
```

Runs, newest first:

```jsonc
{
  "runs": [
    {
      "kind": "execution",
      "pipeline_name": "acme-daily",
      "pipeline_execution": "acme-daily-run-2026-05-31-ab12cd34",
      "pipeline_execution_short": "-2026-05-31-ab12cd34",
      "status": "running",           // running | success | failed | aborted
      "failed_tasks": 1,             // tasks of the run that failed
      "failed_step": null,           // a failed direct step: {"step": "notify", "error": "..."}
      "started_at": "...", "finished_at": null,
      "date": "2026-05-31",
      "duration_ms": null,
      "triggered_by": "schedule"
    }
  ],
  "count": 1,
  "next": "2026-05-31T08:41:07.552Z",  // cursor for the older page; null = nothing older
  "filters": { "pipeline": "", "status": "", "date": "" }
}
```

Paging works as for `/api/tasks`. A run is `running` until every task has finished, with `failed_tasks` counting the ones that failed; then `success`, `failed` or `aborted` (a task was stopped and none failed). A failed direct step makes a finished run `failed`. `finished_at` and `duration_ms` are set once the run has finished.

Filters: `status` matches the status, `pipeline` the pipeline's name, `date` the run's date. Without `date` the list holds the runs started in the last 90 days; with `date`, every run of that date.

### Run's tasks

```http
GET /api/execution-children?id={pipeline_execution}
```

```json
{"pipeline_execution": "acme-daily-run-2026-05-31-ab12cd34", "count": 1,
 "children": [{"execution_name": "extract-2026-05-31--2026-05-31-ab12cd34", "task_name": "extract", "status": "success",
               "date": "2026-05-31", "started_at": "...", "finished_at": "..."}]}
```

### Run actions

```http
POST /api/execution-stop?pipeline_execution={pipeline_execution}   # body: {"reason": "..."}, optional
POST /api/execution-pause?id={pipeline_execution}
POST /api/execution-resume?id={pipeline_execution}
```

Each answers at once, for example `{"message": "Stopping the run: its tasks stop, dependants first", "stopping": true, "pipeline_execution": "..."}`; Pause and Resume answer `"paused": true` or `false`. polyris stops the run's unfinished tasks, dependants first, within seconds. A pause lasts until the Resume. Repeating an action while the first is in progress changes nothing; a Stop of a run whose stop is done starts a new one. Pause answers 409 for a run that is not active or whose Stop is in progress.

---

## Notifications

```http
GET /api/notifications?limit=20&hours=4
```

What the console's bell shows: every task that failed within the last `hours` (default 24, at most 168), every task that waits for an operator's decision, and polyris's warnings. `limit` caps the list (default 10, at most 50). Decision requests come first, the nearest deadline first; then failures and warnings, newest first. A run has one entry for its tasks at most, a decision request over a failure.

```json
{
  "notifications": [
    {"id": "decision:load-2026-01-15--2026-01-15-ab12cd34:2026-01-15T14:02:11.000Z", "type": "decision_required",
     "pipeline_name": "acme-daily", "task_name": "load", "pipeline_execution": "acme-daily-run-2026-01-15-ab12cd34",
     "error": "", "date": "2026-01-15", "deadline": "2026-01-15T14:02:11.000Z", "finished_at": "", "time_ago": ""},
    {"id": "failure:extract-2026-01-14--2026-01-14-cd56ef78:2026-01-14T09:01:40.000Z", "type": "failure",
     "pipeline_name": "orders", "task_name": "extract", "pipeline_execution": "orders-run-2026-01-14-cd56ef78",
     "error": "Glue job failed: ...", "date": "2026-01-14", "finished_at": "2026-01-14T09:01:40.000Z", "time_ago": "5h ago"}
  ],
  "count": 2
}
```

- `id` names the event, not the run: the console keeps "read" and "cleared" by it. A pause or a resume moves a decision's `deadline`, and with it the `id`.
- `deadline` is when the task fails on its own, `null` while its run is paused.
- A warning (`"type": "warning"`) carries a `kind` and the `error` text.

---

## Settings

```http
GET /api/settings/decision-timeout
```

`{"decision_timeout_seconds": 18000}`: a failed task that waits for an operator's decision fails after 5 hours.

---

## Health

```http
GET /api/health          # Checks DynamoDB, Step Functions, recent failures, circuit breakers, task coordination
GET /api/health/live     # {"status": "ok"}, no downstream call
GET /api/metrics         # System metrics
```

`/api/health` answers 200 while its status is `healthy` or `degraded`, and 503 when DynamoDB or Step Functions cannot be reached. Task coordination that needs attention makes it `degraded`. To monitor it, read `checks.coordination.status`: `healthy` or `unhealthy`.

Without a valid token you get the status words only:

```json
{"status": "degraded", "timestamp": "2026-11-14T17:40:02.118123+00:00", "response_time_ms": 41.2, "detail": "minimal",
 "checks": {"dynamodb": {"status": "healthy"}, "stepfunctions": {"status": "healthy"},
            "recent_failures": {"status": "healthy"}, "circuit_breakers": {"status": "healthy"},
            "coordination": {"status": "unhealthy"}}}
```

With a valid token you get `"detail": "full"`, each check's details, and `findings`: the problems the console's **System** status shows.

```json
"findings": [{"title": "Asset event delivery is off",
              "resource": "The stream mapping of polyris-dev-polyris-asset-fanout is Disabled.",
              "changed_at": "2026-11-14T17:18:00.000Z",
              "impact": "Tasks that wait for an asset start up to 40 minutes late, and pipelines scheduled on an asset up to an hour late.",
              "fix": "Re-enable the mapping: in the Lambda console, open polyris-dev-polyris-asset-fanout, then Configuration → Triggers.",
              "command": "aws lambda update-event-source-mapping --uuid 48529b7b-abda-4306-8550-8d3bbc1391c3 --enabled --region us-east-1"}]
```

Every finding has a `title`; the other fields appear only when they apply. `changed_at` is when AWS last changed the resource.

---

## Errors

An error of authentication, routing or a missing parameter has a code:

```json
{"error": "ERROR_CODE", "message": "Human readable message", "request_id": "abc123"}
```

A route's own error carries the message in `error`, sometimes with more fields: `{"error": "Pipeline my-pipeline not found"}`.

| Status | Meaning |
|--------|---------|
| 200 | Success |
| 400 | Bad request or validation error |
| 401 | Missing or invalid token |
| 404 | Not found |
| 409 | The action does not apply to the current status, or the row kept changing (try again) |
| 500 | Internal error |
| 503 | The run index is being built after an upgrade (`INDEX_BUILDING`), a pipeline version cannot be read, or `/api/health` cannot reach DynamoDB or Step Functions |
