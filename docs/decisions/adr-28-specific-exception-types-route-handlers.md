---
area: console-api
accepted: by 2026-02-24
---
# ADR #28 — Specific Exception Types in Route Handlers

**Decision:** Route handlers use `except (ClientError, BotoCoreError)` instead of `except Exception`. Broad `except Exception` only at route-level top-level handlers.

**Rationale:**
- `except Exception` silently swallows unexpected errors (TypeError, KeyError, AttributeError) that indicate bugs
- `ClientError` catches AWS service errors (throttle, validation, not found)
- `BotoCoreError` catches network errors (timeout, connection refused, endpoint unreachable)
- Together they catch all AWS infrastructure issues but NOT programming bugs — those bubble up and get noticed
- 3 of 13 original catches were completely silent (`except Exception:` without `as e` or logging)
- Graceful degradation preserved: inner catches `continue` or return partial data

**Pattern:**
```python
from botocore.exceptions import ClientError, BotoCoreError

# ✅ Route-level: catch-all is correct (returns 500 to client)
def get_pipeline_executions(...):
    try:
        ...
    except Exception as e:
        log.error(...)
        return cors_response(500, {'error': str(e)})

# ✅ Inner: specific AWS errors
try:
    items = executions_repo.query_by_date(...)
except (ClientError, BotoCoreError) as e:
    log.error(...)
    continue  # Skip this date, try next

# ❌ Never: silent broad catch
except Exception:
    pass
```

**Migration (v69.4):** `pipelines_list.py` — 13 `except Exception` → 1. Added `from botocore.exceptions import ClientError, BotoCoreError`.
