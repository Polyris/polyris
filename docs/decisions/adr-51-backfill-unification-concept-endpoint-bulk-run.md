---
area: engine
accepted: 2026-05-20
---
# ADR #51 — Backfill Unification — one concept, one endpoint, one bulk-run SFN

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §5 (1.3.0).** A backfill record carries a `date`, and lives while its backfill runs and 90 days after, instead of 30 days from its creation.

> **Deferral notes (v0.78.0 implementation):**
>
> One design element is not shipped in v0.78 and is marked as deferred:
>
> 1. **`target.type='batch'` (multi-target atomic backfill)** — rejected
>    at validation with 400 `invalid_target_type`. The engine-level
>    Map-of-Maps machinery added ~30% complexity for a use case no user
>    has yet. Re-add when a concrete demand appears; write a follow-up
>    ADR documenting the new contract. All `batch`-related sections
>    below remain as the original design — they are the starting point
>    for v0.79+ when batch lands.
>
> **`options.force` and `options.incremental` are accepted by the API
> but are no-ops in v0.78** — `force` is redundant with backfill
> semantics (a backfill by its nature bypasses scheduled-run dependency
> waits, it's a user-initiated run for a specific date); `incremental`
> was a v0.77 concept that didn't carry forward into the unified model.
> Kept in the schema so existing callers don't 400, but they don't
> change behavior. Documented in `_apply_options` defaults block.
>
> **Real implementation of skip_completed:** `_scan_completed_partitions`
> does live DDB queries to identify partitions where every expected
> task has SUCCESSFUL status. This is the only gate — SFN does not
> have a Check_If_Done state. Bypassed above PREFLIGHT_MAX_PARTITIONS=100
> to stay within API Gateway 29s timeout.
>
> **Child SFN options honored end-to-end (run_task helper):**
> `skip_tasks` (from task_subset), `_suppress_asset_event` (from
> cascade='none'), and `cascade_all` (from cascade='all') are now read
> by the run_task helper template. The plumbing chain is:
> API → bulk_backfill SFN Input → child pipeline ASL Input →
> per-task run_task invocation → Choice states that branch on the
> flags. Default branches preserve existing scheduled-run behavior
> when flags are absent. Contract tests in
> tests/sdk/test_run_task_template.py pin this behavior so future
> edits can't silently strip the support.

#### Context

Pre-v0.78, six distinct code paths perform what is conceptually the same
operation — "start a slice of pipeline work":

1. `POST /api/runs/{name}` — single manual run for one date
2. `POST /api/pipeline-backfill?name={name}` — pipeline backfill across date range
3. `POST /api/assets/backfill` — asset backfill (find producers, run them)
4. `POST /api/dags/{name}/force-trigger` — single force-trigger ignoring deps
5. `usePipelineActions.runFromHere/toHere/onlyThis` — task-level run for one date
6. Matrix cell click — `AssetBackfillModal` invocation for one date

Each path has its own handler, own UI modal, own response shape, own SFN
input format, own `triggered_by` value. They share little code. This
causes:

- **Silent inconsistencies.** Asset-backfill omits `is_backfill=true`,
  so its tasks bypass the run_task SFN's Slack-suppression check — every
  asset-backfill execution spams Slack alerters. Asset-backfill also has
  no `max_parallel` parameter — 5 consumers × 30 days = 150 simultaneous
  `start_execution` calls → ThrottlingException territory.
- **No tracking unit.** A 30-day backfill creates 30 independent SFN
  executions linked only by name pattern (`{pipeline}-{date}-backfill-
  {HHMMSS}`). Cannot pause as a group, cannot cancel pending dates, cannot
  retry only failed dates, cannot show progress (X/30).
- **No partition awareness.** All paths iterate by daily date even when
  the target asset has `granularity="weekly"` or `"monthly"` (ADR #50).
  Weekly asset over 90 days produces 90 daily executions instead of 13
  weekly ones — 86% wasted SFN transitions.
- **Naming collision in DDB.** Existing task records use a field named
  `run_id` whose value is the parent `pipeline_execution`. This was named
  for PagerDuty event grouping but semantically conflicts with the new
  "Backfill record ID" concept introduced here.
- **No cost preview.** User clicks "Backfill 90 days" and discovers
  post-factum what the AWS bill looks like. Competitors (Dagster) also
  lack this; we can do better as a serverless platform where costs are
  knowable per-operation.

#### Decision

Introduce **"Backfill"** as a first-class operation: one endpoint
(`POST /api/backfill`), one orchestrator SFN (`polyris-bulk-backfill`),
one persisted record per operation, one UI modal with seed-based pre-fill
for all six entry points.

**Why "Backfill" not "Run":** the existing `/runs` page in the UI shows
all pipeline executions (cron-driven, manual, asset-event-triggered) and
users associate the word "run" with a single execution. Renaming would
break established habits; the new bulk-operation concept gets a distinct
name. "Backfill" is also industry-standard (Dagster, dbt use
this term for the same concept).

##### Backfill = unit of work

A Backfill is `(target, partition_subset, task_subset, cascade_option,
options)`:

- **target** — what gets materialized:
  - `{type: "pipeline", name: "..."}` — direct pipeline target
  - `{type: "asset", name: "..."}` — asset target, resolved to its
    producer pipeline(s) via outlets in `pipeline_registry`
  - `{type: "batch", items: [...]}` — multiple (asset, partitions) pairs
    in one Backfill (e.g., Matrix multi-cell selection)
- **partitions** — which time slices:
  - `{start, end}` — date range, expanded per target granularity
  - `{keys: [...]}` — explicit list (used by Matrix multi-cell, retry-failed)
- **tasks** — optional task subset within the pipeline (null = all tasks).
  Used by Matrix click → producer task only; by Task Detail Modal "Run from
  Here" → subset of downstream tasks; by manual backfill with task filter.
- **cascade** — applies only when `target.type == "asset"`:
  - `auto` (default) — emit normal asset events; downstream consumers
    decide based on their AND/OR/freshness rules
  - `all` — force-trigger direct consumers regardless of rules
  - `none` — suppress asset event emission (silent re-materialization)
  - Non-transitive: only direct consumers are force-triggered under `all`.
- **options** — operational knobs:
  - `force: bool` — bypass producer's own asset-dependency checks
  - `skip_completed: bool` (default true) — skip partitions where all
    target tasks already succeeded
  - `incremental: bool` — within a partition, skip already-succeeded tasks
  - `max_parallel: int` (default 5, range 1-10) — Map concurrency
  - `allow_concurrent: bool` (default false) — allow starting partitions
    that are currently running in another backfill
  - `variables: dict` — user-defined variables passed to task input

##### One concept replaces six

Each old entry point becomes a seed configuration for the universal
`POST /api/backfill`:

| Entry point | Seed |
|---|---|
| Pipeline page → "Run" | `{target: pipeline, partitions: [today]}` |
| Pipeline page → "Backfill" | `{target: pipeline}` (user fills range) |
| Asset page → "Backfill" | `{target: asset, cascade: "auto"}` |
| Matrix click 1 cell | `{target: asset, partitions: [key], cascade: "auto"}` |
| Matrix multi-select | `{target: batch, items: [...], cascade: "auto"}` |
| Task Detail → Run from Here | `{target: pipeline, tasks: [task, ...downstream], partitions: [today]}` |
| Task Detail → Run to Here | `{tasks: [...upstream, task], partitions: [today]}` |
| Task Detail → Run Only This | `{tasks: [task], partitions: [today]}` |
| Force-trigger | `{target: pipeline, partitions: [today], force: true}` |
| Retry failed of bf-X | `{target: same as X, partitions: {keys: X.failed}, parent_backfill_id: X}` |

UI: six entry points remain as user-facing shortcuts. Backend: one
endpoint, one handler, one resolver, one execution engine.

#### Architecture

```
POST /api/backfill
       ↓
   runs.py::start_backfill
       ↓
   resolve target → producer pipeline(s)
   expand partitions per target granularity
   compute task subset
   pre-flight: skip_completed scan, allow_concurrent check
   validate (limits: soft 500, hard 1000)
   estimate cost
       ↓
   write backfill record to pipeline-tokens
   start polyris-bulk-backfill SFN
       ↓ (returns immediately with backfill_id)
   
polyris-bulk-backfill (Standard SFN):
  Initialize
    ├─ capture pipeline_dag_hash from pipeline_registry
    └─ update backfill record (status=running, hash, partition list)
  Map (MaxConcurrency = options.max_parallel)
    Iterator per partition_key:
      ├─ Check_Backfill_Canceled (DDB read backfill record)
      │     if canceled → succeed iteration (skip)
      ├─ Skip_If_Done (if skip_completed, DDB query for partition status)
      ├─ Build_Child_Input (JSONata: backfill_id, partition_key,
      │     variables, skip_tasks, is_backfill, current_date for legacy
      │     compat with run_task)
      ├─ Start_Child_SFN (Standard sync, child = pipeline's own SFN)
      ├─ Catch ThrottlingException → exponential backoff retry x3
      └─ Update_Counter (DDB UpdateItem ADD completed_count/failed_count)
  Finalize
    └─ status = completed | failed | partial | canceled (computed from counters)
```

##### Data model — `pipeline-tokens` extensions

DynamoDB is schemaless; new attributes are added sparsely:

| Attribute | Type | On row type | Purpose |
|---|---|---|---|
| `record_type` | S | All new rows | `"backfill"` or `"execution"`; sentinel for `is_internal_record` |
| `backfill_id` | S | Execution + Backfill rows | Link execution to parent Backfill (sparse on legacy/cron) |
| `partition_key` | S | All new rows | Granularity-aware partition identifier |
| `cascade_source_backfill_id` | S | Cascade-triggered executions | Lineage to upstream Backfill that emitted the asset event |
| `pipeline_dag_hash` | S | Backfill records only | DAG hash at Initialize (for redeploy detection) |

New GSI on `pipeline-tokens`: **`backfill-id-index`** (PK: `backfill_id`).
Sparse — legacy/cron executions with null `backfill_id` are not indexed.

Existing `run_id` attribute (currently equal to `pipeline_execution`, used
for PagerDuty grouping) is **renamed to `parent_execution_id`** across SFN
templates, `utils.py`, and DDB writes. This frees the semantic space for
`backfill_id` and removes the naming collision. Since no production exists,
the rename is a clean atomic change with no migration path needed.

##### Backfill record shape

```
{
  execution_name = backfill_id (e.g., "bf-a1b2c3d4")
  pipeline_name = "_polyris_bulk_backfill"  # sentinel
  record_type = "backfill"
  
  # User intent
  target_seed = {type, name | items, ...}        # original request for audit
  target_pipeline = "acme/daily_orders"          # resolved producer
  task_subset = ["extract", "transform"] | null
  partition_keys = ["2024-01-15", ...]
  cascade = "auto" | "all" | "none" | null
  options = {force, skip_completed, max_parallel, ...}
  
  # Lifecycle
  status = "pending" | "running" | "completed" | "failed" | "partial" | "canceled"
  total_partitions = 30
  completed_partitions = 18    # incremented by Map iteration
  failed_partitions = 2
  skipped_partitions = 5
  
  # Context
  pipeline_dag_hash = "a1b2c3d4"   # captured at Initialize
  started_by = "makskoval@..."     # from Cognito claim
  started_at = "2026-05-20T07:55:00Z"
  finished_at = "2026-05-20T08:12:30Z"
  parent_backfill_id = "bf-xyz789" | null   # set if this is retry-failed of another
  
  # Operational
  sfn_arn = "arn:aws:states:...:execution:polyris-bulk-backfill:bf-a1b2c3d4"
  child_executions = ["acme-daily-2024-01-15-...", ...]  # populated as they start
  ttl = epoch + 30 days
}
```

#### API contract

##### `POST /api/backfill`

Request body:

```json
{
  "target": {
    "type": "pipeline" | "asset" | "batch",
    "name": "acme/daily_orders",
    "items": [{type, name, partitions}, ...]   // for batch only
  },
  "tasks": ["transform", "load"] | null,
  "partitions": {
    "start": "2024-01-01",
    "end": "2024-01-31"
  },
  "cascade": "auto" | "all" | "none" | null,
  "options": {
    "force": false,
    "skip_completed": true,
    "incremental": false,
    "max_parallel": 5,
    "allow_concurrent": false,
    "variables": {}
  }
}
```

Response 202 Accepted:

```json
{
  "backfill_id": "bf-a1b2c3d4",
  "target_pipeline": "acme/daily_orders",
  "granularity_inferred": "daily",
  "partition_count_requested": 31,
  "partition_count_skipped_completed": 23,
  "partition_count_skipped_running": 2,
  "partition_count_to_run": 6,
  "task_subset": ["extract", "transform", "load", "publish"],
  "cascade": "auto",
  "estimated_sfn_cost_usd": 0.006,
  "estimated_duration_minutes": 4,
  "warnings": [
    "weekly_summary uses .within(hours=6) — events older than 6h will not trigger it"
  ],
  "ui_url": "/backfills/bf-a1b2c3d4"
}
```

Error responses:

| Status | Code | Cause |
|---|---|---|
| 400 | `target_not_found` | Pipeline or asset doesn't exist in registry |
| 400 | `multi_producer_asset` | Asset has >1 producer; user must pick one (returned with producer list) |
| 400 | `no_producer` | Asset has no producer pipeline (external-only) |
| 400 | `invalid_partition_format` | partition key doesn't match granularity format |
| 400 | `partition_start_violation` | requested range below asset's `partition_start` |
| 400 | `range_too_large` | >1000 partitions (hard limit) |
| 400 | `range_outside_target` | end < start, or both in future >7 days |
| 400 | `invalid_cascade_for_pipeline_target` | cascade option set when target is pipeline (cascade applies to asset only) |
| 422 | `nothing_to_run` | After skip_completed, 0 partitions remain (preview-only condition) |
| 503 | `throttled` | SFN start_execution throttled; retry with exponential backoff |

##### Other endpoints

- `GET /api/backfills` — list (filters: status, target, started_by, time range; sort: started_at desc)
- `GET /api/backfills/{id}` — detail with child executions, progress, cascade warnings
- `POST /api/backfills/{id}/cancel` — mark status=canceled in DDB; bulk-run Map's `Check_Backfill_Canceled` skips remaining iterations
- `POST /api/backfills/{id}/retry-failed` — create new backfill with `partitions.keys = failed`, `parent_backfill_id = id`

##### Deprecated endpoints (deleted, since no production)

The following are removed entirely in v0.78:
- `POST /api/runs/{name}`
- `POST /api/pipeline-backfill`
- `POST /api/assets/backfill`
- `POST /api/dags/{name}/force-trigger`

UI components that called them are also removed:
- `BackfillModal.tsx`
- `AssetBackfillModal.tsx`
- `usePipelineActions::handleBackfill`
- `useBackfillMutation`, `useAssetBackfillMutation`, `useForceTriggerMutation`, `useRunPipelineMutation`

#### Cascade semantics (overview)

Detailed rules in ADR #57. Summary here:

When `target.type == "asset"`, the endpoint:

1. Resolves asset → producer pipeline(s) via `pipeline_registry` outlets.
   If >1 producer found, returns 400 with producer list (user must pick).
2. Expands partitions per asset's granularity (ADR #58).
3. Runs producer pipeline for each partition (one child SFN per partition,
   via Map).
4. Applies cascade per producer execution success:
   - `auto`: producer emits normal asset event; downstream consumers
     evaluate their AND/OR/freshness rules and trigger (or not) accordingly.
     Late events (event_time hours behind now) fail `within()` checks —
     this is correct behavior, not a bug; surfaced in preview warnings.
   - `all`: after producer success, directly invoke notify_asset_consumers
     SFN with `force=true` flag (new field), which bypasses freshness/AND
     checks and starts downstream pipelines regardless.
   - `none`: producer execution completes but the asset event emission
     step is skipped (new flag `_suppress_asset_event=true` in execution
     input, consumed by notify_dependents).

Cascade is **non-transitive** — only direct consumers are affected.
If `asset_A → asset_B → asset_C` and Backfill on A uses `cascade=all`,
B's producer runs (force), B emits its own event normally, C's reaction
follows C's rules (whether it triggers depends on C's setup, not on
A's cascade choice).

Cascade `lineage`: cascade-triggered executions (under `auto` or `all`)
carry `cascade_source_backfill_id` in their input → stored in DDB →
visible in `/runs` row as "🔗 cascade from bf-a1b2c3d4". Click on the
link navigates to the originating Backfill detail page. Question #7 (A).

#### Edge cases — matrix

| Scenario | Behavior |
|---|---|
| Asset has 1 producer | Standard flow |
| Asset has N producers | 400 with `multi_producer_asset` + list; user picks one |
| Asset has 0 producers (external) | 400 with `no_producer`; user may use `target=batch` with `cascade=all` for event-only emission (future ADR) |
| `partition_start` is mid-bucket | Round down to bucket boundary (ADR #58); preview shows clamped range |
| Requested range below `partition_start` | 400 with `partition_start_violation` |
| Pipeline has no schedule | Cron inference defaults to "daily" with no warning |
| Pipeline schedule is ambiguous (`0 8 * * 1-5`) | Inference defaults to "daily" with explicit warning in preview |
| Pipeline outlets mixed granularities | Disallowed at deploy time (ADR #52); cannot occur at runtime |
| Concurrent backfill on same partition | Default: pre-flight skip; opt out via `allow_concurrent=true` |
| Cancel race (iteration started between cancel + check) | Up to N race-through (where N = max_parallel); documented as acceptable |
| Mid-run pipeline redeploy | `pipeline_dag_hash` captured at Initialize; if changed by run end, Backfill Detail UI shows warning banner |
| Consumer paused mid-cascade | Auto mode: events queue (existing behavior); shown in cascade summary |
| Consumer freshness gate fails | Auto mode: consumer skipped silently; surfaced in cascade summary; preview warns proactively |
| `consecutive(days=N)` partial coverage | Auto mode: consumer not triggered until N consecutive days exist; surfaced in cascade summary |
| `skip_completed=true` + 0 to_run | 422 `nothing_to_run` with body explaining why; UI shows "Nothing to do" before submit |
| Bulk-run SFN itself throttled at start | 503 `throttled`; UI retries with backoff transparently |

#### Migration

Since no production deployment exists, no backward compatibility is
preserved. All old endpoints, UI modals, hooks, and DDB field semantics
are replaced atomically in v0.78.0.

The `run_id` → `parent_execution_id` rename is a synchronized change
across:
- `sam/sfn_templates/dependency_wrapper/sfn.tpl.json` (4 sites)
- `sam/sfn_templates/helpers/run_task/sfn.tpl.json` (5 sites)
- `sam/sfn_templates/helpers/pagerduty_alerter/sfn.tpl.json` (1 site)
- `sam/sfn_templates/helpers/pagerduty_resolver/sfn.tpl.json` (1 site)
- `sam/lambdas/console_api/utils.py` (3 sites)

The new `backfill_id` field is added to the same SFN templates (sparse —
written only when execution is part of a Backfill).

Snapshot tests under `tests/snapshots/` are re-baselined as part of the
implementation; PR review verifies the diff matches the rename + addition
pattern.

#### Limits

Hard-coded constants in `sam/lambdas/console_api/constants.py::BackfillLimits`:

- `PARTITION_SOFT_LIMIT = 500` — preview shows warning, allows submit
- `PARTITION_HARD_LIMIT = 1000` — endpoint returns 400; for larger backfills,
  user must chunk
- `MAX_PARALLEL = 10` — Map state MaxConcurrency upper bound (lower for safety)
- `RECORD_TTL_DAYS = 30` — same as executions, for consistency
- `BULK_BACKFILL_SFN_TIMEOUT = 24h` — Standard SFN max wall time

#### CLI

New `polyris.run_cli` module exposes `polyris backfill` and `polyris
backfills` subcommands:

```
polyris backfill pipeline acme/daily_orders \
    --partitions 2024-01-01:2024-01-31 \
    --max-parallel 5

polyris backfill asset acme/orders \
    --partitions 2024-01:2024-03 \
    --cascade auto

polyris backfills list [--status running] [--target ...] [--user ...]
polyris backfills show bf-a1b2c3d4
polyris backfills cancel bf-a1b2c3d4
polyris backfills retry bf-a1b2c3d4
```

Thin wrapper over `/api/backfill`; AWS credentials via standard boto3
profile chain. Used by CI pipelines, cron jobs on dev machines, and
power users.

#### Why these decisions

| Decision | Rationale |
|---|---|
| Single concept "Backfill" | Eliminates 6 code paths with shared logic; reduces maintenance surface ~30%; matches industry term |
| Separate DDB table not used | Schema is sparse and clean inside `pipeline-tokens` via sentinel + `record_type`; one less migration concern; one less DAL repo file (until #4 below) |
| Separate `backfills_repo.py` DAL | Question #4 (A); different concept lifecycle, cleaner tests, future-proof for separate TTL/audit table |
| Standard SFN for bulk-backfill | Question #2; simpler than Express+watcher; cost $0.01/backfill negligible at any volume |
| Cron inference at runtime | Question #3 (B); no DSL change; fallback to daily with warning for ambiguous cron |
| `pipeline_dag_hash` captured | Question #5 (D); leverages existing `generate_dag_hash`; honest visibility of mid-run redeploys without false promise of point-in-time execution |
| `/runs` page unchanged, `/backfills/{id}` added | Question #6; preserves user habits; filter chip + lineage column gives drill-in path |
| Cascade lineage with UI | Question #7 (A); complete feature, not stubbed; uses existing infrastructure (notify_asset_consumers SFN); low LOC |
| Soft 500 / Hard 1000 | Question #8 (A); guardrail + protection; based on real payload size + reasonable cognitive limit |
| 30-day TTL | Question #9 (A); consistency with executions; audit needs older than 30 days are out of scope |
| CLI in v0.78 | Question #10 (A); commodity feature; serves CI/automation use case from launch |
| Schedule runs NOT through bulk-backfill | Question #6 implication; preserves existing direct EventBridge → SFN path; `/runs` shows them as before |
| Force-trigger collapsed into Backfill | One-day-partitions backfill with `force=true` is functionally identical; no need for separate concept |

#### Alternatives considered (rejected)

1. **Keep three separate handlers, fix bugs in-place** — addresses the 5
   immediate hardening items from v0.77.2 audit but leaves the
   no-tracking-unit problem, granularity ignorance, and code duplication.
   Doesn't unlock cancel/retry/cost-preview features.

2. **Express SFN with separate watcher** — 110× cheaper per backfill
   ($0.0001 vs $0.0091), but requires separate poller SFN, fragments
   state across two engines, and edge-cases on 5-min timeout for large
   backfills. Cost saving is meaningless at our scale.

3. **Bulk-backfill in Lambda (no SFN)** — eliminates SFN cost entirely
   but loses native cancel, concurrency control, retry, observability.
   Custom code burden offsets the saving. Also fails CLAUDE.md "SFN-first
   architecture" (memory item: async push model shelved).

4. **Rename `/runs` → `/executions`, new `/runs` for Backfills** —
   considered until rejected by user (sees existing /runs page in
   production usage). Habit break is real UX cost.

5. **"Backfill" not "Run" naming** — chosen over "Run" because (a)
   conflicts with existing UI terminology, (b) industry-standard term
   for this concept (Dagster, dbt), (c) avoids re-education
   cost for users coming from those tools.

#### Cost

Per backfill (90-day daily example):
- Bulk-backfill Standard SFN: ~365 transitions × $0.025/1000 = **$0.0091**
- DDB writes: Backfill record (1 PutItem) + 90 counter updates + 90
  child execution rows = ~$0.00015
- Lambda invocations: 1 × `/api/backfill` handler = ~$0.0000002

**Total: ~$0.01 per 90-day backfill.**

Per month (estimated 50 backfills at this size):
- Backfill operations: 50 × $0.01 = $0.50
- New GSI `backfill-id-index`: $0.15/month flat (sparse storage)
- **Total: +$0.65/month over baseline.**

For 10× volume (500 backfills/month): +$5.15/month. Cost scales linearly
and remains marginal.

#### Open questions deferred to subsequent ADRs

- ADR #52: pipeline granularity validation rules
- ADR #53: cost preview formula and update mechanism
- ADR #54: bulk-backfill SFN state machine detailed design
- ADR #55: scheduled runs (out-of-scope confirmation, not migration)
- ADR #56: backfill status model + mapping to execution statuses
- ADR #57: cascade semantics — full edge case enumeration
- ADR #58: partition keys format and range expansion algorithm
