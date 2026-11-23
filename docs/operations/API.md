# polyris REST API Reference

## Base URL

```
https://{api-gateway-id}.execute-api.{region}.amazonaws.com
```

Or via CloudFront:
```
https://{cloudfront-domain}/api
```

All endpoints use query parameters for resource identification (not path params):
```
GET /api/pipeline-status?name=my-pipeline    # Correct
GET /api/pipeline/my-pipeline/status          # Wrong
```

## Authentication

When auth enforcement is on (`AUTH_ENABLED=true`), every request except
`/api/health*` and `/api/metrics` requires a Cognito bearer token:

```
Authorization: Bearer <cognito-access-token>
```

For scripts/CI, authenticate with a **Cognito access token**
(`scripts/get-e2e-token.sh` obtains one), or run with `AUTH_ENABLED=false`.
See [authentication.md](../features/authentication.md) for the full how-to. The
`http` examples below omit the header for brevity — add it to every call.

---

## Pipelines

### List Pipelines

```http
GET /api/pipelines?stats=true
```

Response (with `stats=true`):
```json
{
  "pipelines": [
    {
      "name": "acme-daily",
      "arn": "arn:aws:states:...",
      "description": "Daily pipeline",
      "group": "acme",
      "schedule": "cron(0 10 ? * MON *)",
      "status": "running",
      "waiting_on": "waiting_decision",
      "failed_tasks": 1,
      "sla": 85,
      "recent_runs": [
        { "date": "2026-02-19", "started_at": "2026-02-19T06:00:01.204Z", "exec": "a1b2c3d4", "status": "running", "failed_tasks": 1 },
        { "date": "2026-02-18", "started_at": "2026-02-18T06:00:00.871Z", "exec": "e5f6g7h8", "status": "failed", "failed_tasks": 2 },
        { "date": "2026-02-17", "started_at": "2026-02-17T06:00:02.019Z", "exec": "i9j0k1l2", "status": "success", "failed_tasks": 0 }
      ]
    }
  ],
  "stats_incomplete": false,
  "index_building": false
}
```

