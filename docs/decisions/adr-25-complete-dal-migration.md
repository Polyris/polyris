---
area: console-api
accepted: by 2026-02-24
---
# ADR #25 — Complete DAL Migration — Zero Direct Table Access in Routes

**Decision:** All 55 direct `boto3.Table()` operations in route files migrated to repository methods. Routes never call `table.get_item()`, `table.query()`, etc. directly.

**Problem:** DAL repos were introduced but routes still bypassed them with `repo.table.query(...)` or kept local table references. This defeated the purpose — schema changes, testing, and instrumentation still required touching every route file.

**Migration scope (v69.3):**
- 10 route files, 55 direct DDB calls → 0
- Patterns: `get_item` → `repo.get()`, `query` → `repo.query_by_date()` / `query_by_pipeline_execution()`, `update_item` → `repo.update()`, `put_item` → `repo.put()`, `scan` → `repo.scan()` / `list_all()`
- Raw queries (need `LastEvaluatedKey`, `Count`): `repo.query_by_date_raw(**params)`, `repo.scan_raw(**params)`
- Signature changes: `resolve_task_item()` dropped `table` param, `_query_pipeline_by_date_range()` and `_reconcile_running()` dropped `tokens_table` param

**Exception:** `health.py` circuit_breaker table — separate table with no repo. Acceptable — it's a monitoring-only table, not part of the core data model.

**What this enables:**
- Schema changes: edit 1 repo file instead of 10+ routes
- Testability: mock repos instead of boto3 internals
- Instrumentation: add logging/metrics at repo layer if needed
- Consistency: all conditional writes, pagination, error handling defined once
