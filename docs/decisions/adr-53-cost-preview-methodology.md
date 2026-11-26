---
area: console-api
accepted: 2026-05-20
---
# ADR #53 — Cost Preview Methodology — formula, sources, accuracy disclaimer

#### Context

ADR #51 introduces backfill cost preview in `POST /api/backfill` response
(`estimated_sfn_cost_usd`) and in the UI before user submits the
operation. This requires a methodology — what's included, what's excluded,
how accurate, and how surfaced.

Competing orchestrators (Dagster) don't preview costs; they don't have to,
since their pricing is hosting-based (you pay for the orchestrator
cluster regardless of operations). polyris is serverless — every
operation costs incremental dollars — so cost preview is meaningful and
adds real value as a differentiator.

#### Decision

Cost preview is **best-effort estimate**, surfaced as a single dollar
value in the API response and a one-line summary in the UI. The
methodology covers AWS Step Functions transitions for the bulk-backfill
SFN itself and the spawned child pipeline executions. Lambda invocations
and DynamoDB writes are excluded — they fall under AWS Free Tier for
realistic volumes and would noise the estimate.

Estimate is computed in `polyris/partitions.py::PartitionRange.cost_estimate()`
and called from the request handler before responding.

##### Formula

```
sfn_cost_usd = (bulk_backfill_transitions + child_execution_transitions) 
             * STANDARD_SFN_PRICE_PER_TRANSITION

where:
  STANDARD_SFN_PRICE_PER_TRANSITION = 0.000025  # $0.025 per 1000

  bulk_backfill_transitions = (
      INITIALIZE_TRANSITIONS               # 5 (constant)
    + N_PARTITIONS * PER_ITERATION         # 4 per Map iteration
    + FINALIZE_TRANSITIONS                 # 3 (constant)
  )

  child_execution_transitions = (
      N_PARTITIONS * AVG_TASKS_PER_PIPELINE * EXPRESS_PER_TASK
    + N_PARTITIONS * STANDARD_WRAPPER_OVERHEAD
  )
  
  Where:
    EXPRESS_PER_TASK = 0  # Express SFN per-task is per-request, not transition
    STANDARD_WRAPPER_OVERHEAD = ~50 transitions per pipeline execution
                                (dependency_wrapper invocations, state changes)
```

Specifically per partition for a typical 4-task pipeline:
- Bulk-backfill iteration: 4 transitions
- Child pipeline (Standard SFN): ~50 transitions (Initialize, 4× Wrap+Run+Resolve+Notify, Finalize)
- Total: ~54 transitions per partition

For 90 partitions: 5 + 90×4 + 3 + 90×50 = 4868 transitions × $0.025/1000 =
**$0.122**.

Of that:
- Bulk-backfill contribution: $0.009 (the overhead we're adding)
- Child pipeline contribution: $0.113 (the work itself, would happen
  regardless of bulk-backfill)

##### What's included vs excluded

**Included** (surfaced to user):
- All AWS Step Functions Standard transitions across bulk-backfill +
  child pipelines for the partitions to be run

**Excluded** (free tier or negligible):
- Lambda invocations (~$0.0000002 per invocation; 1M free/month)
- DynamoDB writes (~$1.25 per million write requests; backfill of 90
  partitions = ~400 writes = $0.0005)
- DynamoDB reads (pre-flight skip_completed scan = ~$0.0001)
- CloudWatch Logs (~$0.50/GB ingested; backfill logs are KB-scale)
- S3 storage for task outputs (user-specific, can't estimate generically)
- Athena/Glue costs for tasks that query data (user-specific)
- Data egress (cross-region, NAT gateway, etc.)
- IAM/STS calls

Excluded items are listed in `/backfills/{id}` Detail Page in a footnote:
"Cost estimate covers Step Functions only. Lambda, DynamoDB, S3, and
data processing costs are not included."

##### Accuracy

The estimate is **±20% accurate** for typical backfills, with these
sources of error:

1. **AVG_TASKS_PER_PIPELINE assumption.** Formula uses the pipeline's
   actual task count from `pipeline_registry`. Accurate per pipeline.
2. **STANDARD_WRAPPER_OVERHEAD constant.** Calibrated empirically against
   smoke pipelines (5 measured runs, mean 48 transitions, σ 4). May
   underestimate for pipelines with many conditional branches or asset
   subscriptions.
3. **Tasks that skip via `skip_tasks` parameter.** Backfill input includes
   `skip_tasks`, but wrapper still transitions through the skip-decision
   states. Cost is paid for skipped tasks (~5 transitions per skipped
   task instead of 50).
4. **Retries.** If a task fails and retries (per-task retry policy),
   actual transitions exceed estimate by retry count × 50. Estimate does
   not account for failure-driven retries.
5. **Mid-run redeploy** (ADR #51, Question #5/D). If pipeline changes
   structure mid-backfill, transitions per partition may differ. Surfaced
   via DAG hash warning, not in cost.

Estimate is labeled "Estimated" in UI and copy reads "approximate based
on current pipeline structure." Users with cost-sensitive workflows are
directed to AWS Cost Explorer for precise post-hoc accounting.

##### Cost preview update flow

The preview updates as user changes inputs in the modal:

- Date range changes → partition count changes → cost updates
- Skip completed toggle → partition count changes → cost updates
- Max parallel changes → duration changes (cost unchanged, since total
  work is identical)
- Task subset changes → child transitions per partition change → cost
  updates

Frontend `RunModal.tsx` calls `POST /api/backfill?preview=true` (idempotent
query param that runs pre-flight only, returns same response shape but
does not start the backfill) on debounced input change (300ms). API
returns within 500ms typical (DDB scan for skip_completed is the
bottleneck).

##### Alternative considered: real-time cost via AWS Pricing API

Rejected. AWS Pricing API queries are slow (~2s), the prices for
Step Functions Standard are stable enough that hardcoding the
$0.025/1000 constant is fine. If AWS changes pricing, we update the
constant in `polyris/partitions.py` — a single source location.

##### Out of scope

- Real-time actual cost tracking (would require periodic Cost Explorer
  polling per backfill) — out of scope; AWS Cost Explorer handles this
  natively
- Cost trends across many backfills ("you spent $X on backfills this
  month") — out of scope; out-of-band reporting
- Multi-region cost differences (SFN pricing is uniform across most
  regions but not all) — out of scope; assume current region pricing
- Reserved capacity / savings plans — out of scope; not applicable to
  SFN
