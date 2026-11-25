---
area: engine
accepted: 2026-05-20
---
# ADR #56 — Backfill Status Model — six states, aggregation rules, mapping to execution statuses

#### Context

ADR #51 introduces Backfill as a unit of work with its own lifecycle.
Backfill status is not the same as execution status — a Backfill
aggregates N executions and has its own state machine reflecting bulk
operation semantics (cancel, partial completion). This ADR specifies
the status enum, transitions, computation rules, and how it relates to
the existing execution status enum.

#### Decision

##### Backfill status enum (6 values)

```python
class BackfillStatus(str, Enum):
    PENDING   = "pending"     # Backfill record created, bulk-backfill SFN not yet started Map
    RUNNING   = "running"     # Map iterating; at least one partition in flight
    COMPLETED = "completed"   # All partitions succeeded
    FAILED    = "failed"      # All attempted partitions failed (zero succeeded)
    PARTIAL   = "partial"     # Some succeeded, some failed (mixed outcome)
    CANCELED  = "canceled"    # User-initiated cancel via /api/backfills/{id}/cancel
```

##### Status transitions

```
[Initial: backfill record created in /api/backfill handler]
                 ↓
              PENDING
                 ↓ (bulk-backfill SFN Initialize state writes status=running)
              RUNNING ──────────── (cancel signal) ──────────→ CANCELED
                 ↓
              [Map iterations complete, Finalize state runs]
                 ↓ (aggregation rules below)
                 ├── COMPLETED  (completed_partitions > 0 AND failed_partitions == 0)
                 ├── FAILED     (completed_partitions == 0 AND failed_partitions > 0)
                 └── PARTIAL    (completed_partitions > 0 AND failed_partitions > 0)
```

Once a terminal status is set (COMPLETED, FAILED, PARTIAL, CANCELED), no
further transitions occur. The DDB record is immutable except for TTL.

##### Aggregation rule (in Finalize state)

```python
def compute_final_status(record):
    if record["status"] == "canceled":
        return "canceled"  # cancel preserved even if some partitions ran
    
    completed = record.get("completed_partitions", 0)
    failed = record.get("failed_partitions", 0)
    skipped = record.get("skipped_partitions", 0)
    total = record["total_partitions"]
    
    # Skipped don't count as failed; they were intentionally skipped via skip_completed
    if completed + failed + skipped != total:
        # In-flight state — shouldn't reach Finalize, but defensive default
        return "failed"
    
    if failed == 0:
        return "completed"
    elif completed == 0:
        return "failed"
    else:
        return "partial"
```

##### Cancel preservation

User cancels mid-run. By that point, e.g., 3 partitions succeeded and 2
failed. What's the final status?

**Decision: CANCELED preserves over outcome.** The cancel signal is what
the user explicitly requested; the per-partition outcomes are
informational. UI shows:

```
Status: canceled
  Completed: 3
  Failed: 2
  Pending (canceled): 5
```

This avoids ambiguity. If a backfill is canceled with 3/10 done, calling
it "partial" suggests the cancel was incidental — but cancel was the
user's deliberate action. Better to be honest: canceled, with details.

##### Mapping to execution status

Each Backfill child is a separate pipeline execution with its own status.
Execution status enum (existing, unchanged):

```python
class ExecutionStatus(str, Enum):
    PENDING        = "pending"          # SFN started, dependency_wrapper init
    RUNNING        = "running"          # In-flight tasks
    SUCCESS        = "success"          # All tasks succeeded
    FAILED         = "failed"           # At least one task failed (no skip recovery)
    UPSTREAM_FAILED = "upstream_failed" # Skipped due to upstream task failure
    SKIPPED        = "skipped"          # Skipped via skip_tasks parameter
    STOPPED        = "stopped"          # User stop_execution call
    ABORTED        = "aborted"          # SFN ABORTED (rare; race conditions)
```

Mapping for Backfill aggregation purposes (in bulk-backfill SFN
ChildSucceededChoice):

```
Execution status → Counter increment
─────────────────────────────────────
SUCCESS          → completed_partitions++
FAILED           → failed_partitions++
UPSTREAM_FAILED  → failed_partitions++
STOPPED          → failed_partitions++  (counts as failed for aggregation)
ABORTED          → failed_partitions++  (defensive)
SKIPPED          → completed_partitions++  (all tasks intentionally skipped == "did nothing successfully")
PENDING/RUNNING  → Never observed; .sync waits for terminal status
```

