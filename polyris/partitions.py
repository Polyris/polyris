"""PartitionRange — partition key formatting, range expansion, translation.

Per ADR #58. Used by backfill backend (``routes/backfill.py``) to convert
user-friendly date ranges into the concrete list of partition keys that
``polyris-bulk-backfill`` Map iterates over.

Granularity formats (all UTC):

  hourly   YYYY-MM-DDTHH    e.g. 2024-01-15T14
  daily    YYYY-MM-DD       e.g. 2024-01-15
  weekly   YYYY-Www         e.g. 2024-W03 (ISO 8601 week, Monday-start)
  monthly  YYYY-MM          e.g. 2024-01

Each granularity has a "floor to bucket" semantic: a sub-bucket date
(e.g., a daily date for weekly granularity) snaps to the bucket start.
The same is true on the input side — users can supply ``2024-01-15``
when picking a weekly range, and we'll clip to ``2024-W03``.

This module is dependency-free (stdlib datetime only). No AWS calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

Granularity = Literal["hourly", "daily", "weekly", "monthly"]

GRANULARITIES: tuple = ("hourly", "daily", "weekly", "monthly")

# Format validators per granularity. Used at the boundary (user input,
# Asset.partition_start). Internal storage always uses the formal format.
_FORMAT_PATTERNS = {
    "hourly":  re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$"),
    "daily":   re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "weekly":  re.compile(r"^\d{4}-W\d{2}$"),
    "monthly": re.compile(r"^\d{4}-\d{2}$"),
}

# Hard limit per ADR #51 Q8 — must match BackfillLimits.PARTITION_HARD_LIMIT
# in sam/lambdas/console_api/constants.py. Lowered from 5000 to 1000 because
# the bulk-backfill Inline Map's history-event budget (~20 events/partition,
# 25k AWS hard ceiling) caps at ~1250 partitions; 1000 gives headroom.
# Backfills larger than this must be chunked client-side.
_PARTITION_HARD_LIMIT = 1000


# -----------------------------------------------------------------------
# Internal helpers — parse, floor, advance, format
# -----------------------------------------------------------------------

def _parse_to_datetime(s: str, granularity: Granularity) -> datetime:
    """Parse a partition key OR loose date input into a UTC ``datetime``.

    For granularity-specific input, expect the formal format. For loose
    input (e.g., ``2024-01-15`` for weekly), parse as a date and let the
    caller floor to the bucket.
    """
    if granularity == "hourly":
        if "T" in s:
            return datetime.strptime(s, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        # Accept plain date — hour defaults to 0
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if granularity == "daily":
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if granularity == "weekly":
        if "W" in s:
            year, week = s.split("-W")
            return datetime.fromisocalendar(int(year), int(week), 1).replace(tzinfo=timezone.utc)
        # Loose date input — return as date, caller will floor
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if granularity == "monthly":
        if len(s) == 7 and s[4] == "-":
            return datetime.strptime(s, "%Y-%m").replace(tzinfo=timezone.utc)
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    raise ValueError(f"Unknown granularity: {granularity!r}")


def _floor_to_bucket(dt: datetime, granularity: Granularity) -> datetime:
    """Snap a datetime to the start of its bucket."""
    if granularity == "hourly":
        return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "daily":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "weekly":
        # ISO 8601: Monday is day 1
        monday = dt - timedelta(days=dt.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "monthly":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unknown granularity: {granularity!r}")


def _advance(dt: datetime, granularity: Granularity) -> datetime:
    """Move ``dt`` forward by one bucket."""
    if granularity == "hourly":
        return dt + timedelta(hours=1)
    if granularity == "daily":
        return dt + timedelta(days=1)
    if granularity == "weekly":
        return dt + timedelta(weeks=1)
    if granularity == "monthly":
        # Manual month math to avoid dateutil dependency
        if dt.month == 12:
            return dt.replace(year=dt.year + 1, month=1)
        return dt.replace(month=dt.month + 1)
    raise ValueError(f"Unknown granularity: {granularity!r}")


def _format_key(dt: datetime, granularity: Granularity) -> str:
    """Render ``dt`` to the canonical partition key string."""
    if granularity == "hourly":
        return dt.strftime("%Y-%m-%dT%H")
    if granularity == "daily":
        return dt.strftime("%Y-%m-%d")
    if granularity == "weekly":
        iso = dt.isocalendar()
        # isocalendar() returns (year, week, weekday)
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "monthly":
        return dt.strftime("%Y-%m")
    raise ValueError(f"Unknown granularity: {granularity!r}")


# -----------------------------------------------------------------------
# Cross-granularity partition mapping (ADR #87 — intersecting-window default)
# -----------------------------------------------------------------------

def partitions_covering(
    target_key: str,
    target_granularity: Granularity,
    upstream_granularity: Granularity,
) -> List[str]:
    """Upstream partition keys whose time window intersects a target
    partition's window — the default partition mapping of ADR #87.

    Cases:
      - same granularity            -> ``[target_key]`` (1↔1, the common case)
      - coarser target, finer up    -> the covering set
        (e.g. one daily target over an hourly upstream -> 24 keys)
      - finer target, coarser up    -> the single containing upstream bucket
        (e.g. one hourly target over a daily upstream -> 1 key)

    All times are UTC. Reuses the module's bucket helpers; no new date math.
    """
    if target_granularity not in GRANULARITIES:
        raise ValueError(
            f"Invalid target granularity {target_granularity!r}; "
            f"must be one of {GRANULARITIES}"
        )
    if upstream_granularity not in GRANULARITIES:
        raise ValueError(
            f"Invalid upstream granularity {upstream_granularity!r}; "
            f"must be one of {GRANULARITIES}"
        )

    t_start = _floor_to_bucket(
        _parse_to_datetime(target_key, target_granularity), target_granularity
    )
    t_end = _advance(t_start, target_granularity)  # exclusive

    cur = _floor_to_bucket(t_start, upstream_granularity)
    out: List[str] = []
    guard = 0
    while cur < t_end:
        out.append(_format_key(cur, upstream_granularity))
        cur = _advance(cur, upstream_granularity)
        guard += 1
        if guard > 100_000:  # pragma: no cover -- defensive runaway guard; no legitimate granularity pair produces a covering set this large
            raise RuntimeError(
                f"covering set runaway for {target_granularity!r} <- "
                f"{upstream_granularity!r}"
            )
    # The loop always runs at least once: cur = floor(t_start) <= t_start, and
    # t_end = t_start + one target bucket > t_start, so cur < t_end on entry.
    # The finer-target case (one hourly target over a daily upstream) is handled
    # here too — it yields the single containing bucket — so `out` is never empty.
    return out


# -----------------------------------------------------------------------
# Public surface — PartitionRange
# -----------------------------------------------------------------------

@dataclass
class PartitionRange:
    """A frozen, ordered list of partition keys for one granularity.

    Construct via :meth:`expand` (range → keys), :meth:`from_keys` (explicit
    list), or :meth:`translate_to` (re-bucket to a different granularity).
    """

    keys: List[str] = field(default_factory=list)
    granularity: Granularity = "daily"

    def __post_init__(self):
        if self.granularity not in GRANULARITIES:
            raise ValueError(
                f"Unknown granularity {self.granularity!r}; "
                f"must be one of {GRANULARITIES}"
            )
        # Validate every key matches the granularity format
        pattern = _FORMAT_PATTERNS[self.granularity]
        for k in self.keys:
            if not pattern.match(k):
                raise ValueError(
                    f"Partition key {k!r} does not match {self.granularity} format"
                )

    def __len__(self) -> int:
        return len(self.keys)

    def __iter__(self):
        return iter(self.keys)

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    # -----------------------------------------------------------------
    # Construction helpers
    # -----------------------------------------------------------------

    @classmethod
    def expand(
        cls,
        start: str,
        end: str,
        granularity: Granularity,
        partition_start: Optional[str] = None,
    ) -> "PartitionRange":
        """Expand a date range into a list of partition keys.

        Args:
            start: Inclusive start date, loose or formal format.
            end: Inclusive end date, loose or formal format.
            granularity: One of ``"hourly"|"daily"|"weekly"|"monthly"``.
            partition_start: Asset's declared earliest partition; if
                ``start`` is before this, silently clip to ``partition_start``.

        Raises:
            ValueError: if ``end < start`` or expansion exceeds hard limit.
        """
        if granularity not in GRANULARITIES:
            raise ValueError(
                f"Unknown granularity {granularity!r}; "
                f"must be one of {GRANULARITIES}"
            )

        start_dt = _parse_to_datetime(start, granularity)
        end_dt = _parse_to_datetime(end, granularity)

        # Clip start to asset's partition_start if applicable.
        if partition_start:
            ps_dt = _parse_to_datetime(partition_start, granularity)
            ps_dt = _floor_to_bucket(ps_dt, granularity)
            if ps_dt > start_dt:
                start_dt = ps_dt

        # Floor to bucket boundary
        start_dt = _floor_to_bucket(start_dt, granularity)
        end_dt = _floor_to_bucket(end_dt, granularity)

        if end_dt < start_dt:
            raise ValueError(
                f"End ({end}) is before start ({start}) after bucket alignment"
            )

        keys: List[str] = []
        current = start_dt
        while current <= end_dt:
            keys.append(_format_key(current, granularity))
            current = _advance(current, granularity)
            if len(keys) > _PARTITION_HARD_LIMIT:
                raise ValueError(
                    f"Range exceeds hard limit of {_PARTITION_HARD_LIMIT} partitions"
                )

        return cls(keys=keys, granularity=granularity)

    @classmethod
    def from_keys(
        cls,
        keys: List[str],
        granularity: Granularity,
    ) -> "PartitionRange":
        """Construct from an explicit (possibly sparse) key list.

        Keys are sorted by canonical comparison (lexicographic works for
        all four formats by design).
        """
        sorted_keys = sorted(set(keys))
        if len(sorted_keys) > _PARTITION_HARD_LIMIT:
            raise ValueError(
                f"Key list exceeds hard limit of {_PARTITION_HARD_LIMIT} partitions"
            )
        return cls(keys=sorted_keys, granularity=granularity)

    # -----------------------------------------------------------------
    # Transformations
    # -----------------------------------------------------------------

    def translate_to(self, target_granularity: Granularity) -> "PartitionRange":
        """Re-bucket all keys to a coarser (or equal) ``target_granularity``,
        deduped.

        Used for ``cascade=all`` on multi-granularity consumers (e.g., a
        daily-asset backfill cascading to a weekly summary — daily → weekly).

        Only coarsening (or no-op) is meaningful: each existing key maps into
        the single coarser bucket that contains it. Translating to a *finer*
        granularity is rejected — it would silently drop partitions (one weekly
        key floors to a single day, inventing 1 of 7 days rather than expanding
        to the week), which is never what a caller wants. Cascade always runs
        toward coarser downstream consumers, so this guard never fires in
        practice; it exists to fail loudly if that assumption ever breaks.
        """
        if target_granularity == self.granularity:
            return PartitionRange(keys=list(self.keys), granularity=self.granularity)
        if target_granularity not in GRANULARITIES:
            raise ValueError(
                f"Unknown granularity {target_granularity!r}; "
                f"must be one of {GRANULARITIES}"
            )
        if GRANULARITIES.index(target_granularity) < GRANULARITIES.index(self.granularity):
            raise ValueError(
                f"translate_to only coarsens: cannot re-bucket "
                f"{self.granularity!r} partitions to finer {target_granularity!r} "
                f"(would drop partitions). Order: {GRANULARITIES}."
            )

        target_dts = set()
        for key in self.keys:
            dt = _parse_to_datetime(key, self.granularity)
            floored = _floor_to_bucket(dt, target_granularity)
            target_dts.add(floored)

        sorted_dts = sorted(target_dts)
        new_keys = [_format_key(dt, target_granularity) for dt in sorted_dts]
        return PartitionRange(keys=new_keys, granularity=target_granularity)

    def skip_completed(self, completed_keys: set) -> "PartitionRange":
        """Return a new PartitionRange excluding keys already complete.

        ``completed_keys`` is typically gathered from a pipeline-tokens
        DDB scan in the backfill resolver.
        """
        remaining = [k for k in self.keys if k not in completed_keys]
        return PartitionRange(keys=remaining, granularity=self.granularity)

    # Cost estimation removed in v0.78.2 — see ADR #62. The methodology
    # described in ADR #53 was sound but the "estimate without actuals"
    # surface created UX confusion. Estimate + actual reconciliation +
    # budgets is a coherent Pro-tier feature, not a half-Community one.
    # The constants and computation are recoverable from git history at
    # tag v0.78.1 if/when Pro re-introduces cost.
