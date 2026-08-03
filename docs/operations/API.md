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
GET /api/pipelines?stats=true&date=2026-02-19
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
      "status": "failed",
      "paused": false,
      "sla": 85,
      "progress": 67,
      "today_stats": { "success": 2, "failed": 1, "running": 0, "waiting": 0, "skipped": 0, "total": 3 },
      "recent_runs": [
        { "date": "2026-02-19", "exec": "a1b2c3d4", "status": "running" },
        { "date": "2026-02-18", "exec": "e5f6g7h8", "status": "failed" },
        { "date": "2026-02-17", "exec": "i9j0k1l2", "status": "success" }
      ]
    }
  ]
}
```

Fields `schedule`, `recent_runs`, `sla`, `progress`, `today_stats` are only present when `stats=true`.

### Get Pipeline Status

```http
GET /api/pipeline-status?name={pipeline_name}
```

### Get Pipeline Executions

```http
GET /api/pipeline-executions?name={pipeline_name}
```

### Get Pipeline DAG

```http
GET /api/pipeline-dag?name={pipeline_name}&pipeline_execution={execution_name}
```

Returns DAG structure with lookup priority:
1. **Snapshot** — per-execution snapshot from `tokens_table` (if `pipeline_execution` provided)
2. **Registry** — current DAG from `pipeline_registry`
3. **Inferred** — reconstructed from task execution data

Response includes `dag_source` field: `'snapshot'` | `'registry'` | `'inferred'`

### Run Pipeline

```http
POST /api/pipeline-run?name={pipeline_name}
```

Body (optional): `{"variables": {"custom_var": "value"}}`

### Register Pipeline

```http
POST /api/pipeline-register
```

Body: `{"pipeline_name": "...", "sfn_arn": "...", "dag": {...}}`

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
don't build one; endpoints encode it differently (this feed uses a `started_at`, the
execution list a date) and the encoding is not part of the contract. `next: null` means
the feed is exhausted — it is the only honest end-of-feed signal, since a full page is
not one.

How far back a page can reach depends on the filters:
- `pipeline` (no date) — `pipeline-date-index`, no window; bounded by the row TTL.
- `date` — that one logical date.
- neither — the last 14 days (`Limits.SLA_DAYS`); `date` is the shard key for a
  cross-pipeline feed, so it is read one day at a time (ADR #108).

### Get Task Config

```http
GET /api/task-config?name={execution_name}
```

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
POST /api/task-skip?name={execution_name}       # Skip waiting/failed task
POST /api/task-fail?name={execution_name}       # Mark as failed
POST /api/task-success?name={execution_name}    # Mark as successful
POST /api/task-stop?name={execution_name}       # Force-stop running task
POST /api/task-restart?name={execution_name}    # Restart terminal task (409 if not terminal)
POST /api/task-retry?name={execution_name}      # Retry with same params
```

`task-fail` and `task-success` accept optional body: `{"reason": "..."}`

---

## Executions

### List All Runs

```http
GET /api/runs?pipeline={name}&status={status}&date={YYYY-MM-DD}&limit=50&before={cursor}
```

Returns pipeline executions, `started_at`-descending:

```jsonc
{
  "runs": [
    {
      "kind": "execution",
      "pipeline_name": "acme-daily",
      "pipeline_execution": "acme-daily-2026-05-31-ab12cd34",
      "pipeline_execution_short": "ab12cd34",
      "status": "success",           // running | success | failed | timed_out | aborted | recovered
      "started_at": "...", "finished_at": "...",
      "date": "2026-05-31",
      "duration_ms": 42000
    }
  ],
  "count": 1,
  "next": "2026-05-31T08:41:07.552Z",  // cursor for the older page; null = nothing older
  "filters": { "pipeline": "", "status": "", "date": "" }
}
```

Paging: pass the previous response's `next` back as `before` to get the rows older
than it; `next` is opaque (see `/api/tasks`). `next: null` means the feed is exhausted.

Filter semantics:
- `status` — literal match against execution status vocabulary.
- `pipeline` — matches `pipeline_name`.
- `date` — matches the execution's logical date.
- Reach: `pipeline` (no date) reads `pipeline-date-index` with no window (bounded by
  the row TTL); `date` is that one logical date; neither is the last 14 days
  (`Limits.SLA_DAYS`), fanned out one query per day (ADR #108).

Expand a row on demand via `GET /api/execution-children?id=` (execution);
children are not embedded.

### Execution Actions

```http
POST /api/execution-stop?id={execution_arn}
POST /api/execution-pause?id={pipeline_execution}
POST /api/execution-resume?id={pipeline_execution}
POST /api/execution-extend?id={pipeline_execution}   # +12h pause timeout
```

### Execution Info

```http
GET /api/execution-children?id={execution_name}
GET /api/execution-parent?id={execution_name}
```

---

## Notifications

```http
GET /api/notifications?limit=20&hours=4
```

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
GET /api/health          # Full check (DDB, SFN, circuit breaker)
GET /api/health/simple   # Liveness check
GET /api/metrics         # System metrics
```

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
