# ADR #54 — Bulk-Backfill SFN — Standard, Map-based, fire-and-wait architecture (v0.78.0)

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §5 (1.3.0).** `bulk_backfill` waits for each run as long as the run lives, not 24 hours.

#### Context

ADR #51 calls for a `polyris-bulk-backfill` SFN that orchestrates one
"slice of work" per partition for a backfill. This ADR specifies its
internal structure: state machine layout, JSONata expressions, IAM
permissions, error handling, cancel mechanism, and operational
characteristics.

#### Decision

**Type:** Standard SFN (not Express).
**Pattern:** Single Map state with synchronous child invocations
(`.sync` integration pattern).
**State count:** ~12 states (Initialize, Map iterator with 5 sub-states,
Finalize, plus error paths).
**Naming:** `polyris-bulk-backfill` (one SFN, namespace-scoped via SAM
template substitution).
**Execution naming:** `{backfill_id}` (e.g., `bf-a1b2c3d4`); matches the
DDB Backfill record's `execution_name`/`backfill_id` field for 1:1
correspondence.

##### Why Standard SFN, not Express

Question #2 (Standard).

| Factor | Standard | Express |
|---|---|---|
| Per-iteration cost | ~$0.0001 | ~$0.000001 |
| Total cost for 90-partition backfill | $0.009 | $0.0001 |
| Max execution time | 1 year | 5 minutes |
| `.sync` integration support | Native | Limited |
| AWS Console visibility | Full history | 24h retention |
| State logging | CloudWatch Logs | EXPRESS log only |

Express is 100× cheaper but caps at 5 minutes. For our use case
(backfills with `.sync` waiting on child pipelines that may take hours
each), Express would require a separate watcher SFN to poll completion,
adding architectural complexity. The 1¢ savings per backfill is not
worth the complexity. Standard chosen.

##### State machine layout

```
Initialize (Task: DynamoDB.PutItem)
   ↓ writes backfill record with status=running, dag_hash from pipeline_registry
   
LoadPipelineDagHash (Task: DynamoDB.GetItem)
   ↓ fetches current pipeline registry record to capture dag_hash
   
UpdateBackfillWithHash (Task: DynamoDB.UpdateItem)
   ↓ writes pipeline_dag_hash to backfill record
   
Map (Iterator on partition_keys, MaxConcurrency=options.max_parallel)
   ↓ for each partition:
   
   CheckBackfillCanceled (Task: DynamoDB.GetItem)
     ↓ reads backfill record
   
   IsCanceledChoice (Choice)
     ├─ if status==canceled → SkipPartition (Succeed iteration)
     └─ continue →
   
   SkipIfDone (Choice)  [only if skip_completed=true]
     ├─ if all target tasks succeeded → SkipPartition + IncrementSkipped
     └─ continue →
   
   StartChildSFN (Task: StepFunctions.StartExecution.sync)
     ↓ starts child pipeline SFN, waits for completion
     Catch: ThrottlingException → BackoffRetry (Wait 2s → retry x3)
     Catch: All other → IncrementFailed
   
   ChildSucceededChoice (Choice)
     ├─ if status==SUCCEEDED → IncrementCompleted
     └─ else → IncrementFailed
   
   IncrementCompleted | IncrementFailed | IncrementSkipped 
     (Task: DynamoDB.UpdateItem with ADD expression)
   
End of Map iteration.

Finalize (Task: DynamoDB.UpdateItem)
   ↓ reads counters, sets final status based on completed/failed/skipped:
     - if failed > 0 and completed > 0: status = "partial"
     - if failed > 0 and completed == 0: status = "failed"
     - if canceled at any point: status = "canceled"
     - else: status = "completed"
   ↓ writes finished_at timestamp
```

Total states: 3 init + 6 iterator + 1 finalize = 10 unique states.

##### Map state configuration

```json
"BulkBackfillMap": {
  "Type": "Map",
  "ItemsPath": "$.partition_keys",
  "MaxConcurrencyPath": "$.options.max_parallel",
  "ItemSelector": {
    "backfill_id.$": "$.backfill_id",
    "partition_key.$": "$$.Map.Item.Value",
    "target_pipeline.$": "$.target_pipeline",
    "task_subset.$": "$.task_subset",
    "options.$": "$.options",
    "cascade.$": "$.cascade",
    "variables.$": "$.options.variables"
  },
  "ItemProcessor": { ... },
  "ResultSelector": {
    "iterations_complete.$": "$$.Map.Length"
  },
  "Next": "Finalize"
}
```

`MaxConcurrencyPath` is dynamic — reads from input, allowing user to set
max_parallel per backfill (1-10 range, validated by handler).

##### Child SFN invocation (StartExecution.sync)

