"""Canonical backfill status derivation (v0.80.0, ADR #83).

A backfill aggregates N partition outcomes. Two pieces of logic decide a
backfill's terminal status, and they MUST agree:

  1. The bulk_backfill SFN's ``Finalize`` state (JSONata), which runs after
     the Map and stamps the terminal status in DynamoDB.
  2. The console API's ``_compute_derived_backfill_status``, which derives
     the terminal status from counters when the SFN's Finalize never ran
     (zombie: timeout/abort/crash) so the UI/guard see reality.

Historically each re-implemented the rule and drifted — twice (ADR #81:
raw vs. derived in the concurrency guard; ADR #82: a stray ``+ skipped``
term reported running backfills as terminal). The fix is structural: this
module is the ONE Python authority for the rule. The API derivation calls
``finalize_status`` directly (so the Python side can never drift), and a
CI parity check (``polyris.codegen.check_backfill_status_parity``) verifies
the SFN's JSONata encodes the same rule (the only place that *can* drift,
since JSONata cannot import Python).

Counter semantics (the part that caused ADR #82):
  - ``total_partitions`` is the number of partitions the Map *runs* — set
    AFTER the skip_completed pre-flight filter, so it EXCLUDES pre-skipped
    partitions.
  - The Map increments only ``completed_partitions`` and
    ``failed_partitions`` (one per iteration).
  - ``skipped_partitions`` is the pre-flight count; the SFN never touches
    it and it is NOT part of ``total``. It must not feed the done-check.

So "all Map work done" is exactly ``completed + failed >= total``.
"""
from __future__ import annotations

from .constants import BackfillStatus


def all_map_done(total: int, completed: int, failed: int) -> bool:
    """True once every partition the Map runs has an outcome.

    ``total`` is the to-run count (excludes skip_completed pre-flight skips).
    Only completed/failed are Map outcomes; pre-flight ``skipped`` is NOT
    included here (see module docstring — this is the ADR #82 invariant).
    """
    return total > 0 and (completed + failed) >= total


def finalize_status(completed: int, failed: int, *, canceled: bool = False) -> str:
    """The canonical terminal status of a finished backfill.

    Mirrors the bulk_backfill SFN Finalize JSONata exactly:

        canceled              → 'canceled'   (cancel wins regardless of counts)
        failed == 0           → 'completed'
        completed == 0        → 'failed'
        otherwise (mixed)     → 'partial'

    This is the single source of the rule. The SFN JSONata is verified
    against it by the parity drift check; do not change one without the
    other (the check will fail CI if they diverge).
    """
    if canceled:
        return BackfillStatus.CANCELED
    if failed == 0:
        return BackfillStatus.COMPLETED
    if completed == 0:
        return BackfillStatus.FAILED
    return BackfillStatus.PARTIAL
