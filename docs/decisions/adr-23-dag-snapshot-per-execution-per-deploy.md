---
area: engine
accepted: by 2026-02-24
---
# ADR #23 — DAG Snapshot Per Execution (not per deploy)

**Superseded by [ADR #137](adr-137-tasks-as-rows.md) §8 (1.3.0).** A run's graph comes from the spec version its run row names, which never changes; new runs write no snapshot. The console reads the snapshots of runs started before 1.3 until 1.4. Lookup: the run's spec version (a 1.2 run's snapshot) → the registry's version → inferred.

**Decision:** Each execution writes its own DAG snapshot to `tokens_table` with key `dag_snapshot::{execution_name}`. TTL: 120 days (AWS SFN history is 90 days + 30-day buffer).

**Problem:** After `polyris-deploy` with changed DAG, old executions in UI showed the *new* graph instead of the graph they actually ran with. Debugging "why did it fail" was impossible because the DAG structure was wrong.

**Alternatives considered:**
- Per-deploy snapshot (S3 or DDB) — doesn't tie to specific execution, overwrites on each deploy
- Git commit hash — requires git in deploy environment, no actual DAG data
- Hash-only indicator — shows "DAG changed" but not *how* it changed

**Why per-execution:** AWS Step Functions guarantees that each execution runs the ASL definition that was active at `StartExecution` time. The DAG metadata is hardcoded as a string literal in the ASL at `polyris-deploy` time. Each execution writes to a unique key (its own execution ARN) — no overwrites, no race conditions.

**API lookup priority:** snapshot → registry → inferred. Old executions (pre-feature) gracefully fall back to registry.

**Cost:** ~2-3 KB per execution × 60,000/month = ~2 GB/year = $0.50/month DDB storage. TTL auto-cleans after 120 days.
