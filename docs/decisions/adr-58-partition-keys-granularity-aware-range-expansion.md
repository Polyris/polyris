# ADR #58 — Partition Keys & Granularity-Aware Range Expansion (v0.78.0)

#### Context

ADR #51 introduces `partition_key` as a first-class field on backfill
records and child executions. This ADR specifies:
- Format per granularity
- Range expansion algorithm
- partition_start clipping
- Translation between granularities (for cascade=all on multi-granularity
  consumers)
- Compatibility with existing `current_date` field

#### Decision

##### Partition key format per granularity

| Granularity | Format | Example | Boundary |
|---|---|---|---|
| `hourly` | `YYYY-MM-DDTHH` | `2024-01-15T14` | UTC hour-start |
| `daily` | `YYYY-MM-DD` | `2024-01-15` | UTC midnight |
| `weekly` | `YYYY-Www` | `2024-W03` | ISO 8601 week (Mon-start) |
| `monthly` | `YYYY-MM` | `2024-01` | First of month UTC |

ISO 8601 conformance:
- `weekly`: `YYYY-Www` is ISO 8601 week date (week 1 = week containing
  Jan 4). Already used by Asset DSL `partition_start` validation
  (ADR #50).
- `monthly`: `YYYY-MM` is ISO 8601 reduced precision.
- `daily`: `YYYY-MM-DD` is ISO 8601 calendar date.
- `hourly`: `YYYY-MM-DDTHH` is **shortened** from full ISO 8601
  (`YYYY-MM-DDTHH:MM:SS+ZZ`). We use UTC implicitly and omit minute/second.

All times are UTC. No timezone in the format; documented as UTC by
convention.

##### Range expansion algorithm

```python
# polyris/partitions.py
class PartitionRange:
    @classmethod
    def expand(
        cls,
        start: str,         # ISO date or partition_key
        end: str,
        granularity: Literal["hourly", "daily", "weekly", "monthly"],
        partition_start: Optional[str] = None,  # asset's partition_start (clipping)
    ) -> "PartitionRange":
        # 1. Parse start and end into a normalized comparable form
        start_dt = _parse_to_datetime(start, granularity)
        end_dt = _parse_to_datetime(end, granularity)
        
        # 2. Clip start to asset's partition_start if applicable
        if partition_start:
            ps_dt = _parse_to_datetime(partition_start, granularity)
            if ps_dt > start_dt:
                start_dt = ps_dt  # don't go earlier than asset's history
        
        # 3. Normalize to bucket boundary (round down)
        start_dt = _floor_to_bucket(start_dt, granularity)
        end_dt = _floor_to_bucket(end_dt, granularity)
        
        # 4. Iterate bucket-by-bucket
        keys = []
        current = start_dt
        while current <= end_dt:
            keys.append(_format_key(current, granularity))
            current = _advance(current, granularity)
        
        return cls(keys=keys, granularity=granularity)
```

Helpers:

```python
def _parse_to_datetime(s: str, granularity: str) -> datetime:
    if granularity == "hourly":
        # Accept YYYY-MM-DDTHH or YYYY-MM-DD (defaults to hour 0)
        if "T" in s: return datetime.strptime(s, "%Y-%m-%dT%H")
        return datetime.strptime(s, "%Y-%m-%d")  # hour=0
    
    if granularity == "daily":
        return datetime.strptime(s, "%Y-%m-%d")
    
    if granularity == "weekly":
        if "W" in s:
            # Parse YYYY-Www to ISO Monday-of-week
            year, week = s.split("-W")
            return datetime.fromisocalendar(int(year), int(week), 1)
        # Accept YYYY-MM-DD; clip to that date's ISO week start (Monday)
        d = datetime.strptime(s, "%Y-%m-%d")
        return d - timedelta(days=d.weekday())
    
    if granularity == "monthly":
        if "-" in s and len(s) == 7:  # YYYY-MM
            return datetime.strptime(s, "%Y-%m")
        d = datetime.strptime(s, "%Y-%m-%d")  # accept full date, clip to month
        return d.replace(day=1)

def _floor_to_bucket(dt: datetime, granularity: str) -> datetime:
    if granularity == "hourly":   return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "daily":    return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "weekly":   return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0)
    if granularity == "monthly":  return dt.replace(day=1, hour=0, minute=0, second=0)

def _advance(dt: datetime, granularity: str) -> datetime:
    if granularity == "hourly":   return dt + timedelta(hours=1)
    if granularity == "daily":    return dt + timedelta(days=1)
    if granularity == "weekly":   return dt + timedelta(weeks=1)
    if granularity == "monthly":
        if dt.month == 12: return dt.replace(year=dt.year+1, month=1)
        return dt.replace(month=dt.month+1)

def _format_key(dt: datetime, granularity: str) -> str:
    if granularity == "hourly":   return dt.strftime("%Y-%m-%dT%H")
    if granularity == "daily":    return dt.strftime("%Y-%m-%d")
    if granularity == "weekly":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "monthly":  return dt.strftime("%Y-%m")
```

##### Edge cases

| Input | Granularity | Expansion |
|---|---|---|
| `2024-01-15` to `2024-01-15` | daily | `["2024-01-15"]` (1 partition) |
| `2024-01-15` to `2024-01-15` | weekly | `["2024-W03"]` (clipped to week start; 1 partition) |
| `2024-01-15` to `2024-01-31` | daily | 17 partitions |
| `2024-01-15` to `2024-01-31` | weekly | `["2024-W03", "2024-W04", "2024-W05"]` (3 weeks, partial week 5) |
| `2024-01-31` to `2024-02-01` | monthly | `["2024-01", "2024-02"]` (2 months, both partial) |
| `2024-W03` to `2024-W05` | weekly | `["2024-W03", "2024-W04", "2024-W05"]` |
| Input `2024-12-30` | weekly | ISO calendar: that's week 2025-W01 |
| Reverse order (end < start) | any | Error: `invalid_partition_range` |
| `partition_start=2024-06-01` + request `2024-01-01:2024-12-31` daily | daily | Clipped start to `2024-06-01`; 214 partitions, with warning surfaced in response |

##### Compatibility with `current_date`

Existing pipeline SFN templates (`run_task::Prepare_Task_Input`) use
`$states.input.current_date` to compute date variables for tasks:
- `current_date` → user code via `{{ current_date }}` template variable
- `yesterday`, `tomorrow` → date arithmetic in JSONata
- `minus_1_month`, `day_of_year`, `week_of_year` → calendar math
  (handled at ingest by `routes/backfill.py`, see ADR #51)

After v0.78 redesign, **bulk-backfill SFN translates `partition_key` to
`current_date`** when invoking child pipeline:

| Partition key | current_date (daily anchor) |
|---|---|
| `2024-01-15` (daily) | `2024-01-15` |
| `2024-01-15T14` (hourly) | `2024-01-15` (truncate to date) |
| `2024-W03` (weekly) | Monday of W03 = `2024-01-15` |
| `2024-01` (monthly) | First of month = `2024-01-01` |

This preserves backward compatibility — task code reading `current_date`
sees a YYYY-MM-DD string as it always did.

For tasks that need the original partition_key (e.g., to construct
weekly file paths like `s3://bucket/year=2024/week=03/data.parquet`),
the input also carries the original `partition_key` field unchanged.
Task code accesses it via `{{ partition_key }}` template variable
(new, added to the `polyris/variables.py` registry).

##### Translation for cascade=all on multi-granularity consumers

When `cascade=all` and the backfilled asset (e.g., daily orders) is
consumed by a pipeline of different granularity (e.g., weekly summary),
the consumer must receive a partition_key in **its** granularity, not
the source's.

Translation algorithm:

```python
def translate_to(
    source_keys: List[str],
    source_granularity: str,
    target_granularity: str,
) -> List[str]:
    if source_granularity == target_granularity:
        return source_keys
    
    # Convert each source key to datetime, floor to target bucket,
    # dedupe, format.
    target_dts = set()
    for key in source_keys:
        dt = _parse_to_datetime(key, source_granularity)
        floored = _floor_to_bucket(dt, target_granularity)
        target_dts.add(floored)
    
    sorted_dts = sorted(target_dts)
    return [_format_key(dt, target_granularity) for dt in sorted_dts]
```

Example:
- Source: daily, keys = `["2024-01-15", "2024-01-16", "2024-01-22"]`
- Target: weekly
- Result: `["2024-W03", "2024-W04"]` (Jan 15-16 → W03, Jan 22 → W04)

Each unique target partition is triggered exactly once, even if multiple
source partitions map to it.

##### Validation rules

`POST /api/backfill` validates partition input:

1. **Format match.** If `partitions.start`/`partitions.end` provided as
   strings, parser must accept under the resolved target granularity.
   Mismatched formats → 400 `invalid_partition_format`.
2. **Range non-empty.** start ≤ end. Otherwise 400.
3. **Range bounded.** end - start ≤ 5 years to prevent typo-induced
   massive expansions. Otherwise 400 `range_too_large`.
4. **partition_start clipping.** If asset's `partition_start` > start,
   silently clip start (with warning, not error). Reasonable user
   forgiveness.
5. **Hard partition cap.** After expansion, `len(keys) ≤ 1000`. Else 400
   `range_too_large` (ADR #51, Question #8).

##### Out of scope

- Custom partition definitions (e.g., "every full moon", arbitrary
  irregular partitions) — not supported. Asset must have one of 4
  granularities.
- Per-asset partition_freq override (e.g., daily granularity but only
  twice a day) — out of scope; if needed, hourly granularity covers most
  sub-daily cases.
- Time zones other than UTC — out of scope. All partition keys are UTC.
  If user's data is in another timezone, they normalize at ingest.

#### Testing

`tests/sdk/test_partitions.py` (~350 LOC):
- Format validation per granularity (positive + negative cases)
- Range expansion for all 4 granularities including edge cases (year
  boundaries, ISO week edge weeks, leap day)
- partition_start clipping
- Reverse range rejection
- Translation between granularities (daily→weekly, daily→monthly,
  hourly→daily, dedup correctness)
- 1000-partition limit triggers correct error
- Single-partition cases (start == end)

Coverage target: 100% of `polyris/partitions.py` (small surface, no
external dependencies, achievable).

#### Migration

No migration. `partition_key` is a new field on all new backfill records
and child executions. Legacy executions without it work normally. UI
falls back to `current_date` for display if `partition_key` absent.

`polyris/partitions.py` is a new module; no existing module is replaced.

#### Cost

Zero infrastructure. Pure-Python computation. Sub-microsecond per
expansion call.
