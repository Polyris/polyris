# polyris Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              polyris System                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────────┐ │
│  │ polyris-deploy│──▶│  Pipeline   │───▶│         AWS Resources           │ │
│  │  (deploy)   │    │    SFN      │    │  • Step Function                │ │
│  └─────────────┘    └─────────────┘    │  • EventBridge Rule             │ │
│        │                   │           │  • CloudWatch Logs              │ │
│        ▼                   ▼           └─────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    Shared Infrastructure (SAM/CloudFormation)         │   │
│  │   ┌───────────────┐    ┌───────────────┐    ┌──────────────────┐   │   │
│  │   │  dependency   │    │  EventBridge  │    │    DynamoDB      │   │   │
│  │   │   wrapper     │    │    Rules      │    │    (8 tables)    │   │   │
│  │   └───────────────┘    └───────────────┘    └──────────────────┘   │   │
│  │          │                     │                                   │   │
│  │   ┌───────────────┐    ┌───────────────┐                          │   │
│  │   │   Helpers     │    │    Lambdas    │                          │   │
│  │   │ • run_task    │    │ • console_api │                          │   │
│  │   │ • failure     │    │ • evaluate_dep│                          │   │
│  │   │ • registration│    │ • query_subs  │                          │   │
│  │   │ • notify_deps │    │ • check_asset │                          │   │
│  │   │ • notify_asset│    │ • notify_a_sub│                          │   │
│  │   │ • slack       │    │ • ui_bootstrap│                          │   │
│  │   │ • pagerduty   │    └───────────────┘                          │   │
│  │   │ • +5 more     │                                               │   │
│  │   └───────────────┘                                               │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────────────┐│
│  │                        Web Console (React)                           ││
│  │   • Pipeline list & status         • Asset lineage graph            ││
│  │   • DAG / Gantt / Calendar         • Backfill with date range       ││
│  │   • Auto-refresh (polling)         • Task actions (skip/restart)    ││
│  └──────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Design Principles

These are the load-bearing decisions the rest of the architecture follows from.
(Each is expanded in [DESIGN_DECISIONS.md](../reference/DESIGN_DECISIONS.md).)

1. **Nothing to run — Step Functions *is* the runtime.** polyris has no
   scheduler, no worker pool, and no orchestrator metadata database to operate.
   A pipeline compiles to a Step Functions state machine; EventBridge Scheduler
   triggers it; AWS runs it. Between runs the system scales to zero. The only
   always-on cost is the shared control surface (console API, DynamoDB, the
   helper state machines) — a typical deployment is ~$31/month, and a marginal
   pipeline run is fractions of a cent.

2. **Nothing hidden — SFN-first over event-cleverness.** Execution flows through
   visible, debuggable Step Functions executions rather than opaque event
   choreography. Earlier designs that fanned work out through EventBridge as a
   "black box" were deliberately replaced with Step Functions so that every run
   has an inspectable history and state. Observability and transparency beat
   cleverness; EventBridge is used for scheduling and asset-event delivery, not
   as the execution substrate.

3. **Asset-centric, not just task-aware.** Tasks declare the data assets they
   produce (`outlets`). Assets form a graph with lineage and partitions, and
   that graph — not just the task DAG — drives cross-pipeline triggering, the
   asset matrix (partition status across assets), and lineage-aware backfill.

4. **Data passes through a canonical output store.** Tasks do not hand data to
   each other in-process. Each task writes its result to a stable key
   (`output#{pipeline}#{task}#{date}`) in DynamoDB; downstream tasks read their
   upstreams' outputs from there (`Read_Upstream_Outputs` → `Prepare_Task_Input`
   in the run_task helper). This survives partial/incremental backfill — an
   auto-skipped task's prior output is still readable — and makes "is this
   upstream ready for this partition?" a single point lookup. Outputs carry a
   TTL, so very deep historical backfills may find an ancestor's output expired
   (surfaced as a backfill warning).

