---
area: engine
accepted: by 2026-02-24
---
# ADR #10 — Single Execution ID Pattern + `pipeline_execution_short`

**Decision:** Use deterministic execution names (task-date-short). Keep `pipeline_execution_short` as a core identifier.

**Rationale:**
- Prevents duplicate runs for same date
- Makes backfill idempotent
- Easy correlation in logs

**Format:** `{task}-{date}-{pipeline_short}`
Example: `scrape-2026-01-12-abc12345`

**`pipeline_execution_short` (keep decision):**
- 103 references across codebase — refactoring is 2-3 day effort
- Industry moving in same direction (short execution IDs)
- Used in: execution_name construction, DynamoDB keys, SFN input, UI display
- Collision probability at 20 chars: ~10^-19 at 1000 concurrent pipelines (safe)
- Revisit only if it causes a concrete bug
