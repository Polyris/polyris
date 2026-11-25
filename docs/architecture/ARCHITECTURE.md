# polyris Architecture

## Contents

- [High-Level Overview](#high-level-overview) — diagram
- [Design Principles](#design-principles)
- [Data Flow: "What Calls What"](#data-flow-what-calls-what)
- [DynamoDB Tables](#dynamodb-tables)
- [Trigger Rules](#trigger-rules)
- [Task Statuses](#task-statuses)
- [State Machines and Lambdas](#state-machines-and-lambdas)
- [Cost Model](#cost-model)
- [Task Status Lifecycle](#task-status-lifecycle)
- [UI Architecture (React Console)](#ui-architecture-react-console)
- [Debugging Guide](#debugging-guide)
- [Glossary](#glossary)

## High-Level Overview

![The polyris engine and the path of a run: Start_Run writes the run's rows, the tasks table's stream wakes the dispatcher, which starts run_task; run_task records its success, and once every row is final the dispatcher answers the run's token.](engine.svg)

1. A schedule, Run in the console, or an asset's fact starts the pipeline's state machine.
2. After the run's registration, its `Start_Run` state invokes the platform's start-run Lambda with a task token. The Lambda writes the run row, the run's input and one row per task, `waiting`, and returns within seconds. The state waits for its token, and a waiting state costs nothing.
3. The tasks table's stream wakes the dispatcher.
4. The dispatcher passes over the run's rows, writes `deps_ready` in a transaction and starts `run_task`.
5. `run_task` marks the task `running` and runs the work in your resources.
6. Its success is one transaction: the status, `output#` and the facts of the task's assets. The row's change wakes the dispatcher again, which starts the next tasks.
7. Once every row is final, the dispatcher answers the run's token, and the pipeline's execution ends.

The sweeper checks what is due every 5 minutes, so a lost wake-up delays a task instead of losing it. `asset-fanout` makes a narrow pass over the tasks that wait for a new fact, and starts the pipelines scheduled on it. `polyris-deploy` writes a pipeline's task specs to `pipeline-specs` and deploys its stack: its state machine, its schedule and its log group. `polyris-up` installs and upgrades the platform. [State Machines and Lambdas](#state-machines-and-lambdas) and [DynamoDB Tables](#dynamodb-tables) list every piece.

---

## Design Principles

These are the load-bearing decisions the rest of the architecture follows from.
(Each is expanded in [DESIGN_DECISIONS.md](../reference/DESIGN_DECISIONS.md).)

1. **Nothing to run — AWS runs it.** polyris has no scheduler, no worker pool
   and no orchestrator to operate. A pipeline compiles to a Step Functions state
   machine whose `Start_Run`, after the run's registration, writes its tasks as rows;
   EventBridge Scheduler triggers it. Step Functions runs each task's work
   (`run_task`), and a dispatcher Lambda, woken by the tasks table's stream,
   decides from the rows which task starts next. Between runs the system scales
   to zero ([README § Cost](../../README.md#cost)).

2. **Nothing hidden — decisions from rows and facts.** A task's state is its
   row, so the console shows why each task waits. The dispatcher decides a task
   from its upstreams' rows and the asset facts, read consistently. The streams
   only wake it: a lost wake-up delays a task, never loses it, because the
   sweeper checks every run on a schedule. Each task's work is a Step Functions
   execution with an inspectable history.

3. **Asset-centric, not just task-aware.** Tasks declare the data assets they
   produce (`outlets`). An asset links the pipelines that produce and consume it,
   by partition, and those links — not just the task DAG — drive cross-pipeline triggering.

4. **Data passes through a canonical output store.** Tasks do not hand data to
   each other in-process. Each task writes its result to a stable key
   (`output#{pipeline}#{task}#{date}`) in DynamoDB; downstream tasks read their
   upstreams' outputs from there (`Read_Upstream_Outputs` → `Prepare_Task_Input`
   in the run_task helper). A skipped task's prior output stays readable, and
   "is this upstream ready for this partition?" is a single point lookup.
   Outputs carry a TTL, so a run for a very old date may find an ancestor's
   output expired.

5. **Single source of truth, generated.** Enum families and shared constants are
   defined once in `polyris/constants.py` and code-generated into every Lambda
   (`constants_generated.py`) and the UI (`ui/src/generated/enums.ts`), with CI
   drift gates. Hand-maintained copies of the same vocabulary are treated as
   bugs (see DESIGN_DECISIONS #72/#83/#93/#94).

6. **No silent failures.** A task that fails after its retries waits for an
   operator's decision, with the cause on its row. A failure of the platform
   itself is retried until it succeeds, fails the task it concerns with the real
   cause, or shows in the console's bell or System status (ADR #130 §9).

---

## Data Flow: "What Calls What"

Use this section to debug "why didn't my task start?" — trace the chain step by step. [ADR #137](../reference/adr-137-tasks-as-rows.md) is the full design.

### Flow 1: A scheduled run

```
EventBridge schedule (cron or rate)
     │
     ▼
The pipeline's state machine
     │  after the registration: Start_Run (lambda:invoke.waitForTaskToken)
     ▼
start-run Lambda
     ├─ reads the task specs of the run's spec version ──► pipeline-specs
     ├─ writes the run row _run_{execution} with the run's token, the run's input,
     │  one row per task (waiting) and the asset: pointers of wait_for tasks
     └─ marks the run active
     │
     ▼  the tasks table's stream
reconcile Lambda — the dispatcher: one pass over the run's rows
     ├─ decides each waiting row from its upstreams' rows and the asset facts,
     │  read consistently, by its trigger rule (ADR #138)
     ├─ ready ──────► writes deps_ready, then starts run_task under a derived name
     ├─ blocked ────► upstream_failed, or skipped (skip_origin = rule)
     └─ every row final ─► answers the run's token: the pipeline execution ends
```

Direct steps (`dynamodb`, `sns`, `sqs`, `s3`, `http` and the like) stay branches of the pipeline's state machine, beside `Start_Run`.

### Flow 2: A task's work (run_task)

```
run_task — one execution per attempt of a task's work
     ├─ Claim ─────────────────► the row: deps_ready → running, under its generation
     ├─ Read_Upstream_Outputs ─► output# of its upstreams (data passing)
     ├─ Save_Input_Record ─────► input#: what the console shows as the task's input
     ├─ the work (one of): Lambda, Step Functions (.sync:2), Glue, ECS,
     │    Batch, Athena, EMR (.sync), with the task's retries in place
     ├─ success ─► Record_Success: one transaction of the status, output#
     │             and a fact per outlet (asset-events)
     └─ failure after its retries ─► waiting_decision, with the cause and the
                                     decision's deadline
```

### Flow 3: How task B learns that task A finished

```
run_task writes A's final status (or an operator's action does)
     │
     ▼  the tasks table's stream, filtered to the changes that wake
reconcile Lambda — a pass over A's run
     └─ decides B from A's row and B's other upstreams (consistent reads)

sweeper — every 5 minutes, a full pass over each due run
     └─ so a lost wake-up only delays a task: a run is checked again
        10, 20, then every 30 minutes while it has rows to watch
```

An operator's action and a run's request (a Restart's pointers, a reset of dependants, a Stop of the run) are writes too, and wake the dispatcher the same way.

### Flow 4: Asset-Based Cross-Pipeline Triggers

```
Producer task succeeds with outlets: [{name: "inventory"}]
     │
     ▼
run_task's success transaction ──► asset-events [Put, never overwrites]
     │    one fact per outlet: partition_key (the run's date), partition_date,
     │    granularity, event_time
     ▼  the asset-events stream (INSERT)
asset-fanout Lambda
     ├─ PULL (wait_for): the asset:inventory pointers, read consistently
     │    └─ one narrow pass of the dispatcher over each run with a waiting
     │       task, which decides from the facts of the run's partition
     └─ PUSH (schedule=[asset]): asset-subscriptions "inventory"
          └─ for each consumer pipeline (never the producer itself):
               ├─ evaluate its whole schedule for the fact's date, as pull does
               └─ if satisfied → StartExecution under run_name(...):
                    AND → one run per date while its history is kept; OR → one per event
                    ExecutionAlreadyExists → already started; transient error →
                    push retry row for the sweeper; anything else → warning
```

A fact's partition is the partition of the run that produced it; Mark Successful records it for the marked run's date. A task is woken only when a fact serves its own run's date (hourly and daily: that date; weekly and monthly: its period or the one before), so a run for an old date never starts on today's fact.

The waiting task's side (a task with `wait_for`):

```
Run starts (Start_Run)
        ├── Write the task row, waiting
        └── Write one pointer per asset: "asset:<name>" → this run's task
                │
                ▼
Dispatcher (woken when the task's upstreams finish, or by a fact)
        ├── Read asset-events for this run's partition, consistently
        │   (and `.within(...)` freshness, `.consecutive(...)` days)
        ├── If ready → deps_ready → start the task's run_task
        └── If not   → the row waits; the pointer brings the next fact
```

The tables involved, all created by the platform stack:

| Table | Purpose |
|-------|---------|
| `asset-events` | Every fact (PK: `asset_name`, SK: `event_time`), with the partition it belongs to; the `asset-partition-index` finds facts by partition date |
| `dep-subscriptions` | Per-run pointers: `asset:<name>` → each waiting task of a run whose `wait_for` names the asset |
| `asset-subscriptions` | Pipelines scheduled on an asset (PK: `asset_name`, SK: `pipeline_name`) |

### Flow 5: Failure → decision

```
The task's work fails after its retries (run_task)
     ├─ the row ─► waiting_decision, with the cause and the decision's deadline
     └─ the operator decides in the console: Restart, Skip, Fail, Mark Successful
          └─ no decision by the deadline ─► the dispatcher fails the task
A final failure decides the dependants by their rules (ADR #138); the run ends once
every task is final, and fails if a task failed.
```

A failure of the platform itself is retried until it succeeds, fails the task it concerns with the real cause, or shows in the console's bell or System status (ADR #130 §9).

### Flow 6: Pipeline Registration on Deploy

**Primary path (polyris-deploy lifecycle):**
```
polyris-deploy
     │  writes each task's spec to pipeline-specs, reads it back and checks it
     │  CloudFormation creates/updates the pipeline StateMachine
     ▼
dynamodb.update_item (direct boto3 call from deploy.py)
     └─ pipeline-registry ──── SET registered_at, format_version
     ▼
sfn.start_execution(register_only=true)  ← fire-and-forget
     ├─ Register_Pipeline ──────► pipeline-registry [updateItem: sfn_arn, spec_id, task_count, ...]
     └─ Register_Asset_Subscriptions (if asset triggers)
          └─ Map ──────────────► asset-subscriptions [putItem]
```

**On destroy:**
```
polyris-destroy
     │
     ▼
CloudFormation delete_stack ◄── until the stack is gone: stop the pipeline's running runs
     └─ StateMachine deleted once none of its runs is left
```

### Quick Debug Reference

| Symptom | Where to look | Table / Service |
|---------|--------------|-----------------|
| Task stuck in `waiting` | The task in the console: why it waits, and since when. reconcile logs for its run | `pipeline-tokens` — the row's `reasons` and `attention_at` (its next check); `asset-events` — a fact for the run's date? |
| Task never becomes `deps_ready` | reconcile logs for the run; its timeline's decisions | `pipeline-tokens` — the upstreams' rows of the same run |
| Task `deps_ready` but not running | run_task's execution named by the row's `task_run_id` | `pipeline-tokens` — `start_failed_first`, `start_failures`; the bell |
| The **System** pill in the console header is amber | "no connection" or "API error": its panel, and the console API's logs for `/api/pipelines`. Else its panel, or `GET /api/health` with a valid token → `findings`; sweeper logs | `pipeline-tokens` — the `_engine_health` row |
| `wait_for` task not woken | asset_fanout and reconcile logs (`execution_name`) | `asset-events` — a fact for the run's partition? `dep-subscriptions` — `asset:{name}` pointer? |
| Asset trigger didn't fire | `/aws/lambda/{namespace}-{stage}-polyris-asset-fanout` logs | `asset-events` + `asset-subscriptions` |
| Pipeline not in UI after deploy | Check `polyris-deploy` output for registration step | `pipeline-registry` — is a row present for the pipeline name? |
| Wrong DAG in UI for old execution | Check `dag_source` in API response | `run`: the run row's `spec_id` in `pipeline-specs`; `snapshot` (a run started before 1.3): `dag_snapshot::{execution}` in `pipeline-tokens` |
| Wrong trigger_rule result | The task's timeline: the dispatcher's decision and its reasons | reconcile logs for the run |

---

## DynamoDB Tables

Quick reference — for full attribute definitions and access patterns see
[BACKEND.md § DynamoDB Tables](BACKEND.md#dynamodb-tables).

| Table | Purpose | PK | SK |
|-------|---------|----|----|
| pipeline-tokens | Task rows and run rows (`_run_{execution}`) | execution_name | - |
| dep-subscriptions | Per-run pointers to waiting tasks | dependency_key | subscriber |
| pipeline-registry | Pipeline metadata | pipeline_name | - |
| pipeline-specs | Task specs, by version and by text hash | pk | sk |
| asset-events | Asset history | asset_name | event_time |
| asset-subscriptions | Pipelines scheduled on an asset | asset_name | pipeline_name |
| task-events | Task history | task_run_id | event_time |
| api-tokens | Read by the console's sign-in check for a `plrs_` token; no route of this console creates one | token_id | - |

Each table's full name is `<namespace>-<stage>-polyris-<name>`.

---

## Trigger Rules

Nine rules (ADR #138); `docs/features/DSL.md#trigger-rules` describes each one with an example, and `polyris/coordination/trigger_rules.py` is the one implementation, checked against the ADR's truth table (`tests/fixtures/trigger_rules_truth_table.json`).

A rule reads the final outcomes of its task's direct upstreams: a failed task is final only after the decision on it (ADR #114), and `stopped` is a final failure. It runs its task as soon as its condition holds, and decides the rest only once every upstream is final. A rule that needs success or no failure (`all_success`, `one_success`, `none_failed`, `none_failed_min_one_success`) ends a task that a final failure blocks `upstream_failed`; any other unmet rule ends it `skipped`, with `skip_origin = rule`. Only `all_success` reads a skip's origin: a rule's skip cascades, an operator's or a run's passes. A name the engine does not know fails its task.

---

## Task Statuses

| Status | Description |
|--------|-------------|
| `waiting` | Waiting for its upstreams, its assets or its countdown (`wait_before`) |
| `waiting_paused` | Ready, but its run is paused: it starts after the Resume |
| `deps_ready` | Ready: the dispatcher starts its `run_task` |
| `running` | Its `run_task` runs the work |
| `waiting_decision` | Failed after its retries: waits for an operator's decision until its deadline |
| `success` | Completed successfully, or marked successful by an operator |
| `failed` | Failed for good: an operator's Fail, the decision's deadline, a limit, or a start refused |
| `skipped` | Skipped — by an operator (`skip_origin = manual`), by the run's `skip_tasks` (`run`), or by a trigger rule (`rule`) |
| `upstream_failed` | Blocked: a final failure of an upstream means its rule can never hold |
| `stopped` | Stopped by an operator or with its run |

Rows of runs started before 1.3 may also show `aborted` and `pending`, and `stale` for one that no engine finishes.

---

## State Machines and Lambdas

One-line inventory — for what each one does see
[BACKEND.md § State machines](BACKEND.md#state-machines).

| Component | Purpose |
|-----------|---------|
| `polyris-dep-run-task-helper` | `run_task`: one attempt of a task's work, its outcome and its facts in one write |
| `polyris-bulk-backfill` | Starts one pipeline execution per date of a range |
| `polyris-test-quick`, `-test-success`, `-test-failure` | Demo targets for `@task.sfn` |
| `start-run` | Starts a run's tasks, after its registration: writes the run row, its input, one row per task and the `asset:` pointers |
| `reconcile` | The dispatcher: decides each row, starts `run_task`, answers the run |
| `asset-fanout` | Takes new facts to the runs that wait for them, and starts the pipelines scheduled on them |
| `sweeper` | Every 5 minutes: a pass over each due run, push retries and catch-up, the engine's self-check |
| `console-api` | The console's REST API |
| `notify` | Reserved for alert delivery: `run_task` invokes it when a task fails or recovers, and no route of this console configures a channel for it |

---

## Cost Model

Full breakdown (per-run cost per AWS service, small-deployment total,
MWAA comparison) is in the root [README § Cost](../../README.md#cost).
The runtime primitives — Step Functions transitions, Lambda invocations,
DynamoDB read/write units, CloudWatch Logs GB — are all pay-per-request.
No always-on control plane, so idle cost is near zero.

---

## Task Status Lifecycle

```
                      ┌──────────────────────────────────────────┐
                      │                 WAITING                   │
                      │ (its upstreams, its assets, its countdown) │
                      └──────────────────────────────────────────┘
                           │               │                 │
                           ▼               ▼                 ▼
                    ┌────────────┐ ┌──────────────┐ ┌─────────────────────────┐
                    │ DEPS_READY │ │WAITING_PAUSED│ │ UPSTREAM_FAILED, SKIPPED│
                    │            │ │ (run paused) │ │ (decided by its rule)   │
                    └────────────┘ └──────────────┘ └─────────────────────────┘
                           │
                           ▼
                    ┌────────────┐
                    │  RUNNING   │
                    │ (run_task) │
                    └────────────┘
                           │
                  ┌────────┴──────────┐
                  ▼                   ▼
             ┌─────────┐      ┌──────────────────┐      ┌────────┐
             │ SUCCESS │      │ WAITING_DECISION │ ───► │ FAILED │
             └─────────┘      │ (after retries)  │      └────────┘
                              └──────────────────┘
```

A task of a paused run that becomes ready waits as `waiting_paused`: the dispatcher holds it, and starts it after the Resume. Tasks that already run go on. A pause lasts until it is lifted (ADR #137 §5).

**Final for the engine** (`SETTLED_STATUSES`): success, failed, skipped, upstream_failed and stopped. Any final task can be restarted; a Restart puts it back to `waiting`.

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
│  │ • Runs       │  │ • Filters    │  │ &view=dag     │ │
│  │ • Health     │  │ • Theme      │  │               │ │
│  └──────┬───────┘  └──────────────┘  └───────────────┘ │
│         │                                                │
│  fetch from the static export, straight to the API      │
└─────────┼───────────────────────────────────────────────┘
          │
          ▼
  API Gateway → Lambda (console_api) → DynamoDB
```

**Key pattern (ADR #22):** All UI reads come from DynamoDB. The console asks Step Functions nothing about a run (ADR #137 §4): it starts a run, and stops the `run_task` of a task an operator acts on.

**Store subscriptions:** All components use `useShallow` selectors from Zustand to subscribe to only the fields they need, preventing cascade re-renders when unrelated state changes.

**Bundle optimization:** `TaskDetailModal` is lazy-loaded via `React.lazy()` — it's only fetched when first opened (~50KB deferred from initial load).

**Testing:** Vitest + Testing Library. ReactFlow components are tested via mock renderers that output DOM-testable `<div>` elements.

Component details, accessibility and testing patterns: [DEVELOPMENT.md](../tools/DEVELOPMENT.md#console-ui).

---

## Debugging Guide

> For detailed solutions with commands, see [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md).

### Task stuck in "waiting"

1. Open the task in the console: it shows why it waits (its reasons) and since when.
2. Search the reconcile Lambda's logs for its run: each decision gives its reasons. The sweeper checks a run with waiting rows 10, 20, then every 30 minutes; if the console's System status says the periodic re-check is not running, that check is not happening.
3. For `wait_for`: `dep-subscriptions` holds one `asset:{name}` pointer per asset, with the task's `execution_name`; `asset-events` must hold a fact for the run's partition.

### Pipeline doesn't start

1. Check the pipeline's **EventBridge schedule** (`…-schedule`) — is it enabled, and is the expression right?
2. Check the pipeline's **state machine** — a run that fails at once can be a pipeline deployed for 1.2: the console marks it "redeploy needed".
3. Check the **pipeline registry** — is the pipeline registered?

### Task fails immediately

1. Check the task's **ARN or name** — is it right?
2. Check **IAM** — may the orchestration role, which runs `run_task`, start it? See [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md).
3. Check the task's own **logs** — what is the error?

---

## Glossary

The engine's own terms, for contributors. The terms a pipeline author meets (run, task, run's date, fact, trigger rule, decision and the like) are in [GLOSSARY.md](../../GLOSSARY.md).

| Term | Definition |
|------|------------|
| **Run** | One execution of a pipeline: a run row and one task row per task |
| **Run row** | `_run_{execution}`: the run's token, its stop mark, its pauses and its requests |
| **Task row** | A task's state in a run, keyed `{task}-{date}-{short}` |
| **Dispatcher** | The reconcile Lambda: decides rows from rows and facts, starts `run_task`, answers the run |
| **run_task** | The state machine that runs one attempt of a task's work |
| **Sweeper** | Every 5 minutes, checks each due run: a lost wake-up only delays a task |
| **Token** | The run's `waitForTaskToken`, which the dispatcher answers once every row is final |
| **Pointer** | An `asset:{name}` row of `dep-subscriptions`: a waiting task's link to an asset |
