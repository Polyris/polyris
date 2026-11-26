---
area: engine
accepted: by 2026-02-24
---
# ADR #14 — Canonical Output Key (not run-specific)

**Decision:** Store task output under stable key `output#pipeline#task#date` instead of run-specific `task-date-executionShort`.

**Rationale:**
- Run-specific key breaks incremental backfill (skip writes empty `{}`, overwrites real output)
- Canonical key is run-independent — latest successful output always available
- Read_Upstream doesn't need to know execution details
- Auto_Skip never touches canonical — previous output preserved

**Cost:** One extra DynamoDB putItem per successful task in same transaction. ~$0.22/month at 60K tasks.