```json
"StartChildSFN": {
  "Type": "Task",
  "Resource": "arn:aws:states:::states:startExecution.sync:2",
  "Parameters": {
    "StateMachineArn.$": "$.target_pipeline_sfn_arn",
    "Name.$": "States.Format('{}-{}', $.target_pipeline_name, $.partition_key)",
    "Input": {
      "backfill_id.$": "$.backfill_id",
      "partition_key.$": "$.partition_key",
      "current_date.$": "$.partition_key",   // legacy compat for run_task
      "is_backfill": true,
      "skip_tasks.$": "$.skip_tasks_computed",
      "variables.$": "$.variables"
    }
  },
  "TimeoutSeconds": 86400,
  "Retry": [
    {
      "ErrorEquals": ["States.TaskFailed"],
      "ErrorRegex": ".*ThrottlingException.*",
      "IntervalSeconds": 2,
      "MaxAttempts": 3,
      "BackoffRate": 2.0
    }
  ],
  "Catch": [
    {
      "ErrorEquals": ["States.ALL"],
      "Next": "IncrementFailed",
      "ResultPath": "$.error"
    }
  ],
  "Next": "ChildSucceededChoice"
}
```

`startExecution.sync:2` waits for child to complete and returns
`Output.Status` field reflecting child's final state (SUCCEEDED / FAILED).
`TimeoutSeconds: 86400` (24h) covers worst-case long-running pipelines.

For non-daily granularity, `current_date` in the input is set to the
**daily anchor** of the partition (e.g., weekly partition `2024-W03` →
Monday of that week, `2024-01-15`). This preserves backward compatibility
with `run_task` SFN template's `Prepare_Task_Input` state which expects
daily YYYY-MM-DD. The `partition_key` is the canonical identifier for
tooling/UI/DDB; `current_date` is the runtime anchor for JSONata date math
inside tasks. ADR #58 specifies the translation.

##### Cancel mechanism

Question #1 (A) — cancel races up to N partitions (where N = max_parallel)
through despite cancel signal. Acceptable. Implementation:

```json
"CheckBackfillCanceled": {
  "Type": "Task",
  "Resource": "arn:aws:states:::dynamodb:getItem",
  "Parameters": {
    "TableName": "${PipelineTokensTable}",
    "Key": {
      "execution_name": {"S.$": "$.backfill_id"},
      "pipeline_name": {"S": "_polyris_bulk_backfill"}
    },
    "ConsistentRead": true
  },
  "ResultSelector": {
    "status.$": "$.Item.status.S"
  },
  "ResultPath": "$.backfill_status",
  "Next": "IsCanceledChoice"
}
```

The `ConsistentRead: true` flag ensures cancel signal is seen within
the AWS replication window (single-digit milliseconds). Map iterations
that have already started their `StartChildSFN` cannot be canceled (they
complete naturally); only iterations that haven't reached the check yet
will skip.

Cancel handler in `routes/backfill.py::cancel_backfill`:

```python
def cancel_backfill(backfill_id, event):
    repo.update_item(
        Key={execution_name=backfill_id, pipeline_name=SENTINEL},
        UpdateExpression="SET #status = :canceled",
        ConditionExpression="#status IN (:pending, :running)",
        ExpressionAttributeValues={
            ":canceled": "canceled",
            ":pending": "pending",
            ":running": "running",
        },
        ExpressionAttributeNames={"#status": "status"}
    )
    return {"backfill_id": backfill_id, "status": "canceled"}
```

