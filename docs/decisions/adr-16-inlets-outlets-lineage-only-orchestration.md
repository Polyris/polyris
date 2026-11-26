---
area: sdk
accepted: by 2026-02-24
---
# ADR #16 — Inlets/Outlets for Lineage Only (not orchestration)

**Decision:** `inlets` and `outlets` are metadata for lineage tracking. They do not affect task execution, skipping, or scheduling.

**Rationale:**
- Old behavior: backfill used `skip_source_tasks` heuristic — tasks with no inlets were considered "source tasks" and auto-skipped
- Problem: scrapers that read from Athena views had inlets for lineage → no longer skipped during backfill
- Mixing lineage metadata with orchestration logic creates hidden coupling

**Resolution:** Removed `skip_source_tasks` heuristic. Replaced with explicit `skip_on_backfill=True` parameter (see #12). Clear separation of concerns:

| Parameter | Purpose | Affects execution? |
|-----------|---------|-------------------|
| `outlets` | Declares what task produces (asset events) | Yes (emits asset event) |
| `inlets` | Declares what task reads (lineage display) | No |
| `dependencies` | Task waits for these tasks (same pipeline) | Yes (blocks) |
| `skip_on_backfill` | Skip this task during backfill | Yes (skips) |
