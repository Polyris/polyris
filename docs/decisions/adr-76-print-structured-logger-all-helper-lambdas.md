---
area: engine
accepted: 2026-05-27
---
# ADR #76 — print() → structured logger in all helper Lambdas

#### Context

Per the v0.78.10 Philosophy compliance audit (CLAUDE.md "12-Factor App
logs paragraph"): 4 helper Lambdas — `evaluate_deps`,
`notify_asset_subscribers`, `check_assets`, `query_subscriptions` —
used `print()` for diagnostics, scattered across ~21 sites. CloudWatch
captures them, but unstructured: no level field, no consistent
function name, hard to filter via CloudWatch Insights.

Console API has had `from logger import log` (structured JSON) since
v0.68. The helper Lambdas were carrying this gap because each was
small enough that print() felt sufficient.

#### Decision

**1. Lift `logger.py` to `sam/lambdas/_shared/logger.py`** as canonical.
Identical content to the previous `console_api/logger.py`; one
module class (`_Logger`) with `info` / `warn` / `error` methods.

**2. Copy to each Lambda** at deploy time via the same pattern as
`_shared/constants.py`. Added `make sync-loggers` target with drift
detection — runs in CI and locally; fails if any Lambda's `logger.py`
differs from the canonical.

**3. Migrate ~21 `print(...)` sites** to structured calls:

```python
# Before
print(f"[evaluate_deps] Unhandled error: {type(e).__name__}: {e}")

# After
log.error("handler", "Unhandled error",
          error_type=type(e).__name__, error=str(e))
```

CloudWatch Insights queries now work:
```
filter level = "WARN" and fn = "_check_freshness"
filter level = "ERROR" and fn = "_check_asset"
stats count(*) by level, fn
```

#### Per-Lambda site counts

| Lambda | print() before | After |
|---|---|---|
| evaluate_deps | 3 | 0 |
| notify_asset_subscribers | 9 | 0 |
| check_assets | 5 | 0 |
| query_subscriptions | 4 | 0 |
| **Total** | **21** | **0** |

#### Why copy-per-Lambda instead of shared at runtime

Same reason as `_shared/constants.py`: Lambda deploy packages are
independent. Each Lambda needs `logger.py` in its own deploy
artifact. `make sync-loggers` enforces canonical-source discipline;
divergence fails CI before merge.

#### What this does NOT do

- **Doesn't touch console_api's `logger.py`** functionally — it was
  already canonical. Now copied from `_shared/logger.py` (same
  content), enforced by `make sync-loggers`.
- **Doesn't change log shape.** Still JSON one-line-per-event,
  consumed by CloudWatch Logs as-is.
- **Doesn't add log levels filter env variable.** All levels emitted
  by default; CloudWatch retention policy handles volume.

#### Tests

No new tests for this release — `print() → log.X()` is a pure
substitution at every site, all existing tests continue to pass.
Tests do not assert on log output; that would couple test bodies
to log message strings, which is an anti-pattern.

| Suite | Was | Now | Δ |
|---|---|---|---|
| Python total | 1347 | **1347** | 0 |
| vitest | 830 | 830 | 0 |
| **All gates** | green | **green** | — |

#### Lesson

Closing a Philosophy compliance gap doesn't require a feature
release. A small dedicated cleanup release with one principle's
worth of changes is the right unit. The sync-loggers target follows
the same SSoT pattern as sync-constants — drift catches creep that
manual review would miss.

The 21 sites looked small enough that print() "felt sufficient"
when each Lambda was first written. Adding structured logging
later costs the same as adding it at the start, plus one
incremental release. The lesson is to start with structured
logging on day one; the marginal cost is roughly zero.
