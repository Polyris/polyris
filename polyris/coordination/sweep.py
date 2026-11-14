"""Rules of the sweeper, the periodic re-check from facts (ADR #130 §8).

The sweeper runs every 5 minutes. It re-checks each waiting task at its due
time, so a lost wake-up hint delays a task instead of losing it. These are
its pure rules; the Lambda in sam/lambdas/coordination applies them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List, Mapping, Optional

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
#: The engine's own row: the self-check, catch-up progress and the adoption cursors.
HEALTH_KEY = "_engine_health"
#: A push start that keeps failing transiently is given up after this long, with a warning.
PUSH_RETRY_WINDOW = timedelta(hours=24)
#: How often the catch-up replays the push branch for recent facts, and how far back it reads.
CATCH_UP_EVERY = timedelta(hours=1)
CATCH_UP_WINDOW = timedelta(hours=48)
#: The row shapes the adoption scans bring older rows to (ADR #130 §8). Raise it when
#: a release changes what a task row or a fact must carry; the scans then run again.
ADOPTION_GENERATION = 1
#: After the task-row scan first finishes, it repeats for this long: rows the old
#: definitions wrote while the stack update was still running get adopted too.
ADOPTION_REPEAT = timedelta(hours=1)

#: DescribeExecution is eventually consistent: a wrapper that registered less than this long ago may
#: not be visible yet, so "does not exist" means gone only for an older row.
EXECUTION_VISIBLE_AFTER = timedelta(minutes=2)
#: The console calls the engine unhealthy when the sweeper has not reported for this long,
STALE_AFTER = timedelta(minutes=15)
#: or when a waiting task has been due for longer than the sweeper's 40-minute target.
BEHIND_AFTER = timedelta(minutes=40)
#: The event source mapping and stream states that deliver facts.
MAPPING_ENABLED = "Enabled"
STREAM_ENABLED = "ENABLED"
#: How the mapping's LastProcessingResult begins when its last call to AssetFanout failed.
MAPPING_PROBLEM = "PROBLEM"
#: The steps of one sweeper run, in order, and the words the console uses for them.
#: A step that fails is reported in `_engine_health`; the others and the self-check still run.
#: The name a failed self-check reading is reported under, in `failed_steps`.
SELF_CHECK = "self_check"

STEPS = {
    "due_rows": "re-checking waiting tasks",
    "paused_rows": "releasing tasks held by a pause",
    "push_retries": "retrying push starts",
    "catch_up": "the hourly push catch-up",
    "adopt": "adopting rows an older version wrote",
}


#: A wrapper started before the upgrade retries its dependency wait twice on a
#: timeout, so it may wait three timeouts; a current wrapper waits one (D41).
LEGACY_WAITS = 3


def liveness_deadline(started_at: datetime, orchestration_timeout_seconds: Optional[float], *,
                      legacy: bool = False) -> datetime:
    """When a waiting task's wrapper is checked even without a signal (ADR #130 §3).

    The wrapper's dependency wait lasts one orchestration timeout, or three for
    a ``legacy`` (pre-upgrade) wrapper that still retries it. The extra hour
    covers a wait that started just before the row's ``started_at`` was written.
    """
    timeout = (timedelta(seconds=float(orchestration_timeout_seconds)) if orchestration_timeout_seconds
               else DEFAULT_ORCHESTRATION_TIMEOUT)
    return started_at + (LEGACY_WAITS if legacy else 1) * timeout + timedelta(hours=1)


def next_push_retry(now: datetime, attempts: int) -> datetime:
    """When a failed push start is tried again: 5 minutes, doubling, at most an hour (ADR #130 §8)."""
    return now + min(timedelta(minutes=5) * 2 ** max(attempts, 0), timedelta(hours=1))


def catch_up_due(now: datetime, last: Optional[datetime]) -> bool:
    """Whether the hourly catch-up should run (it always may when it never ran)."""
    return last is None or now - last >= CATCH_UP_EVERY


def engine_problems(health: Optional[Mapping[str, Any]], now: datetime) -> List[str]:
    """Why the coordination engine is unhealthy, from the sweeper's `_engine_health` row (ADR #130 §9).

    Empty means healthy. The console shows these as a banner. Time fields are
    UTC timestamps as `times.format_utc` writes them.
    """
    from .times import parse_utc

    if not health or not health.get("checked_at"):
        return ["The sweeper has not reported yet: waiting tasks are not re-checked until it does."]
    problems = []
    silent = now - parse_utc(str(health["checked_at"]))
    if silent > STALE_AFTER:
        problems.append(f"The sweeper last reported {int(silent.total_seconds() // 60)} minutes ago: "
                        "waiting tasks are not re-checked.")
    mapping = str(health.get("fanout_mapping", ""))
    if mapping != MAPPING_ENABLED:
        problems.append(f"Asset events are not delivered: the stream mapping is {mapping or 'missing'}.")
    result = str(health.get("fanout_result", ""))
    if result.startswith(MAPPING_PROBLEM):
        problems.append(f"Asset events are not delivered: the stream mapping's last call failed ({result}).")
    stream = str(health.get("stream", ""))
    if stream != STREAM_ENABLED:
        problems.append(f"Asset events are not recorded for delivery: the table stream is {stream or 'missing'}.")
    overdue = health.get("oldest_overdue")
    if overdue and now - parse_utc(str(overdue)) > BEHIND_AFTER:
        behind = int((now - parse_utc(str(overdue))).total_seconds() // 60)
        problems.append(f"Waiting tasks are {behind} minutes behind their checks.")
    for failure in health.get("failed_steps") or []:
        step, _, error = str(failure).partition(": ")
        name = "its self-check" if step == SELF_CHECK else STEPS.get(step, step)
        problems.append(f"The sweeper could not finish {name} on its last run: {error}")
    return problems


def primary_path_missed(enabled_at: Optional[datetime], due: datetime) -> bool:
    """Whether the task could run before the sweeper's due time, so its wake-up hint was lost."""
    return enabled_at is not None and enabled_at < due
