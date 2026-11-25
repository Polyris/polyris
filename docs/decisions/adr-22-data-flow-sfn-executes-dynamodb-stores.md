# ADR #22 — Data Flow: SFN Executes → DynamoDB Stores → UI Reads

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §4, §5 (1.3.0).** Its one exception goes: no console route asks Step Functions about a run, neither to reconcile a running one nor to mark the tasks of an ended one. The engine keeps the run row true, and a run's status comes from its run row's summary.

**Decision:** All UI reads come from DynamoDB. Step Functions API is used only for control actions and reconciliation of running executions.

**Principle:** The execution engine writes, the database stores, the UI reads from the database.

**Step Functions (execution engine) — WRITE/CONTROL only:**
- Start execution, Stop/Abort execution
- Send task token (callbacks)
- Describe execution (reconcile running tasks only)

**DynamoDB (metadata database) — ALL reads:**
- Calendar, Sidebar stats, DAG view, All Tasks, All Runs
- Execution dropdown, Task Detail, Sparkline, SLA, Metrics

**Reconcile pattern:** Running executions may have stale status in DDB. UI reconciles by calling `sfn.describe_execution` for executions with `status=running`. This is the ONLY case where UI reads from SFN.

**Why not SFN API for reads:**
- `startDate` is physical (when AWS started execution), not logical (`current_date`)
- Calendar must show logical date — "data for Feb 13" not "button pressed Feb 19"
- SFN execution history expires after 90 days — old runs disappear
- `ListExecutions` has no date/status filtering — must fetch all then filter client-side
- Cross-pipeline queries require N API calls (one per pipeline)

**Cost:** DDB reads add ~$0.45/month for 5 users. SFN `ListExecutions` was free but returned wrong dates.

**Affected endpoints (migrated from SFN → DDB):**
- `get_pipeline_executions` (Calendar + Execution dropdown)
- `get_all_runs` (All Runs view)
