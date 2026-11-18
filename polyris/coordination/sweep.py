"""Rules of the sweeper, the periodic re-check from facts (ADR #130 §8).

The sweeper runs every 5 minutes. It re-checks each waiting task at its due
time, so a lost wake-up hint delays a task instead of losing it. These are
its pure rules; the Lambda in sam/lambdas/coordination applies them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

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
#: What the self-check writes when the asset-fanout function has no stream mapping.
MAPPING_MISSING = "missing"
MAPPING_DISABLED = "Disabled"
#: Mapping states AWS passes through while it creates, enables or updates a mapping. Seen within
#: STALE_AFTER of the mapping's last change, they are a deploy at work, not a delivery that is off.
MAPPING_SETTLING = frozenset({"Creating", "Enabling", "Updating"})
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


#: A row whose stream pass may still be under way is no lost hint: longer than the stream's batching
#: window and its retries of a failed batch (ADR #137 §7).
LOST_HINT_GRACE = timedelta(minutes=5)


def lost_hint(enabled_at: Optional[datetime], check_time: datetime, *, left_at: bool, countdown_ended: bool) -> bool:
    """Whether a row whose wait a run row's check ends had lost its hint (ADR #137 §7).

    Only when its enabling time is older than the grace: neither a row whose
    countdown ended (the sweeper is how a countdown ends) nor a row found while
    the run row carries ``left_at`` (a pass left it for its budget) counts.
    """
    if left_at or countdown_ended or enabled_at is None:
        return False
    return enabled_at < check_time - LOST_HINT_GRACE


def failed_step(passes: int, failed: int) -> bool:
    """Whether a sweep reports a failed step: more than half of its passes failed, and at least two (ADR #137 §7).

    One broken run alone does not turn the System status red; its run's warning shows it.
    """
    return failed >= 2 and failed * 2 > passes


def next_push_retry(now: datetime, attempts: int) -> datetime:
    """When a failed push start is tried again: 5 minutes, doubling, at most an hour (ADR #130 §8)."""
    return now + min(timedelta(minutes=5) * 2 ** max(attempts, 0), timedelta(hours=1))


def catch_up_due(now: datetime, last: Optional[datetime]) -> bool:
    """Whether the hourly catch-up should run (it always may when it never ran)."""
    return last is None or now - last >= CATCH_UP_EVERY


def unknown_state(detail: str) -> str:
    """The state the self-check writes for a reading it could not make: ``unknown (<detail>)``."""
    return f"unknown ({detail})"


def _unknown_detail(state: str) -> Optional[str]:
    """The detail of a state `unknown_state` wrote, or None for any other state."""
    if state.startswith("unknown (") and state.endswith(")"):
        return state[len("unknown ("):-1]
    return None


def ago(delta: timedelta) -> str:
    """How long ago, as a finding says it: ``N min ago`` under 120 minutes, ``N h ago`` under
    48 hours, else ``N days ago``, each floored. A finding never holds an absolute time,
    because the reader's time zone is unknown."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def _finding(title: str, *, impact: str, fix: str, resource: str = "", changed_at: str = "",
             command: str = "") -> Dict[str, str]:
    """One finding, its fields in the console's order; an empty optional field is left out."""
    fields = {"title": title, "resource": resource, "changed_at": changed_at, "impact": impact, "fix": fix,
              "command": command}
    return {key: value for key, value in fields.items() if value}


def _command(*lines: str, needs: Sequence[str]) -> str:
    """The shell lines, only when every name they use, the region included, is known."""
    return "\n".join(lines) if all(needs) else ""


def _logs(function: str, region: str) -> str:
    return f"aws logs tail /aws/lambda/{function} --region {region} --since 1h"


def _settling(mapping: str, report: Mapping[str, Any], now: datetime) -> bool:
    """Whether the mapping is still being created, enabled or updated, and changed recently."""
    from .times import parse_utc

    changed = report.get("fanout_mapping_changed_at")
    return mapping in MAPPING_SETTLING and bool(changed) and now - parse_utc(str(changed)) <= STALE_AFTER


def engine_findings(report: Optional[Mapping[str, Any]], now: datetime) -> List[Dict[str, str]]:
    """What is wrong with the coordination engine, from the sweeper's `_engine_health` row (ADR #130 §9).

    Empty means healthy. Each finding is final English that the console shows as
    is: a ``title``, constant for its clause, and when they apply the
    ``resource`` it names, when AWS last ``changed_at`` it, the ``impact``, the
    ``fix``, and a ``command`` that is there only when every name it uses is
    known. A name the report lacks reads in words. The clauses come in a fixed
    order; without a report only the first applies. Time fields are UTC
    timestamps as `times.format_utc` writes them; one that is not raises
    ValueError, and the console reports the row as unreadable.
    """
    from .times import parse_utc

    if not report or not report.get("checked_at"):
        return [_finding(
            "No check has run yet",
            impact="Waiting tasks are not re-checked until the periodic re-check runs. "
                   "It first runs within 5 minutes of an install or upgrade.",
            fix="If this stays for more than 10 minutes, check in EventBridge Scheduler that the schedule "
                "whose name ends in -polyris-sweeper is enabled.")]
    sweeper, fanout, table, uuid, region = (str(report.get(field) or "") for field in (
        "sweeper_function", "fanout_function", "events_table", "fanout_mapping_uuid", "region"))
    the_sweeper = sweeper or "the sweeper function"
    the_fanout = fanout or "the asset-fanout function"
    the_table = table or "the asset-events table"
    sweeper_logs = _command(_logs(sweeper, region), needs=(sweeper, region))
    findings = []

    silent = now - parse_utc(str(report["checked_at"]))
    stale = silent > STALE_AFTER
    if stale:
        findings.append(_finding(
            "The periodic re-check is not running",
            resource=f"The last run of {the_sweeper} was {ago(silent)}. It runs every 5 minutes.",
            impact="Tasks still start from their signals. A task that missed its signal is not re-checked "
                   "and waits until it times out.",
            fix="Check that its schedule in EventBridge Scheduler is enabled. If it is, read the function's logs.",
            command=_command(f"aws scheduler get-schedule --name {sweeper} --region {region} --query State",
                             _logs(sweeper, region), needs=(sweeper, region))))

    late = ("Tasks that wait for an asset, and pipelines scheduled on an asset, do not start while the "
            "periodic re-check is also not running." if stale else
            "Tasks that wait for an asset start up to 40 minutes late, and pipelines scheduled on an asset "
            "up to an hour late.")
    # An empty mapping state reads as no mapping, as an empty stream state reads `missing` below.
    mapping = str(report.get("fanout_mapping") or MAPPING_MISSING)
    unread = _unknown_detail(mapping)
    if mapping == MAPPING_MISSING:
        findings.append(_finding(
            "Asset event delivery is not set up",
            resource=f"There is no stream mapping for {the_fanout}.",
            impact=late,
            fix=f"Recreate the trigger as the platform template defines it: in the Lambda console, open {the_fanout}, "
                f"then Add trigger → DynamoDB → the stream of {the_table}, starting position Trim horizon, batch size "
                '10, filter {"eventName": ["INSERT"]}. Deploying the same version again does not restore it.'))
    elif unread is not None:
        findings.append(_finding(
            "Asset event delivery could not be checked",
            resource=f"Reading the stream mapping of {the_fanout} failed ({unread}).",
            impact="If delivery is off, tasks that wait for an asset start late.",
            fix="Check that the sweeper's role may call lambda:ListEventSourceMappings, then read the re-check's logs.",
            command=sweeper_logs))
    elif mapping != MAPPING_ENABLED and not _settling(mapping, report, now):
        findings.append(_finding(
            "Asset event delivery is off",
            resource=f"The stream mapping of {the_fanout} is {mapping}.",
            changed_at=str(report.get("fanout_mapping_changed_at") or ""),
            impact=late,
            fix=f"Re-enable the mapping: in the Lambda console, open {the_fanout}, then Configuration → Triggers.",
            # AWS refuses to enable a mapping that is still changing, so only a disabled one gets the command.
            command=_command(f"aws lambda update-event-source-mapping --uuid {uuid} --enabled --region {region}",
                             needs=(uuid, region)) if mapping == MAPPING_DISABLED else ""))

    result = str(report.get("fanout_result") or "")
    if result.startswith(MAPPING_PROBLEM):
        findings.append(_finding(
            "Asset event delivery is failing",
            resource=f"The last call of the stream mapping of {the_fanout} failed: {result}.",
            impact="Tasks that wait for an asset start up to 40 minutes late. The failed records are retried "
                   "until they succeed or expire after 24 hours.",
            fix=f"Read the logs of {the_fanout}: the error there names the cause.",
            command=_command(_logs(fanout, region), needs=(fanout, region))))

    stream = str(report.get("stream") or "")
    unread = _unknown_detail(stream)
    if unread is not None:
        findings.append(_finding(
            "Asset event recording could not be checked",
            resource=f"Reading the stream of {the_table} failed ({unread}).",
            impact="If the stream is off, tasks that wait for an asset start late.",
            fix="Check that the sweeper's role may call dynamodb:DescribeTable, then read the re-check's logs.",
            command=sweeper_logs))
    elif stream != STREAM_ENABLED:
        findings.append(_finding(
            "Asset events are not recorded for delivery",
            resource=f"The stream of {the_table} is {stream or 'missing'}.",
            impact="New facts wake no waiting task. The periodic re-check still starts them, up to 40 minutes late.",
            fix=f"Turn the stream on again (view type NEW_IMAGE). That makes a new stream: add a trigger on it to "
                f"{the_fanout}, then delete the trigger on the old one."))

    overdue = report.get("oldest_overdue")
    if overdue:
        behind = now - parse_utc(str(overdue))
        if behind > BEHIND_AFTER:
            findings.append(_finding(
                "Waiting tasks are behind their checks",
                resource=f"The oldest check was due {ago(behind)}.",
                impact="Tasks that could run start late.",
                fix="Read the re-check's logs: it cannot keep up, or it fails on some rows.",
                command=sweeper_logs))

    for failure in report.get("failed_steps") or []:
        step, _, error = str(failure).partition(": ")
        name = "its self-check" if step == SELF_CHECK else STEPS.get(step, step)
        findings.append(_finding(
            f"The periodic re-check could not finish {name}",
            resource=error,
            impact="The other steps still ran. The next run, within 5 minutes, tries again.",
            fix="If this stays, the error names the cause; the re-check's logs have the full message.",
            command=sweeper_logs))
    return findings


def primary_path_missed(enabled_at: Optional[datetime], due: datetime) -> bool:
    """Whether the task could run before the sweeper's due time, so its wake-up hint was lost."""
    return enabled_at is not None and enabled_at < due
