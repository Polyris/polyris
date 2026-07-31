"""BackfillRecord — typed value-object over a Backfill DDB item (ADR #83).

Moved out of dal/backfills_repo.py in v0.80.0 so the DAL stays
persistence-only and the domain view (status/counter semantics) lives in
the models layer. The terminal-status rule itself is owned by
polyris.backfill_status (the single authority); this record only adapts a
DDB item to it.
"""
from datetime import datetime, timezone
from typing import Optional

from polyris.backfill_status import all_map_done, finalize_status
from constants import BACKFILL_TERMINAL_STATUSES, BACKFILL_ACTIVE_STATUSES


class BackfillRecord:
    """Typed, read-only view over a Backfill DDB item (v0.80.0, ADR #83).

    Centralizes every field access and the status/counter semantics so no
    consumer reads raw dict keys or re-derives "done"/"active"/terminal
    status ad-hoc. That ad-hoc re-derivation is exactly what drifted in
    ADR #81 (raw vs. derived in the concurrency guard) and ADR #82 (a stray
    ``+ skipped`` term). With one typed accessor per concept, there is one
    place to be correct.

    Wraps the item by reference; it does not copy. Pure value-object — no
    AWS calls, no writes. The SFN-state reconciliation that heals zombies
    lives in routes/backfill.py because it performs I/O; it consumes this
    record for its reads.
    """

    __slots__ = ("item",)

    def __init__(self, item: dict):
        self.item = item

    # ── Identity ──────────────────────────────────────────────────────────
    @property
    def id(self) -> Optional[str]:
        """Backfill ID. The record's PK is ``execution_name`` and the GSI
        key is ``backfill_id``; both equal the id, so accept either."""
        return self.item.get("backfill_id") or self.item.get("execution_name")

    @property
    def target_pipeline(self) -> Optional[str]:
        return self.item.get("target_pipeline")

    # ── Counters (typed; semantics per polyris.backfill_status) ────────────
    @property
    def total(self) -> int:
        """To-RUN partition count (excludes skip_completed pre-flight skips)."""
        return int(self.item.get("total_partitions", 0) or 0)

    @property
    def completed(self) -> int:
        return int(self.item.get("completed_partitions", 0) or 0)

    @property
    def failed(self) -> int:
        return int(self.item.get("failed_partitions", 0) or 0)

    @property
    def skipped(self) -> int:
        """Pre-flight skip_completed count. Display only — NEVER part of the
        done-check or terminal derivation (ADR #82)."""
        return int(self.item.get("skipped_partitions", 0) or 0)

    # ── Status ────────────────────────────────────────────────────────────
    @property
    def raw_status(self) -> str:
        """The status as stored. May lag reality if the SFN Finalize never
        ran (zombie); use ``derived_status`` / reconcile for the truth."""
        return self.item.get("status", "unknown")

    @property
    def is_terminal(self) -> bool:
        return self.raw_status in BACKFILL_TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        return self.raw_status in BACKFILL_ACTIVE_STATUSES

    @property
    def map_done(self) -> bool:
        """True once every partition the Map runs has an outcome."""
        return all_map_done(self.total, self.completed, self.failed)

    def derived_status(self) -> str:
        """Counters-only effective status (no AWS calls).

        - Already-terminal raw status → respected.
        - Active + all Map partitions processed → canonical finalize status.
        - Otherwise → raw (genuinely still running, or counters inconclusive).
        """
        if not self.is_active:
            return self.raw_status
        if self.map_done:
            return finalize_status(self.completed, self.failed)
        return self.raw_status

    # ── Timing ────────────────────────────────────────────────────────────
    def age_seconds(self) -> Optional[float]:
        """Seconds since the record was created (from ``started_at``), or
        None if absent/unparseable. ``started_at`` is stamped at creation,
        so None indicates a malformed record."""
        started_at = self.item.get("started_at")
        if not started_at:
            return None
        try:
            started = datetime.fromisoformat(started_at)
        except (ValueError, TypeError):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds()
