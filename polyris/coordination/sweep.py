"""Rules of the sweeper, the periodic re-check from facts (ADR #130 §8).

The sweeper runs every 5 minutes. It re-checks each waiting task at its due
time, so a lost wake-up hint delays a task instead of losing it. These are
its pure rules; the Lambda in sam/lambdas/coordination applies them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

#: What a task row's ``source`` says when the sweeper asks the reconciler.
SOURCE = "sweeper"
#: The row that holds the lease; ``_``-prefixed rows are internal (CLAUDE.md).
LEASE_KEY = "_sweeper_lease"
#: How long a run holds the lease. Longer than the function's 240 s timeout,
#: shorter than the 5-minute schedule, so a crashed run never blocks the next.
LEASE = timedelta(seconds=270)
#: The work budget of one run, inside the function's 240 s timeout.
BUDGET = timedelta(seconds=200)
#: A row adopted from an older platform version may have no orchestration
#: timeout; the SDK's default (a task's 24-hour execution timeout) stands in.
DEFAULT_ORCHESTRATION_TIMEOUT = timedelta(hours=24)
#: The engine's own row: catch-up progress (and, later, the self-check).
HEALTH_KEY = "_engine_health"
#: A push start that keeps failing transiently is given up after this long, with a warning.
PUSH_RETRY_WINDOW = timedelta(hours=24)
#: How often the catch-up replays the push branch for recent facts, and how far back it reads.
CATCH_UP_EVERY = timedelta(hours=1)
CATCH_UP_WINDOW = timedelta(hours=48)


def liveness_deadline(started_at: datetime, orchestration_timeout_seconds: Optional[float]) -> datetime:
    """When a waiting task's wrapper is checked even without a signal (ADR #130 §3).

    Three orchestration timeouts cover the wrapper's timeout retries; the
    extra hour covers a retry that started just before the last one ended.
    """
    timeout = (timedelta(seconds=float(orchestration_timeout_seconds)) if orchestration_timeout_seconds
               else DEFAULT_ORCHESTRATION_TIMEOUT)
    return started_at + 3 * timeout + timedelta(hours=1)


def next_push_retry(now: datetime, attempts: int) -> datetime:
    """When a failed push start is tried again: 5 minutes, doubling, at most an hour (ADR #130 §8)."""
    return now + min(timedelta(minutes=5) * 2 ** max(attempts, 0), timedelta(hours=1))


def catch_up_due(now: datetime, last: Optional[datetime]) -> bool:
    """Whether the hourly catch-up should run (it always may when it never ran)."""
    return last is None or now - last >= CATCH_UP_EVERY


def primary_path_missed(enabled_at: Optional[datetime], due: datetime) -> bool:
    """Whether the task could run before the sweeper's due time, so its wake-up hint was lost."""
    return enabled_at is not None and enabled_at < due
