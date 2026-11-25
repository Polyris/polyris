# ADR #5 — DynamoDB for State (not Step Functions native, not CloudWatch metrics)

**Decision:** Track task state in DynamoDB, not rely on SFN execution state or CloudWatch metrics.

**Rationale:**
- Query across executions (SFN state is per-execution, no cross-query)
- UI can show history and filter by date/pipeline/status
- Cross-pipeline visibility in one table
- SFN state is ephemeral — execution history expires after 90 days
- CloudWatch metrics are aggregated — can't query individual task status
- DynamoDB gives single-digit millisecond reads for UI responsiveness

**Why not SFN DescribeExecution:**
- One API call per task — at 200 tasks, that's 200 API calls per page load
- Rate limited (25 TPS default)
- No filtering by date, pipeline, or status
- No cross-pipeline aggregation

**Why not CloudWatch metrics:**
- Aggregated counters (succeeded: 5, failed: 2) — no per-task detail
- No task names, no timestamps, no error messages
- 1-minute granularity minimum — not real-time
- Can't power a dashboard that shows individual task status

**Tables:** 8 tables for different concerns
- pipeline_tokens (task state + canonical output)
- dependency_subscriptions (dependency tracking)
- task_events (timeline history)
- pipeline_registry (DAG metadata)
- asset_events, asset_subscriptions (asset orchestration)
- queued_asset_events (AND logic for multi-asset triggers)
- api_tokens (PATs — ADR #65, API tokens)

## Trade-offs Accepted

### Eventual Consistency

DynamoDB reads may be slightly stale (unless ConsistentRead). Accepted because:
- All critical writes use conditional expressions (no lost updates)
- evaluate_deps uses ConsistentRead for status checks
- Auto-registration via CloudTrail → EventBridge has ~1 second delay (acceptable)

### Data Passing via DynamoDB (not SFN native)

Task outputs are stored in DynamoDB under a **canonical key** (`output#pipeline#task#date`) —
stable, run-independent. Downstream reads this key via `Read_Upstream_Outputs` in run_task.

**Write:** Save_Success writes canonical output (up to 200KB per task, truncated automatically).
**Read:** Get_Dep_Output reads canonical key, truncates to 25KB per dependency (Step Functions 256KB payload limit).
**Skip:** Auto_Skip never touches canonical — previous output preserved for incremental backfill.

For larger data, tasks should write to S3 and pass the path as output.

### No Dynamic Tasks

Can't generate tasks at runtime. Accepted because:
- Step Functions Map state covers most cases
- Simpler to understand
- Matches established orchestrator model
