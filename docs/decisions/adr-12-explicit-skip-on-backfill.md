---
area: sdk
accepted: by 2026-02-24
---
# ADR #12 — Explicit `skip_on_backfill` Parameter (not heuristic)

**Decision:** Tasks declare `skip_on_backfill=True` explicitly instead of UI guessing from inlets/outlets.

**Rationale:**
- Old heuristic: `isSource = outlets > 0 && inlets === 0` — broke when scrapers had inlets for lineage
- Explicit is better than implicit
- Developer controls behavior, not UI assumptions

**Implementation:**
```python
@task.sfn(arn=ARNS["scraper"], outlets=[raw_data], skip_on_backfill=True)
def scrape_pdp():
    pass
```