No `sfn.stop_execution()` call. The bulk-backfill SFN continues running
through its remaining states (Finalize will set status to "canceled" if
that's already in DDB), but new partitions stop starting. Already-started
child pipelines are not interrupted.

Why not `sfn.stop_execution()`? It would mark the bulk-backfill execution
as ABORTED, but in-flight child SFNs would still complete (no cascading
stop), DDB record would not reflect the cancel cleanly, and the Map's
counter updates would be inconsistent. Cooperative cancel via DDB flag
is cleaner.

##### IAM permissions

The bulk-backfill SFN's execution role needs:

1. **DynamoDB on PipelineTokensTable** — `PutItem`, `GetItem`, `UpdateItem`
   for backfill record + child execution rows (for skip_completed check).
2. **DynamoDB on PipelineRegistryTable** — `GetItem` for fetching
   pipeline DAG hash and target_pipeline_sfn_arn.
3. **States:StartExecution.sync** on **all pipeline SFNs in the same
   namespace** — wildcard `arn:aws:states:*:*:stateMachine:{Namespace}-{Stage}-*`.
4. **CloudWatch Logs** — write permissions for SFN logging.

The OrchestrationRole (existing role used by other helper SFNs) already
has scopes 1, 3, 4 via wildcards. Scope 2 needs explicit addition.

##### Error handling

| Failure mode | Behavior |
|---|---|
| DDB Initialize fails | Bulk-backfill execution fails immediately; no backfill record exists → user sees 500 from API; retry by user |
| DDB GetItem on cancel check fails | Iteration retries 3x with backoff; if persistent, iteration fails-fast with logged error; counter increments failed |
| StartChildSFN throttled | Retried 3x with exponential backoff (2s, 4s, 8s); if still throttled, iteration counts as failed; user retries via UI |
| Child SFN fails | Iteration's `Catch` block sets ResultPath, increments failed_count, continues to next partition |
| Child SFN times out (24h) | Treated as failure |
| Finalize fails | Bulk-backfill state machine fails; backfill record stuck in "running" status — handled by reconciliation cron (out of scope, would be ADR-future) |

##### Observability

- **AWS Console:** full execution history visible per backfill. Map
  iterations show as fan-out diagram.
- **CloudWatch Logs:** all state transitions logged. Searchable by
  `backfill_id`.
- **CloudWatch Metrics:** `ExecutionsSucceeded`, `ExecutionsFailed`,
  `ExecutionTime` per state machine — standard SFN metrics.
- **X-Ray tracing:** enabled (matches existing helper SFNs); shows
  bulk-backfill → child pipeline call graph.
- **API Detail page:** mirrors above in friendly UI, updated via
  3-second polling against `/api/backfills/{id}`.

##### Performance characteristics

Per Map iteration (excluding child SFN runtime):
- CheckBackfillCanceled DDB read: ~5ms
- SkipIfDone DDB query: ~10ms (if enabled)
- StartChildSFN: ~20ms to start (then synchronous wait for child)
- IncrementCounter DDB write: ~10ms

Iteration overhead: ~45ms. For 90-partition backfill at max_parallel=5,
effective serialization is 90/5 × 45ms = ~800ms total overhead beyond
child pipeline runtime.

Map MaxConcurrency=10 (hard upper) prevents AWS-side throttling: SFN
StartExecution has a 1000/s account-wide rate limit and 200 burst. With
10 concurrent + ~45ms iteration overhead, peak rate is ~220/s — safely
under burst limit.

##### Limits enforcement

`routes/backfill.py::start_backfill` enforces limits before invoking SFN:

- `len(partition_keys) > PARTITION_HARD_LIMIT` → 400 `range_too_large`
- `len(partition_keys) > PARTITION_SOFT_LIMIT` → append warning in response, allow
- `options.max_parallel not in 1..MAX_PARALLEL` → 400 invalid input

SFN itself does not re-validate (trust handler). Defense-in-depth at
schema level: SAM template's `BulkBackfillSfn` resource has
`Definition.Parameters` validated by cfn-lint.

##### Why one Map state, not multiple

Considered alternative: split partitions into batches, one Map per batch
(Standard SFN supports up to 25 distributed Map iterations per execution).
Rejected. One Map is simpler, AWS handles concurrency natively via
MaxConcurrency, and we never expect to exceed 1000 iterations (hard
limit).

##### Why fire-and-wait, not fire-and-forget

Considered alternative: bulk-backfill starts all child SFNs via async
`StartExecution` (no `.sync`), then polls completion via separate Express
SFN watcher. Rejected because:

1. **Complexity.** Two SFNs to maintain, two failure modes.
2. **Counter accuracy.** Fire-and-forget requires the watcher to track
   completion and update counters; race conditions on DDB writes possible.
3. **Cost saving negligible.** ~$0.001 per backfill not worth the
   architectural cost.

`.sync` pattern keeps state machine self-contained and intuitive in
AWS Console.

##### Migration

Since no production exists (ADR #51), bulk-backfill SFN is a new resource
added via SAM template diff. No existing infrastructure changes.

Smoke test plan:
1. Deploy SAM update with new resource to test environment
2. Run `polyris backfill pipeline acme/daily_orders --partitions
   2026-05-01:2026-05-03` (3 partitions)
3. Verify in AWS Console:
   - bulk-backfill execution visible, Map shows 3 iterations
   - 3 child pipeline executions created
   - DDB Backfill record updates progressively
4. Cancel mid-run, verify pending iterations skip
5. Trigger backfill with one task forced to fail, verify retry-failed
   creates new backfill with only failed partition

##### Open questions deferred

- Dead Letter Queue for bulk-backfill failures — out of scope; CloudWatch
  Alarms on `ExecutionsFailed` metric is sufficient observability for v0.78
- Backfill of backfill (running backfill that re-runs failed of another)
  — supported via `parent_backfill_id` field, no special SFN handling
  needed; ADR #51 covers this in retry-failed flow

#### Cost

Per backfill (covered in ADR #53):
- 90-partition backfill: ~$0.009 of bulk-backfill SFN transitions
- Child pipelines: separate cost, would happen for any execution path

Monthly fixed:
- Backfill-id-index GSI: $0.15/month (sparse, ~10K rows including legacy)
- bulk-backfill log group: ~$0.05/month at expected log volume
- Total: ~$0.20/month flat baseline
