---
area: sdk
accepted: 2026-05-20
---
# ADR #52 — Pipeline Granularity — runtime cron cadence inference, no DSL change

#### Context

Backfill (ADR #51) needs to know how to expand a date range into
partition keys for a `target.type == "pipeline"` request. For asset
targets, granularity is declared explicitly (`Asset(granularity="weekly")`,
ADR #50). For pipeline targets, no equivalent declaration exists.

Pre-v0.78 backfill code always expands ranges as daily — pipeline
`weekly_summary` (cron `0 8 * * MON`) backfilled over 30 days creates 30
daily executions, of which only 4 fall on Mondays. The other 26 execute
on days the pipeline normally would not run, producing redundant work
and silent data inconsistency.

Three design options were considered:

1. **Add `@dag(granularity="weekly")` field to DSL** — explicit declaration
   matching `Asset.granularity`. Symmetric, but requires user education
   and a new DSL field with validation logic.
2. **Strict cron-to-outlet validation at deploy** — derive granularity
   from outlets, error if cron doesn't match (e.g., daily cron + weekly
   outlet → deploy fails). Catches inconsistencies early.
3. **Runtime inference with daily fallback** — backend inspects
   `pipeline.schedule` (cron string) at backfill start time, infers
   cadence, falls back to daily for ambiguous cases with warning surfaced
   in preview.

Option 3 chosen (Question #3 (B)). Rationale: no DSL change burden, no
deploy-time gating, fallback is safe default, ambiguity surfaced
transparently in UI.

#### Decision

Add `polyris/granularity.py` with `infer_cron_cadence(cron_string)`
function. Called from `routes/backfill.py::start_backfill` when target is
a pipeline. Result feeds `polyris.partitions.expand_range`.

##### Inference rules

```python
def infer_cron_cadence(cron: str) -> Optional[str]:
    """
    Returns one of: "hourly", "daily", "weekly", "monthly", or None
    (ambiguous → caller falls back to "daily" with warning).
    
    Standard 5-field cron: minute hour day-of-month month day-of-week
    """
    # Pure daily — every day, fixed hour
    "0 8 * * *"           → "daily"
    "30 0 * * *"          → "daily"
    
    # Hourly — every hour or sub-hourly fixed
    "0 * * * *"           → "hourly"
    "*/15 * * * *"        → "hourly"  # quarter-hourly counted as hourly bucket
    
    # Weekly — fixed day of week, no day-of-month
    "0 8 * * MON"         → "weekly"
    "0 8 * * 1"           → "weekly"  # numeric form
    "0 8 * * SUN"         → "weekly"
    
    # Monthly — fixed day of month, no day-of-week
    "0 8 1 * *"           → "monthly"
    "0 8 15 * *"          → "monthly"
    
    # Ambiguous cases (return None, caller falls back daily with warning)
    "0 8 * * 1-5"         → None  # weekdays only — 5 per week, neither daily nor weekly cleanly
    "0 8 1,15 * *"        → None  # twice monthly — no clean granularity
    "0 8 * 1,4,7,10 *"    → None  # quarterly — no granularity bucket for it
    "*/5 9-17 * * 1-5"    → None  # business-hours frequency — neither hourly nor daily
    
    # Manual-only pipeline
    None or ""            → "daily"  # default for no schedule
```

##### Algorithm

```python
def infer_cron_cadence(cron: Optional[str]) -> Optional[str]:
    if not cron or not cron.strip():
        return "daily"  # manual-only pipelines default to daily for backfill
    
    parts = cron.split()
    if len(parts) != 5:
        return None  # invalid cron format → ambiguous
    
    minute, hour, day_month, month, day_week = parts
    
    # Hourly: hour field is wildcard or wildcard-step
    if hour in ("*", "*/1") or _is_wildcard_step(hour):
        return "hourly"
    
    # If hour is fixed (single value), continue checking date fields
    if not _is_single_value(hour):
        return None  # multiple hours → ambiguous (every 6h, etc.)
    
    # Daily: both day fields wildcard, hour fixed
    if day_month == "*" and day_week == "*":
        return "daily"
    
    # Weekly: day-of-week fixed (named or numeric), day-of-month wildcard
    if day_month == "*" and _is_single_value(day_week):
        return "weekly"
    
    # Monthly: day-of-month fixed, day-of-week wildcard
    if _is_single_value(day_month) and day_week == "*":
        return "monthly"
    
    return None  # everything else is ambiguous
```

Helper predicates:

- `_is_single_value(field)` — `True` if field is a single number, named
  day, or zero — `False` for ranges, lists, steps, wildcards.
- `_is_wildcard_step(field)` — `True` for `*/N` patterns.

Implementation uses regex matching, ~80 LOC including helpers and named
day translation (MON→1, TUE→2, etc.). No external library dependency.

#### Integration with backfill

`routes/backfill.py::start_backfill` flow when `target.type == "pipeline"`:

```python
pipeline = pipelines_repo.get(target.name)
schedule = pipeline.get("schedule")  # cron string from registry

inferred = infer_cron_cadence(schedule)

if inferred is None:
    granularity = "daily"
    warnings.append({
        "code": "cron_ambiguous",
        "message": (
            f"Cannot reliably infer cadence from cron '{schedule}'. "
            f"Defaulting to daily. {expected_count} partitions will be "
            f"created in the requested range; some may execute on days "
            f"this pipeline doesn't normally run."
        )
    })
else:
    granularity = inferred

partition_keys = expand_range(start, end, granularity, ...)
```

For asset target, granularity comes from `asset.granularity` (no
inference needed). For batch target, each item resolves independently.

#### Why not strict validation at deploy

The rejected option 2 ("strict cron-to-outlet validation") was attractive
because it catches inconsistencies early. Rejected because:

1. **Many cron patterns are legitimately ambiguous.** `0 8 * * 1-5`
   (weekdays only) is a real pattern users write. It's neither daily
   (only 5 of 7 days) nor weekly (5 per week). Strict validation forces
   user to either pick a misleading granularity or refactor their cron.
   Neither is good UX.

2. **No outlets is a valid pipeline.** Some pipelines are pure side-effect
   (S3 cleanup, config sync, alert delivery) and declare no assets. Strict
   validation against outlets has nothing to check.

3. **Backfill UX wants to be helpful, not blocking.** A user kicking off
   a backfill for a weekdays-only pipeline should get reasonable behavior
   (daily expansion with warning) — not "deploy failed because your cron
   is non-standard."

4. **Decoupling cron from granularity is fine in practice.** Asset
   granularity (ADR #50) is the source of truth for *data shape*. Pipeline
   cadence is just *when triggers fire*. Mismatch between them is a soft
   issue that drift detection (ADR #50) catches at runtime, not a deploy
   blocker.

#### Why not @dag(granularity=...) override

The rejected option 1 was attractive because it gives explicit user
control. Rejected because:

1. **Adds DSL surface area.** New field, new validation, new docs,
   new test coverage. CLAUDE.md #2 (no duplication) — `Asset.granularity`
   already exists; pipeline-level field is parallel concept.

2. **Not needed for the 80% case.** Standard daily/weekly/monthly cron
   patterns infer correctly. Only ambiguous crons need disambiguation —
   handled by warning rather than mandatory declaration.

3. **Inference works at runtime when needed.** No need to materialize
   granularity into the registry until backfill is requested. Lazy and
   cheap.

If real cases emerge where users want to override the inference (e.g.,
"my weekdays cron should be treated as daily for backfill purposes"), a
narrow `@dag(backfill_granularity="daily")` field can be added in a
follow-up ADR without breaking changes — it would only suppress the
warning.

#### Edge cases

| Cron | Inferred | Notes |
|---|---|---|
| `0 8 * * *` | daily | Standard daily |
| `0 */6 * * *` | hourly | Every 6 hours — bucketed as hourly |
| `*/15 9-17 * * 1-5` | None → daily + warn | Multiple ambiguities (range hours, weekdays) |
| `0 8 * * MON` | weekly | Day-of-week fixed |
| `0 8 * * MON,WED,FRI` | None → daily + warn | Multiple days of week |
| `0 8 1 * *` | monthly | Day 1 of month |
| `0 8 L * *` | monthly | "L" = last day; treated as monthly |
| `0 0 ? * MON#1` | None → daily + warn | First Monday of month — quarterly-ish, no clean bucket |
| `@daily` (shorthand) | daily | Equivalent to `0 0 * * *` |
| `@weekly` | weekly | Sunday at midnight |
| `@monthly` | monthly | First of month at midnight |
| `rate(1 hour)` | hourly | AWS EventBridge rate expression |
| `rate(1 day)` | daily | AWS EventBridge rate expression |
| Empty / null | daily | Manual-only pipeline, daily default |
| Invalid format | None → daily + warn | Malformed cron, can't parse |

EventBridge `rate(N units)` expressions are mapped via separate helper
since they're not standard cron. `@shorthand` patterns are mapped via
lookup table.

The implementation handles only what's listed above. Pathological cases
("first Monday of every third month") fall to `None` → daily + warn —
not pretending to support what we don't.

#### Update to pipeline_registry — none

The original plan was to write `granularity` to `pipeline_registry` at
deploy time. Dropped because inference is runtime-cheap (~1 µs per call,
no AWS calls needed) and avoiding deploy-time write keeps `polyris-deploy`
simpler.

If profiling later shows inference is hot, cache result in `pipeline_registry`
as denormalization — easy follow-up.

#### Testing

`tests/sdk/test_granularity.py` (new, ~150 LOC):

- 20+ cases from the edge cases table above
- Test that inference doesn't crash on malformed input
- Test that warnings surface correctly in `/api/backfill` response
- Test fallback to daily for None

No drift test needed — `infer_cron_cadence` is pure function, no shared
schema with SFN templates.

#### Out of scope

- Quarterly granularity (every 3 months, named explicitly via cron) —
  current 4-granularity set (hourly/daily/weekly/monthly) from ADR #50
  is closed. If quarterly use case emerges, extension goes through ADR
  amendment.
- AWS Schedule expressions beyond `rate()` and standard cron — out of
  scope; if user uses fancy EventBridge syntax they get daily + warn.
- Cron disambiguation UI (helper to suggest "did you mean weekly?") —
  out of scope for v0.78.

#### Cost

Pure runtime function call, no infrastructure. Zero monthly cost added.