Fields `recent_runs`, `sla`, `waiting_on` and `failed_tasks` are filled only when `stats=true`. They come from the run rows of the runs started in the last 14 days (`run-start-index`, ADR #137 §5); only runs started on 1.3 have one.

- `recent_runs` are the last 10 of those runs whose logical date is within the 14 days, newest first by date, then by start.
- `status` is the latest run's status, `idle` when there is none: `running` until every task of the run has finished, with `failed_tasks` counting the ones that failed, then `success`, `failed` or `aborted`. It comes from the run row's summary; the console asks Step Functions nothing about a run, so a run stopped in the AWS console shows `running` until the engine's next check of it, up to about 30 minutes.
- `waiting_on` is `waiting_decision` or `waiting_paused` when the latest run is running but waits for an operator, else `null`.
- `stats_incomplete` is `true` when a start day could not be read: some statuses may be out of date. `index_building` is `true` while DynamoDB builds `run-start-index` after an upgrade, about 8 minutes on a large table; the statuses show once it is ready. A call the console API is not allowed to make fails the request with a 500 instead, and so does a pipeline registry it cannot read.

### Get Pipeline Status

```http
GET /api/pipeline-status?name={pipeline_name}&date={YYYY-MM-DD}&pipeline_execution={execution_name}
```

Returns one run's tasks: the run named by `pipeline_execution`, or else the latest run of `date` (today by default),
found through the pipeline's task rows of that date (`pipeline-date-index`; ADR #137 §5). `selected_execution` names
the run, `null` when the date has none. Each task's status is the one its row holds: the console asks Step Functions
nothing about a run. `run` says what the run row holds and the task rows do not: `paused`, whether a pause holds the
run, and `stopping`, whether a Stop of it is in progress (ADR #137 §3, §5); `null` for a run started before 1.3.
Each task names its latest `run_task` execution in `run_task_arn` (here and in `/api/tasks`), and its output is read
from the date's store, never from the task's row.

A task that waits (`waiting`, `waiting_paused`, `waiting_decision`) also says why (ADR #137 §1): `reasons`, the
reasons the engine recorded, and `reasons_at`, since when; `countdown_end`, when a ready task's `wait_before`
countdown ends and it starts; `deadline`, when its wait or its decision fails, `null` while a pause holds it or the
task counts down; and, while it waits for its upstreams, `dependency_verdict`: what its trigger rule makes of them
(`verdict`, `reason`, and per upstream its `name`, `status` and what it `counts_as`).

A run started before 1.3 has no run row, and 1.3 finishes none of its rows: a task row of such a run that is not
final carries `"stale": true` here, in `/api/tasks` and in `/api/execution-children`, and such a run shows the status
`stale` in the run lists of a date (ADR #137, *Migration*).

### Get Pipeline Executions

```http
GET /api/pipeline-executions?name={pipeline_name}&page_size=15&before={cursor}
GET /api/pipeline-executions?name={pipeline_name}&date={YYYY-MM-DD}
GET /api/pipeline-executions?name={pipeline_name}&start_date={YYYY-MM-DD}&end_date={YYYY-MM-DD}
```

A pipeline's runs, newest first, each as `/api/runs` shows it (`execution_id`, `execution_short`, `date`, `status`,
`failed_tasks`, `failed_step`, `started_at`, `finished_at`, `duration_ms`, `triggered_by`):
- without a date, its history: the pipeline's run rows of the last 90 days by the day each run started
  (`run-start-index`), a page at a time; pass `next` back as `before` (opaque, `null` once nothing older exists);
- with `start_date` and `end_date`, the runs whose logical date lies in that range, from every start day of the
  90-day window, since a run can start before its date or long after it;
- with `date`, that date's runs from its task rows, runs started before the upgrade included.

Only runs started on 1.3 have run rows, so the history and a range show no run started before the upgrade. While
DynamoDB builds `run-start-index` after an upgrade, the history and a range answer 503 with `"code": "INDEX_BUILDING"`.

### Get Pipeline DAG

```http
GET /api/pipeline-dag?name={pipeline_name}&pipeline_execution={execution_name}
```

Returns DAG structure with lookup priority:
1. **Run** — for a `pipeline_execution` started on 1.3, the task specs of the version its run row names: the graph it
   ran with (ADR #137 §8). **Snapshot** — for a run started before 1.3, its DAG snapshot; 1.4 removes this reader
2. **Registry** — the task specs of the version the registry names: the current definition
3. **Inferred** — from the run's task rows, or the latest run's of `date`, when no specs can be read

Response includes `dag_source` field: `'run'` | `'snapshot'` | `'registry'` | `'inferred'`

### Run Pipeline

```http
POST /api/pipeline-run?name={pipeline_name}
```

Body (optional): `{"variables": {"custom_var": "value"}}`

## Tasks

### List All Tasks

```http
GET /api/tasks?pipeline={name}&status={status}&date={YYYY-MM-DD}&limit=100&before={cursor}
```

All query params optional. Returns tasks matching filters, `started_at`-descending,
one page at a time:

```jsonc
{
  "tasks": [ /* … */ ],
  "count": 100,
  "next": "2026-07-14T09:12:03.114Z",   // cursor for the older page; null = nothing older
  "filters": { "status": "", "date": "", "pipeline": "" }
}
```

Paging (same contract as `/api/runs` and `/api/pipeline-executions`): pass the previous
response's `next` back as `before` to get the rows older than it. **`next` is opaque** —
don't build one; its encoding is not part of the contract. `next: null` means the feed
is exhausted — it is the only honest end-of-feed signal, since a full page is not one.
A page never splits the rows that share a `started_at`, and every task of a run started
by `Start_Run` shares its run's: a page holds whole runs, so it may return more than
`limit` tasks.

How far back a page can reach depends on the filters:
- `pipeline` (no date) — `pipeline-date-index`, no window; bounded by the row TTL.
- `date` — that one logical date.
- neither — the last 14 days (`Limits.SLA_DAYS`); `date` is the shard key for a
  cross-pipeline feed, so it is read one day at a time (ADR #108).

### Get Task Config

```http
GET /api/task-config?name={execution_name}
```

Returns the task's `timeout_seconds` and `max_retries`. For a run of 1.3 they come from its run's spec version
(`execution_timeout`, `null` when the work has no limit, and the retries of its `task_config`), and the route answers
503 with the cause when that version cannot be read.

### Get Task Output

```http
GET /api/task-output?name={execution_name}
```

Returns the task's stored return value from the DynamoDB output store. Large
payloads are offloaded to S3 and transparently resolved (see
[DATA_PASSING.md](../features/DATA_PASSING.md)).

### Get Task Events

```http
GET /api/task-events?name={execution_name}
```

### Task Actions

```http
POST /api/task-skip?name={execution_name}       # Skip a task that has no outcome yet
POST /api/task-fail?name={execution_name}       # Mark as failed
POST /api/task-success?name={execution_name}    # Mark as successful, with the facts of its outlets
POST /api/task-stop?name={execution_name}       # Stop an unfinished task: it ends stopped
POST /api/task-restart?name={execution_name}    # Run a finished or undecided task again
```

Every action accepts an optional body `{"reason": "..."}`; `task-restart` also takes `{"with_dependants": true}`,
which reruns the dependants that a failure or a stop decided. An action on a running task stops its work too. Each
answers 409 when the task's status allows no such action, for a task of a run started before 1.3, and for a Restart
while a Stop of its run is in progress or once its run started more than nine months ago (a year less 90 days).
A Stop of a run whose stop is done starts a new stop, and `/api/execution-pause` refuses only while a stop is in
progress.

---

## Executions

### List All Runs

```http
GET /api/runs?pipeline={name}&status={status}&date={YYYY-MM-DD}&limit=50&before={cursor}
```

Returns pipeline runs, `started_at`-descending:

```jsonc
{
  "runs": [
    {
      "kind": "execution",
      "pipeline_name": "acme-daily",
      "pipeline_execution": "acme-daily-run-2026-05-31-ab12cd34",
      "pipeline_execution_short": "-2026-05-31-ab12cd34",
      "status": "running",           // running | success | failed | aborted | stale
      "failed_tasks": 1,             // tasks of the run that failed
      "failed_step": null,           // a failed direct step, which has no row: {"step": "notify", "error": "..."}
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

Paging: pass the previous response's `next` back as `before` to get the rows older
than it; `next` is opaque (see `/api/tasks`). `next: null` means the feed is exhausted.

A run's status comes from its run row's summary of its tasks (ADR #137 §4): `running`
until every task has finished, with `failed_tasks` counting the ones that failed, then
`success`, `failed` or `aborted` (a stop or an `upstream_failed` without a failure); a
failed direct step makes a finished run `failed`. `finished_at` and `duration_ms` are
set once the run has finished. The console asks Step Functions nothing about a run: the
engine keeps the run row true, and a run stopped in the AWS console shows `running` until
the engine's next check of it, up to about 30 minutes.

Filter semantics:
- `status` — literal match against that status.
- `pipeline` — matches `pipeline_name`.
- `date` — matches the run's logical date.
- Reach: without `date`, the run rows of the last 90 days by the day each run started
  (`run-start-index`, `Limits.RUNS_WINDOW_DAYS`), with `pipeline` or without; only runs
  started on 1.3 have one. `date` reads that one logical date's task rows, with no window,
  so it also finds the runs started before the upgrade, whose status comes from their tasks.
- While DynamoDB builds `run-start-index` after an upgrade (about 8 minutes on a large
  table), a read without `date` answers 503 with `"code": "INDEX_BUILDING"` and says so.

Expand a row on demand via `GET /api/execution-children?id=` (execution);
children are not embedded.

### Execution Actions

```http
POST /api/execution-stop?pipeline_execution={pipeline_execution}   # body: {"reason": "..."}, optional
POST /api/execution-pause?id={pipeline_execution}
POST /api/execution-resume?id={pipeline_execution}
```

Each answers at once: Stop writes the run's stop mark, and the engine stops its unfinished tasks, dependants first,
within seconds. A pause has no time limit: it lasts until the Resume. Repeating an action changes nothing; a run
started before 1.3 takes none of them (409).

### Execution Info

```http
GET /api/execution-children?id={execution_name}
```

---

## Notifications

```http
GET /api/notifications?limit=20&hours=4
```

Read from the attention index (ADR #137 §6), whatever the date of the run: every task
that failed within the last `hours`, by when it failed, and every task that waits for a
decision (`"type": "decision_required"`), with the `deadline` at which it fails on its
own, `null` while its run is paused. One notification per run, a decision request over a
failure. Its `id` names the event, the task's failure or its decision request, not the run:
the console keeps "read" and "cleared" by it, and a run may fail and ask again. A pause or a
resume moves a request's deadline, and with it the `id`. The decision requests come first, the most urgent first; then failures, warnings
and ended backfills, newest first. A warning (`"type": "warning"`) carries a `kind` from
`WarningKind` and is listed by the time it was written, whatever the date of the task it
is about. A run started before 1.3 has no entry of these kinds, so its failures are not
listed.

---

## Settings

```http
GET /api/settings/decision-timeout
```

How long a failed task waits on a human decision before the configured fallback
applies (ADR #114).

---

## Health

```http
GET /api/health          # Checks DynamoDB, Step Functions, failures, circuit breakers, coordination
GET /api/health/live     # Liveness: {"status": "ok"}, no downstream call
GET /api/metrics         # System metrics
```

`/api/health` answers 200 while its status is `healthy` or `degraded`, and 503 when DynamoDB or Step Functions cannot be reached. Task coordination that needs attention, judged from the sweeper's last self-check, makes it `degraded`. To monitor coordination, read `checks.coordination.status`: `healthy` or `unhealthy`.

How much it shows depends on the caller. Without a valid token while auth is on, you get the status words only:

```json
{"status": "degraded", "timestamp": "2026-11-14T17:40:02.118123+00:00", "response_time_ms": 41.2, "detail": "minimal",
 "checks": {"dynamodb": {"status": "healthy"}, "stepfunctions": {"status": "healthy"},
            "recent_failures": {"status": "healthy"}, "circuit_breakers": {"status": "healthy"},
            "coordination": {"status": "unhealthy"}}}
```

With a valid token, or with `AUTH_ENABLED=false`, you get `"detail": "full"`, each check's details, and `findings`. These are the problems the console's System status shows:

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

## Error Responses

```json
{"error": "ERROR_CODE", "message": "Human readable message", "request_id": "abc123"}
```

| Code | Meaning |
|------|---------|
| 200 | Success |
| 202 | Accepted (async) |
| 400 | Bad request / validation error |
| 404 | Resource not found |
| 409 | Conflict (race condition, e.g. restart non-terminal task) |
| 500 | Internal error |