Aggregation question: a SKIPPED execution (all tasks user-skipped via
skip_tasks parameter) counts as completed. This matches user intent: "run
nothing for this partition" should not be a failure.

##### Partial completion handling

When Backfill ends in PARTIAL state:
- UI shows Backfill record with status=partial, prominent retry-failed
  button
- `/api/backfills/{id}/retry-failed` creates new Backfill with
  `partitions.keys = [failed list]` and `parent_backfill_id = original`
- Original Backfill keeps status=partial; new Backfill linked via parent
- User can chain retries; each retry creates new Backfill, all linked

##### Cancel handling — what is "still running" at cancel time

When user POSTs /api/backfills/{id}/cancel:
1. Handler updates DDB record status = canceled
2. Bulk-backfill SFN's Map iterations check status in
   CheckBackfillCanceled at each iteration start
3. Iterations that have already entered StartChildSFN cannot be
   interrupted — they wait for child completion (could take hours)
4. New iterations don't start (Map respects MaxConcurrency from this
   point, but iterations are short-circuited by IsCanceledChoice)
5. Finalize runs at the end; reads counters that include any partitions
   that completed/failed despite cancel; sets final status=canceled
   (preserving user intent)

UI during this transitional state shows:

```
Status: canceling...
  Completed: 3
  Failed: 1
  In progress (will finish before cancel takes effect): 1
  Skipped (canceled before start): 5
```

After bulk-backfill Finalize:

```
Status: canceled
  Completed: 4 of 10
  Failed: 1 of 10
  Skipped: 5 of 10
```

The "canceling..." intermediate state is computed client-side in UI as:
`(record.status == "running" AND user_requested_cancel == true)`. Not
stored in DDB.

##### Why no IN_PROGRESS state separate from RUNNING

Considered but rejected. Two states would require a separate transition
from PENDING → IN_PROGRESS → RUNNING; in practice the bulk-backfill SFN
Initialize writes status=running atomically before Map starts. The
distinction adds complexity without value.

##### Status comparison with other systems

| System | Bulk states |
|---|---|
| **Managed orchestrators** | success / failed / running (only 3) |
| **Dagster Backfill** | requested / in_progress / completed / failed / canceled (5) |
| **polyris** | pending / running / completed / failed / partial / canceled (6) |

polyris adds `partial` because at our scale (90-day backfills), partial
success is the **common** outcome, not exceptional. Conflating it with
"failed" is misleading; with "completed" hides the failures. Distinct
state captures the reality and drives the retry-failed flow naturally.

##### UI status badges

CSS classes for each status (`_runs.css` BEM):

```
.bf-status--pending   { background: gray-200; }
.bf-status--running   { background: blue-100; }
.bf-status--completed { background: green-100; }
.bf-status--failed    { background: red-100; }
.bf-status--partial   { background: yellow-100; }
.bf-status--canceled  { background: gray-300; }
```

Icons (lucide-react):
- pending: `Clock`
- running: `Activity` (spinning animation)
- completed: `CheckCircle2`
- failed: `XCircle`
- partial: `AlertTriangle`
- canceled: `Ban`

##### API responses with status

`/api/backfills/{id}` response includes:

```json
{
  "backfill_id": "bf-a1b2c3d4",
  "status": "partial",
  "total_partitions": 10,
  "completed_partitions": 7,
  "failed_partitions": 2,
  "skipped_partitions": 1,
  ...
}
```

`/api/backfills` list response includes status per item with same shape.

##### Reconciliation

Edge case: bulk-backfill SFN crashes or is manually stopped via AWS
Console. DDB record remains in `running` state indefinitely.

**Reconciliation:** out of scope for v0.78. The 30-day TTL eventually
cleans up stale records. Alternative (future): a daily reconciliation
Lambda that scans for `status=running` records whose SFN execution is
ABORTED/FAILED in AWS, and updates them to `failed`. Not built now.

#### Migration

No data migration needed (no production). New `status` field on Backfill
records (sparse on legacy execution rows — they don't have it). All
backfill code paths consume the enum starting v0.78.

#### Testing

Pinned by `tests/sdk/test_backfill_status.py`.

#### Cost

Zero infrastructure. Pure model + enum. Counter updates already covered
in ADR #54 bulk-backfill SFN's `IncrementCompleted` / `IncrementFailed`
/ `IncrementSkipped` states.
