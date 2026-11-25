# ADR #17 — Task Output: Truncate vs S3 Claim Check

**Decision:** Truncate large outputs (200KB limit per task) instead of implementing S3 claim check pattern.

**Rationale:**
- Large outputs between tasks is an anti-pattern — data should flow through data layer (S3), not orchestration layer
- Task outputs should be metadata: paths, row counts, status flags
- S3 claim check adds complexity (Lambda between every task) and hides bad design

**If you hit the 200KB limit:**
1. Refactor your task to write data to S3 and return only the path
2. The truncation message `{"_truncated": true, "_size": 500000}` tells you the original size

**Example:**
```python
# ❌ Anti-pattern: passing data through orchestration
return {"rows": [{"id": 1, "data": "..."}, ...]}  # 500KB

# ✅ Correct: passing references
return {"output_path": "s3://bucket/output/2026-01-01/", "row_count": 50000}
```