5. **Single source of truth, generated.** Enum families and shared constants are
   defined once in `polyris/constants.py` and code-generated into every Lambda
   (`constants_generated.py`) and the UI (`ui/src/generated/enums.ts`), with CI
   drift gates. Hand-maintained copies of the same vocabulary are treated as
   bugs (see DESIGN_DECISIONS #72/#83/#93/#94).

6. **No silent failures.** Alerts (Slack/PagerDuty) are configured per pipeline in
   the Console UI; on failure the notify Lambda reads that config from DynamoDB and
   fans out to every enabled channel (an empty config is a clean no-op).

---

## Data Flow: "What Calls What"

Use this section to debug "why didn't my task start?" — trace the chain step by step.

### Flow 1: Happy Path (Scheduled Pipeline Run)

```
EventBridge Schedule Rule (cron)
     │
     ▼
Orchestrator SFN (pipeline-level)
     │  launches all tasks in parallel via Map
     │  passes: date, pipeline_execution, orchestration_token
     ▼
┌─────────────────────────────────────────────────────────────────┐
│  Dependency Wrapper SFN (one per task)                          │
│                                                                 │
│  1. Emit_Wrapper_Started ──────► pipeline_tokens [putItem]      │
│  2. Wait_For_Dependencies ─────► Registration Helper SFN        │
│     │                              │                            │
│     │    ┌─────────────────────────┘                            │
│     │    │  a. Save_Task_Record ───► pipeline_tokens [putItem]  │
│     │    │  b. Write subscriptions ► dependency_subscriptions    │
│     │    │  c. Check if deps done ─► evaluate_deps Lambda       │
│     │    │  d. If ready: signal ───► SendTaskSuccess (token)    │
│     │    └─────────────────────────────────────────────────     │
│     │                                                           │
│  3. Emit_Deps_Ready ──────────► pipeline_tokens [putItem]       │
│  4. Run_Task Helper SFN ──────► (see Flow 2)                    │
│  5. Handle_Failure (if error) ► failure_handler SFN             │
└─────────────────────────────────────────────────────────────────┘
```

### Flow 2: Task Execution (run_task Helper)

```
Run_Task Helper SFN
     │
     ├─ Check_Execution_Paused ──► pipeline_registry [getItem]
     │    └─ if paused ──────────► pause_waiter SFN (waitForTaskToken)
     │
     ├─ Update_Status_Running ───► pipeline_tokens [updateItem]
     ├─ Emit_Task_Started ───────► task_events [putItem]
     │
     ├─ Read_Upstream_Outputs ───► pipeline_tokens [getItem] (data passing)
     │
     ├─ Execute task (one of):
     │    ├─ Run_Task_SFN ────► target Step Function (.sync:2)
     │    ├─ Run_Task_Lambda ─► target Lambda
     │    ├─ Run_Task_Glue ───► Glue Job (.sync)
     │    ├─ Run_Task_ECS ────► ECS Task (.sync)
     │    ├─ Run_Task_Athena ─► Athena Query (.sync)
     │    ├─ Run_Task_EMR ────► EMR Step (.sync)
     │    └─ Run_Task_Batch ──► AWS Batch (.sync)
     │
     ├─ ON SUCCESS:
     │    ├─ Save_Success ───────────► pipeline_tokens [updateItem]
     │    ├─ Emit_Task_Finished ─────► task_events [putItem]
     │    ├─ Notify_Dependents ──────► notify_dependents SFN (see Flow 3)
     │    ├─ Emit_Asset_Events ──────► asset_events [putItem] (if outlets)
     │    ├─ Notify_Asset_Subscribers ► Lambda (see Flow 4)
     │    └─ Send_Pipeline_Success ──► orchestrator token (if last task)
     │
     └─ ON FAILURE:
          ├─ Save_Error_Waiting ─────► pipeline_tokens (status=waiting_decision)
          ├─ Check_Is_Backfill ──────► if backfill: skip alerts, go to Wait_For_Decision
          ├─ Interactive_Slack ──────► notify Lambda: Slack w/ buttons (Skip/Restart/Fail)
          ├─ Send_PagerDuty_Alert ───► notify Lambda: PagerDuty (immediate, actionable)
          ├─ Wait_For_Decision ──────► 5h wait for human response
          ├─ Save_Failed ────────────► pipeline_tokens [updateItem]
          ├─ Notify_Dependents_Failed ► notify_dependents SFN
          └─ Send_Pipeline_Failure ──► orchestrator token
```

### Flow 3: Dependency Notification Chain

This is the core of "how does task B know that task A finished?"

```
Task A completes (success or failed)
     │
     ▼
notify_dependents SFN
     │
     ├─ Query_Subscriptions ─────► query_subscriptions Lambda
     │    └─ reads ──────────────► dependency_subscriptions table
     │       query: task_name + pipeline_execution_short
     │       returns: list of subscribers [{task_name, wait_token, ...}]
     │
     ├─ Has_Subscribers? ── no ──► Done (no one waiting)
     │
     └─ yes ► Process_Subscribers (Map, concurrency=10)
              │
              for each subscriber:
              │
              ├─ Get_Subscriber_Record ──► pipeline_tokens [getItem]
              │    (get full record: deps, trigger_rule, wait_token)
              │
              ├─ evaluate_deps Lambda
              │    input: dependencies[], trigger_rule, date, execution
              │    reads: pipeline_tokens [BatchGetItem] — all dep statuses
              │    reads: pipeline_tokens [BatchGetItem] — skip_origin, only if a dep is skipped
              │    logic: apply trigger_rule (5 rules — ADR #117)
              │    output: {is_ready, is_blocked, verdict, reason, dep_statuses}
              │
              ├─ If is_ready ────► SendTaskSuccess(wait_token)
              │    └─ wrapper resumes at step 3 (Emit_Deps_Ready)
              │
              ├─ If verdict='skip' ──► SendTaskSuccess({signal: 'deps_skip'})
              │    └─ wrapper resolves 'skipped', run stays 'success' (ADR #115) —
              │       the rule's condition never occurred; not an error
              │
              ├─ If verdict='upstream_failed' ──► SendTaskSuccess({signal: 'deps_blocked'})
              │    └─ wrapper goes to Emit_Deps_Blocked → failure path → resolves 'upstream_failed'
              │
              └─ Otherwise ──────► skip (still waiting for other deps)
```

### Flow 4: Asset-Based Cross-Pipeline Triggers

```
Producer task completes with outlets: [{name: "inventory"}]
     │
     ▼
notify_asset_subscribers Lambda
     ├─ Write asset_events [putItem] (asset_name + event_time)
     ├─ Query asset_subscriptions (who subscribes to "inventory"?)
     │    returns: [{dag_id: "consumer_pipeline", ...}]
     │
     └─ For each subscriber:
          │
          ├─ PULL model (wait_for):
          │    └─ check_assets Lambda (called from registration helper)
          │         reads: asset_events — is asset fresh enough?
          │         if yes → signal task ready
          │
          └─ PUSH model (asset_trigger on DAG → notify_asset_consumers SFN):
               ├─ OR logic: start pipeline immediately
               └─ AND logic:
                    ├─ Write queued_asset_events [putItem]
                    ├─ Check: all required assets received?
                    └─ If all present → StartExecution (consumer pipeline)
```

### Flow 5: Failure → Alerting

Two-line alerting architecture:

**Line 1 — run_task helper (immediate, while task is actionable):**
```
Task error caught by run_task
     │
     ├─ Save_Error_Waiting ──────► pipeline_tokens (status=waiting_decision)
     ├─ Get_Decision_Timeout ────► read global decision-wait timeout (registry __global_settings__)
     ├─ Check_Is_Backfill ───────► backfill? skip alerts → Wait_For_Decision (no Slack/PD)
     ├─ Interactive_Slack ──────► notify Lambda — Slack w/ buttons (Skip/Restart/Mark Success/Fail)
     ├─ Send_PagerDuty_Alert ───► notify Lambda — PagerDuty (fires immediately)
     └─ Wait_For_Decision ───────► decision-timeout window for human response
          └─ timeout ────────────► Save_Failed → wrapper catches → failure_handler
```

Both the interactive Slack post and the PagerDuty alert are `lambda:invoke` calls
to the single notify Lambda — there are no separate alerter state machines (ADR
#103). The Lambda reads the pipeline's alert_config from the registry itself and
posts to whichever channels are enabled.

**Line 2 — failure_handler (after task is terminal):**
```
Wrapper catches failure
     │
     ▼
failure_handler SFN
     │
     ├─ Truncate_Error ──────────► (prevent DynamoDB 400KB limit)
     ├─ Update_Status_Failed ────► pipeline_tokens [updateItem]
     ├─ Emit_Failure_Event ──────► task_events [putItem]
     │
     ├─ Notify_Dependents ───────► notify_dependents SFN
     │    (so downstream tasks get blocked/triggered per trigger_rule)
     │
     ├─ Check_Is_Upstream_Failed ► upstream_failed? skip Slack (root cause already alerted)
     │
     ├─ Check_Slack_Alert ── yes ► Slack (restart-only message, no action buttons)
     │    └─ on Slack failure ───► pipeline_tokens [updateItem]
     │                              (record slack_notification_failed=true)
     │
     └─ Send_Task_Failure ───────► orchestrator token (callback)
```

**Key design decisions:**
- PagerDuty fires in line 1 (immediate) so on-call can act during the decision-wait window (global timeout, default 5h, ADR #103 1b)
- Backfill suppresses all alerts — results visible in UI calendar
- upstream_failed tasks don't alert — root cause task already sent notifications
- Line 2 Slack sends restart-only message (no Skip/Fail buttons on dead task)
- PagerDuty removed from line 2 — already triggered in line 1, PD handles escalation

### Flow 6: Pipeline Registration on Deploy

**Primary path (polyris-deploy lifecycle):**
```
polyris-deploy
     │  creates/updates Step Function
     ▼
PipelineRegistration dynamic resource
     │  create/update handler
     ▼
StartExecution(register_only=true)
     ├─ Register_Pipeline ──────► pipeline_registry [putItem]
     ├─ Save_DAG_Snapshot ──────► tokens_table [putItem]
     └─ RegisterAssetSubscriptions (if asset triggers)
          └─ Map ──────────────► asset_subscriptions [putItem]
```

**On destroy:**
```
polyris-deploy --destroy
     │
     ▼
PipelineRegistration.delete()
     ├─ pipeline_registry ──── DELETE pipeline_name
     └─ asset_subscriptions ── DELETE for each asset
```

### Quick Debug Reference

| Symptom | Where to look | Table / Service |
|---------|--------------|-----------------|
| Task stuck in `waiting` | registration helper execution | `dependency_subscriptions` — is subscription there? |
| Task never becomes `deps_ready` | notify_dependents execution | `pipeline_tokens` — check dep statuses |
| Task `deps_ready` but not running | run_task helper execution | `pipeline_registry` — is pipeline paused? |
| Asset trigger didn't fire | notify_asset_subscribers logs | `asset_events` + `asset_subscriptions` |
| No Slack alert on failure | run_task + failure_handler execution | Check `alerts.slack` config, `notification_failed` field in token |
| Pipeline not in UI after deploy | Check `polyris-deploy` output for PipelineRegistration | `pipeline_registry` — registered? CFN stack has registration output |
| Wrong DAG in UI for old execution | Check `dag_source` in API response | `pipeline_tokens` — `dag_snapshot::{execution}` exists? |
| Wrong trigger_rule result | evaluate_deps Lambda logs | Check `dep_statuses` + `trigger_rule` in log |

---

## Failure Handling Flow

```
Task Failure
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│                 run_task helper (Line 1)                     │
│                                                             │
│   1. Save_Error_Waiting (DDB: status=waiting_decision)      │
│   2. Check_Is_Backfill → backfill? skip alerts → Wait_For_Decision │
│   3. Interactive_Slack → notify Lambda: Slack (buttons)            │
│   4. Send_PagerDuty_Alert → notify Lambda: PagerDuty              │
│   5. Wait_For_Decision (5h timeout)                         │
│   6. Save_Failed → Notify_Dependents → Send_Pipeline_Failure│
└──────────────────────────┬──────────────────────────────────┘
                           │ wrapper catches failure
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                 failure_handler SFN (Line 2)                 │
│                                                             │
│   1. Truncate_Error (prevent DynamoDB limits)               │
│   2. Update_Status_Failed (DynamoDB)                        │
│   3. Emit_Failure_Event (task_events)                       │
│   4. Notify_Dependents (trigger_rules evaluation)           │
│   5. Check_Is_Upstream_Failed → skip alerts if cascade      │
│   6. Check Slack → Send restart-only message (no buttons)   │
│   7. Send_Task_Failure (callback to pipeline)               │
└─────────────────────────────────────────────────────────────┘
```

---

## DynamoDB Tables

| Table | Purpose | PK | SK |
|-------|---------|----|----|
| pipeline_tokens | Task state | execution_name | - |
| dependency_subscriptions | Dependency tracking | dependency_key | - |
| pipeline_registry | Pipeline metadata | pipeline_name | - |
| asset_events | Asset history | asset_name | event_time |
| asset_subscriptions | Asset→DAG subscriptions | asset_name | dag_id |
| queued_asset_events | AND trigger queue | dag_id#date | asset_name |
| task_events | Task history | task_run_id | event_time |

---

## Trigger Rules

Canonical: `all_success` (default), `one_success`, `all_done`. The other 8 are accepted
accepted rule names — see `docs/features/DSL.md#trigger-rules` for which behave
distinctly vs. collapse onto a canonical one under the intervention-first failure
model (ADR #114), and `docs/reference/adr-115-canonical-trigger-rules-and-skip-semantics.md`
for the full rationale.

| Rule | When triggers |
|------|---------------|
| `all_success` | All deps = success, or skipped with `skip_origin != 'rule'` (default) — a rule-originated skip does *not* count as ok (ADR #115) |
| `all_done` | All deps finished (any status) |
| `all_skipped` | All deps = skipped |
| `one_success` | At least one dep = success (immediate!) |
| `none_skipped` | No deps skipped |

5 rules total (ADR #117 — 6 names are rejected outright; the other 6 either duplicated one
of these in every state reachable under the intervention-first model, ADR #114, or
could never fire at all — see `docs/features/DSL.md#trigger-rules`).

A blocked rule (all deps terminal, condition not satisfied) resolves one of two ways
(`evaluate_deps`'s `verdict` field, ADR #115): `upstream_failed` when a
success/no-failure-requiring rule is blocked by a genuine failure; `skip` (the task
resolves `skipped`, run stays `success`) when the condition simply never occurred —
not an error.

---

## Task Statuses

| Status | Description |
|--------|-------------|
| `waiting` | Waiting for dependencies |
| `deps_ready` *(signal, not persisted)* | `notify_dependents`/`registration`'s callback payload when deps satisfy the rule — the wrapper resumes and the task actually runs; not a status you'll see stored |
| `deps_blocked` *(signal, not persisted)* | Callback payload when a genuine failure blocks a success-requiring rule — resolves to the persisted `upstream_failed` |
| `deps_skip` *(signal, not persisted)* | Callback payload when a rule's condition legitimately never occurred (ADR #115) — resolves to the persisted `skipped` |
| `waiting_delay` | wait_before countdown |
| `waiting_paused` | Pipeline paused, task waiting for resume |
| `waiting_decision` | Waiting for manual decision (interactive Slack) |
| `pending` | Pending redrive (SFN) |
| `running` | Executing |
| `success` | Completed successfully |
| `failed` | Failed |
| `skipped` | Skipped — manually (task action, `skip_origin='manual'`), by the pre-execution `skip_tasks` list (`skip_reason='auto_skipped'`), or by a trigger_rule condition that never occurred (`skip_origin='rule'`) |
| `stopped` | Manually stopped (can restart) |
| `aborted` | Force stopped (terminal) |
| `upstream_failed` | Blocked due to upstream failure |

---

## Step Functions Helpers

| Component | Purpose |
|-----------|---------|
| **sf_dependency_wrapper** | Main wrapper - handles deps, execution, failures |
| **sf_registration_helper** | Register task + subscriptions, check initial deps |
| **sf_run_task_helper** | Execute task (SFN/Lambda/Glue/ECS/Athena/EMR/Batch) |
| **sf_failure_handler** | Update DB, emit events, notify dependents, send follow-up alerts via notify Lambda |
| **sf_pause_waiter** | Save pause token, wait for resume callback |
| **sf_notify_dependents** _(EXPRESS)_ | Query subscribers, evaluate trigger rules, send tokens |
| **sf_notify_asset_consumers** _(EXPRESS)_ | Cross-pipeline asset triggers (PUSH/AND/OR) |
| **sf_restart_task_helper** _(EXPRESS)_ | Restart failed task |
| **sf_restart_wrapper** _(EXPRESS)_ | Restart wrapper execution |
| **sf_register_on_create** | ~~Removed in v69.1~~ — replaced by `register_pipeline` SFN (ADR #24) |

> Interactive Slack and PagerDuty alerts/resolves are **not** separate state
> machines. They are `lambda:invoke` calls from run_task / failure_handler /
> dependency_wrapper to the single notify Lambda (ADR #103). The earlier
> `sf_slack_interactive`, `sf_pagerduty_alerter`, and `sf_pagerduty_resolver`
> Express SFNs (and the EventBridge Connection that backed them) were removed.

---

## Backfill (lineage-aware)

Backfill re-runs a target over a range of partitions. It is a first-class
operation with its own DynamoDB record and a dedicated **bulk-backfill Standard
Step Function** (Standard, not Express, so the history is inspectable). The
console API resolves the request into a plan; the SFN executes it.

**Targets & partitions.** A target is a pipeline or an asset. Partitions are a
date range (`start`/`end`) or an explicit key list. Granularity (daily, weekly,
monthly, hourly) is inferred from the resolved pipeline's cron, with a
declarative override and drift detection (ADR #50/#52).

**Upstream (asset target, ADR #92).** Controls how much of the producer's
*same-pipeline* lineage is rebuilt:
- `off` (default) — only the producer (input assumed present).
- `smart` — walk the producer's ancestors and build only those whose canonical
  output is missing for a requested partition; the frontier stops at ancestors
  whose output already exists (read from the store). `skip_on_backfill` tasks
  are never re-run.
- `force` — rebuild the full same-pipeline lineage regardless of presence.

**Downstream / cascade (asset target, ADR #91).** `auto` (default) triggers
direct consumers of the affected asset; `all` triggers the whole downstream
subgraph; `none` triggers nothing. (`cascade` is the deprecated field alias for
`downstream`.)

**Execution shape (ADR #90).** The bulk-backfill SFN is a nested Map: an outer
tier Map runs lineage tiers sequentially (so an upstream tier finishes before
the tiers that depend on it), and an inner Map runs the partitions of a tier
with `max_parallel` concurrency. Partitions whose output already exists are
skipped; a failed/cancelled upstream tier gates the tiers below it.

**Preview.** `POST /api/backfill?preview=true` returns the resolved plan —
partition counts, reused/skipped counts, the upstream-lineage scope
(`tasks_to_run`/`partitions`), and warnings (e.g. an ancestor output expired by
TTL) — without starting the SFN. There is no cost estimate (removed in v0.78.2).

---

## Cost Model

| Resource | Pricing |
|----------|---------|
| Step Functions | $0.025 / 1000 transitions |
| DynamoDB | ~$0.25 / million requests |
| Lambda | $0.20 / 1M requests |
| EventBridge | $1.00 / million events |

**8-task pipeline, 1x/day, 30 days = ~$0.50/month** (vs MWAA ~$300/month)

---

## Task Status Lifecycle

```
                    ┌─────────────────────────────────────────────────┐
                    │                    WAITING                       │
                    │  (initial - waiting for dependencies)            │
                    └─────────────────────────────────────────────────┘
                                          │
                          ┌───────────────┼───────────────┐
                          ▼               ▼               ▼
                    ┌───────────┐  ┌─────────────┐  ┌───────────────┐
                    │ DEPS_READY│  │WAITING_PAUSED│  │DEPS_BLOCKED   │
                    │           │  │(pipe paused) │  │→upstream_failed│
                    └───────────┘  └─────────────┘  └───────────────┘
                          │
                          ▼
                    ┌───────────────────┐
                    │  WAITING_DELAY    │
                    │ (countdown timer) │
                    └───────────────────┘
                          │
                          ▼
                    ┌───────────────────┐
                    │      RUNNING      │
                    │ (task executing)  │
                    └───────────────────┘
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
          ┌─────────┐ ┌─────────┐ ┌──────────────────┐
          │ SUCCESS │ │ FAILED  │ │WAITING_DECISION   │
          └─────────┘ └─────────┘ │(interactive Slack)│
                                  └──────────────────┘
```

**Terminal statuses:** success, failed, skipped, aborted, upstream_failed

**Non-terminal:** stopped (can be restarted), pending (SFN redrive)

---

## UI Architecture (React Console)

The web console follows a clear data separation pattern:

```
┌─────────────────────────────────────────────────────────┐
│  Browser                                                 │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │ React Query 5│  │  Zustand 5   │  │  URL State    │ │
│  │ Server State │  │  UI State    │  │  (useStoreInit)│ │
│  │              │  │              │  │               │ │
│  │ • Pipelines  │  │ • Navigation │  │ ?pipeline=x   │ │
│  │ • Tasks      │◄─┤ • Modals     │◄─┤ &date=...     │ │
│  │ • Assets     │  │ • Filters    │  │ &view=dag     │ │
│  │ • Metrics    │  │ • Theme      │  │               │ │
│  └──────┬───────┘  └──────────────┘  └───────────────┘ │
│         │                                                │
│  Next.js API Proxy (/api/[...path])                     │
└─────────┼───────────────────────────────────────────────┘
          │
          ▼
  API Gateway → Lambda (console_api) → DynamoDB
```

**Key pattern (ADR #22):** All UI reads come from DynamoDB. Step Functions API is used only for write/control operations (start, stop, send task token) and reconciliation of running executions.

**Store subscriptions:** All components use `useShallow` selectors from Zustand to subscribe to only the fields they need, preventing cascade re-renders when unrelated state changes.

**Bundle optimization:** `TaskDetailModal` and `BackfillModal` are lazy-loaded via `React.lazy()` — they're only fetched when first opened (~50KB deferred from initial load).

**Testing:** 536+ tests across 35 files using Vitest + Testing Library. ReactFlow components are tested via mock renderers that output DOM-testable `<div>` elements.

See [UI Operations Guide](../operations/UI.md) for component details, accessibility, and testing patterns.

---

## Debugging Guide

> For detailed solutions with commands, see [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md).

### Task stuck in "waiting"

1. Check **Dependencies** tab in task modal - are upstreams complete?
2. Check **Subscriptions** in DynamoDB - is subscription created?
3. Check **notify_dependents_helper** execution - did it run?
4. Check **wait_token** - is it valid?

### Task stuck in "waiting_delay"

1. Check `wait_delay_until_ms` - when should countdown end?
2. Check server time sync - is there clock skew?
3. Check wrapper SFN execution - is Wait state active?

### Pipeline doesn't start

1. Check **EventBridge rule** - is schedule correct?
2. Check **orchestrator SFN** - any failed executions?
3. Check **pipeline registry** - is pipeline registered?

### Task fails immediately

1. Check **task_arn** - is it correct?
2. Check **IAM roles** - does wrapper have permission to invoke?
3. Check **task SFN/Lambda** logs - what's the error?

---

## Runbooks

### Stuck waitForTaskToken

```bash
# Find task with stuck token
aws dynamodb query \
  --table-name {namespace}-{stage}-polyris-pipeline-tokens \
  --index-name status-index \
  --key-condition-expression "#s = :status" \
  --expression-attribute-names '{"#s": "status"}' \
  --expression-attribute-values '{":status": {"S": "waiting"}}'

# Check if subscription exists
aws dynamodb get-item \
  --table-name {namespace}-{stage}-polyris-dependency-subscriptions \
  --key '{"dependency_key": {"S": "task_a-abc123"}, "subscriber_name": {"S": "task_b"}}'

# Manually send token (emergency)
aws stepfunctions send-task-success \
  --task-token "aqc..." \
  --task-output '{"status": "success"}'
```

### Restart failed pipeline

```bash
# Via Console UI: Pipelines → Select pipeline → Run button

# Via API:
curl -X POST https://api.example.com/api/pipeline-run?name=my-pipeline \
  -H "Content-Type: application/json" \
  -d '{"input": {"current_date": "2024-01-15"}}'
```

### Stop runaway execution

```bash
# Via Console UI: Stop button (appears when tasks are active)

# Via API:
curl -X POST https://api.example.com/api/execution-stop?id={arn}
```

---

## Glossary

| Term | Definition |
|------|------------|
| **Orchestrator** | Main pipeline SFN that launches all tasks in parallel |
| **Wrapper** | SFN that handles dependency waiting + task execution |
| **Helper** | Utility SFN (notify_dependents, failure_handler, etc.) |
| **Token** | waitForTaskToken callback identifier |
| **Subscription** | Record linking dependent task to upstream task |
| **Trigger Rule** | Condition for when task should run based on upstream statuses |
| **Asset** | Data artifact produced by tasks (for data-driven orchestration) |
