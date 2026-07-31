# polyris REST API Reference

> **Editions.** This reference documents the full Console API across tiers. The
> open-source (CE) build ships the free surface only; endpoints marked
> **🔒 Team** live in the proprietary `ee/` package (ADR #98) and return **404**
> on an OSS deployment. The Team-only endpoints are: token management
> (`/api/tokens`), pipeline `pause`/`restart`/`logs`/`metrics`, everything under
> **Assets** (`/api/assets*`, `/api/asset-*`), and **Slack Actions**
> (`/api/action/*`). Everything else — pipelines list/status/DAG/run/register,
> tasks, executions, runs, notifications, health/metrics — is free.

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
`/api/health*` and `/api/metrics` requires a bearer credential — either a
Cognito access token (browser) or a polyris Personal Access Token (scripts/CI):

```
Authorization: Bearer plrs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Minting and managing PATs (the Console avatar → API Tokens panel and the
`/api/tokens` endpoints) is **🔒 Team**. On an **OSS** deployment there is no way
to mint a PAT, so authenticate scripts/CI with a **Cognito access token**
instead (`scripts/get-e2e-token.sh` obtains one), or run with
`AUTH_ENABLED=false`. See [api-tokens.md](../features/api-tokens.md) (Team) and
[authentication.md](../features/authentication.md) for the full how-to. The
`http` examples below omit the header for brevity — add it to every call.

Tokens are **scoped** (`read` ⊂ `write` ⊂ `admin`, ADR #66): a request whose
token scope is below what the route needs returns **403** (vs **401** for a
missing/invalid token). `GET` needs `read`, mutations need `write`, and
deletes / token management need `admin`.

---

## API Tokens (PAT) — 🔒 Team

Personal Access Tokens for scripts/CI (ADR #65). **Team tier** — not in the OSS
build; OSS scripts/CI use a Cognito access token (see Authentication above).
Full how-to:
[api-tokens.md](../features/api-tokens.md).

### Create token

```http
POST /api/tokens
{ "name": "ci-pipeline", "scope": "write", "expires_in_days": 90 }
// scope (read|write|admin) optional, default "read"; expires_in_days optional
```

Response (`201`) — the `token` field is returned **only here**, once:
```json
{ "token_id": "tok_ab12cd34", "name": "ci-pipeline", "scope": "write",
  "token": "plrs_…", "created_at": "…", "expires_at": "…", "revoked": false }
```

### List tokens

```http
GET /api/tokens
```
Returns `{ "tokens": [ { "token_id", "name", "created_at", "last_used_at",
"expires_at", "revoked" } ] }` — the secret hash is never returned.

### Revoke token

```http
DELETE /api/tokens?id=tok_ab12cd34
```
`200` on success, `404` if the id is unknown.

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

### Get Pipeline Metrics — 🔒 Team

```http
GET /api/pipeline-metrics?name={pipeline_name}
```

### Get Pipeline Logs — 🔒 Team

```http
GET /api/pipeline-logs?name={pipeline_name}
```

### Run Pipeline

```http
POST /api/pipeline-run?name={pipeline_name}
```

Body (optional): `{"variables": {"custom_var": "value"}}`

### Restart Pipeline — 🔒 Team

```http
POST /api/pipeline-restart?name={pipeline_name}
```

### Pause / Unpause Pipeline — 🔒 Team

```http
POST /api/pipeline-pause?name={pipeline_name}
```

Toggles pause state. Running tasks complete; new tasks wait (12h timeout).

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

### Get/Update Task Config

```http
GET /api/task-config?name={execution_name}
PUT /api/task-config?name={execution_name}    # 🔒 Team — config mutation (ADR #110)
```

Reading a task's config is free; mutating it is Team (`task.config` capability)
and 404s on an OSS deployment.

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

Unified Run/Activity feed (ADR #95). Returns a mixed, `started_at`-descending
list of pipeline executions **and** Backfills, discriminated by `kind`:

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
      "duration_ms": 42000,
      "backfill_id": null              // set if this execution belongs to a backfill
    },
    {
      "kind": "backfill",
      "id": "bf-1a2b3c4d",
      "backfill_id": "bf-1a2b3c4d",
      "pipeline_name": "acme-daily",   // = target_pipeline
      "status": "partial",             // pending | running | completed | failed | partial | canceled (ADR #56)
      "started_at": "...", "finished_at": "...",
      "started_by": "alice",
      "total_partitions": 10, "completed_partitions": 6,
      "failed_partitions": 4, "skipped_partitions": 0,
      "downstream": "auto", "granularity": "daily",
      "date": null,                    // backfills span a range, not one logical date
      "duration_ms": 123000
    }
  ],
  "count": 2,
  "next": "2026-05-31T08:41:07.552Z",  // cursor for the older page; null = nothing older
  "filters": { "pipeline": "", "status": "", "date": "" }
}
```

Paging: pass the previous response's `next` back as `before` to get the rows older
than it; `next` is opaque (see `/api/tasks`). Both kinds carry `started_at` and the feed
already sorts on it, so one cursor pages the merged list — no composite key. `next: null`
means the feed is exhausted; a full page does not.

Filter semantics (ADR #95):
- `status` — literal match against whichever vocabulary a row uses (`kind`
  tells you which); no normalization. `running`/`failed` match both kinds.
- `pipeline` — executions match `pipeline_name`; backfills match
  `target_pipeline` (a cross-pipeline backfill surfaces under its target only).
- `date` — executions match their logical date; a backfill matches when the
  date is within its `partition_keys` range (daily-oriented).
- Reach depends on the filters: `pipeline` (no date) reads `pipeline-date-index`
  and has no window (bounded by the row TTL); `date` is that one logical date;
  neither is the last 14 days (`Limits.SLA_DAYS`), fanned out one query per day
  because `date` is the shard key for a cross-pipeline feed (ADR #108). Backfills
  come from `list_recent` and may include one older than the window.

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

## Assets — 🔒 Team

The asset *SDK* (declaring `Asset`, inlets/outlets, `wait_for`) is free and
experimental; the asset **console read API** below (`/api/assets*`,
`/api/asset-*`) ships in the Team `ee/` package only and 404s on OSS. Inspect
lineage in OSS with `polyris-output --graph`.

### List & Lineage

```http
GET /api/assets
GET /api/assets/lineage
GET /api/assets/queued?date={YYYY-MM-DD}
GET /api/assets/recent-events?limit=50&date={YYYY-MM-DD}
```

### Asset Events & Actions

```http
GET    /api/asset-events?name={asset_name}&limit=20
POST   /api/asset-trigger?name={asset_name}           # Body: {"date": "..."}
DELETE /api/asset-delete?name={asset_name}
```

### Queue Management

```http
POST /api/assets/skip-in-queue      # Body: {"dag_id", "asset_name", "date"}
POST /api/assets/clear-queue        # Body: {"dag_id", "date"}
POST /api/assets/delete-orphaned    # Body: {"dry_run": false}
```


### Consecutive Progress (v68+)

```http
GET /api/assets/consecutive-progress?pipeline={name}&execution={exec_name}&date={YYYY-MM-DD}
```

Returns progress for tasks with `wait_for` consecutive asset dependencies. Shows which dates have events and which are missing.

```json
{
  "assets": [
    {
      "name": "daily_metrics",
      "consecutive_days": 7,
      "found_dates": ["2025-01-10", "2025-01-11", "2025-01-12"],
      "missing_dates": ["2025-01-13", "2025-01-14", "2025-01-15", "2025-01-16"],
      "ready": false
    }
  ]
}
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
PUT /api/settings/decision-timeout    # 🔒 Team — config mutation (ADR #110)
```

How long a failed task waits on a human decision before the configured fallback
applies (ADR #114). Reading the value is free; changing it is Team
(`task.config` capability) and 404s on an OSS deployment.

---

## Slack Actions — 🔒 Team

Called by Slack interactive message buttons. Team tier — not in the OSS build.

```http
GET /api/action/skip?execution_name={name}
GET /api/action/fail?execution_name={name}
GET /api/action/success?execution_name={name}
GET /api/action/restart?execution_name={name}
```

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
| 202 | Accepted (async, e.g. orchestrated backfill) |
| 400 | Bad request / validation error |
| 404 | Resource not found |
| 409 | Conflict (race condition, e.g. restart non-terminal task) |
| 500 | Internal error |
