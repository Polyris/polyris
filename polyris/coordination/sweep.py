"""Rules of the sweeper, the periodic check from facts (ADR #130 §8, ADR #137 §6, §7).

The sweeper runs every 5 minutes. It takes the due entries of the ``waiting``
and ``decision`` kinds, run rows included, groups them by run, and makes one
full pass of the dispatcher over each run: a lost wake-up hint delays a task
instead of losing it. These are its pure rules and the findings of its
self-check; the Lambda in sam/lambdas/coordination applies them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: The ``source`` of the sweeper's passes, as the dispatcher logs it.
SOURCE = "sweeper"
#: The row that holds the lease; ``_``-prefixed rows are internal (CLAUDE.md).
LEASE_KEY = "_sweeper_lease"
#: How long a run holds the lease. Longer than the function's 240 s timeout,
#: shorter than the 5-minute schedule, so a crashed run never blocks the next.
LEASE = timedelta(seconds=270)
#: The work budget of one run, inside the function's 240 s timeout.
BUDGET = timedelta(seconds=200)
#: The engine's own row: the self-check and the catch-up's progress.
HEALTH_KEY = "_engine_health"
#: A push start that keeps failing transiently is given up after this long, with a warning.
PUSH_RETRY_WINDOW = timedelta(hours=24)
#: How often the catch-up replays the push branch for recent facts, and how far back it reads.
CATCH_UP_EVERY = timedelta(hours=1)
CATCH_UP_WINDOW = timedelta(hours=48)

#: DescribeExecution is eventually consistent: an execution started less than this long ago may not be
#: visible yet, so "does not exist" means gone only for an older row or run.
EXECUTION_VISIBLE_AFTER = timedelta(minutes=2)
#: The console calls the engine unhealthy when the sweeper has not reported for this long,
STALE_AFTER = timedelta(minutes=15)
#: or when a waiting task has been due for longer than the sweeper's 40-minute target.
BEHIND_AFTER = timedelta(minutes=40)
#: The event source mapping and stream states that deliver facts and task changes.
MAPPING_ENABLED = "Enabled"
STREAM_ENABLED = "ENABLED"
#: What the self-check writes when a function has no stream mapping.
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
    "due_runs": "checking the runs that are due",
    "push_retries": "retrying push starts",
    "catch_up": "the hourly push catch-up",
}
#: How many task names a run's lost-hint warning lists (ADR #137 §7).
LOST_HINT_NAMES = 5


#: A row whose stream pass may still be under way is no lost hint: longer than the stream's batching
#: window and its retries of a failed batch (ADR #137 §7).
LOST_HINT_GRACE = timedelta(minutes=5)


def lost_hint(enabled_at: Optional[datetime], check_time: datetime, *, left_at: bool, countdown_ended: bool,
              resumed_at: Optional[datetime] = None) -> bool:
    """Whether a row whose wait a run row's check ends had lost its hint (ADR #137 §5, §7).

    ``enabled_at`` is ``deadlines.enabled_since``, never before the wait began,
    so a lost activation counts too; this check alone also reads the latest
    resume, and an enabling time is never before it. Only a time older than the
    grace counts: neither a row whose countdown ended (the sweeper is how a
    countdown ends) nor a row found while the run row carries ``left_at`` (a
    pass left it for its budget) does.
    """
    if left_at or countdown_ended or enabled_at is None:
        return False
    effective = max(enabled_at, resumed_at) if resumed_at is not None else enabled_at
    return effective < check_time - LOST_HINT_GRACE


def failed_step(passes: int, failed: int) -> bool:
    """Whether a sweep reports a failed step: more than half of its passes failed, and at least two (ADR #137 §7).

    One broken run alone does not turn the System status red; its run's warning shows it.
    """
    return failed >= 2 and failed * 2 > passes


def failed_passes(passes: int, failed: int, first_cause: str) -> str:
    """What the failed step says of a sweep whose passes failed (``failed_step``)."""
    return f"{failed} of {passes} passes over runs failed; the first: {first_cause}"


def lost_hints_warning(tasks: Sequence[str]) -> str:
    """The one ``sweeper`` warning of a run whose check found lost hints: their number and a few names (§7)."""
    if not tasks:
        raise ValueError("a lost-hint warning names at least one task")
    names = sorted(tasks)
    shown = ", ".join(names[:LOST_HINT_NAMES]) + (f" and {len(names) - LOST_HINT_NAMES} more"
                                                  if len(names) > LOST_HINT_NAMES else "")
    tasks_word = "task" if len(names) == 1 else "tasks"
    return (f"The periodic check found {len(names)} {tasks_word} of this run that could have been decided "
            f"earlier: {shown}. Their wake-up hint was lost on the way, so they waited for this check.")


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


def _settling(mapping: str, changed: Any, now: datetime) -> bool:
    """Whether the mapping is still being created, enabled or updated, and changed recently."""
    from .times import parse_utc

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
            impact="Tasks still start from the streams. A task whose wake-up was lost waits, and no limit, countdown "
                   "or decision timeout ends, until the check runs again.",
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
    elif mapping != MAPPING_ENABLED and not _settling(mapping, report.get("fanout_mapping_changed_at"), now):
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

    findings += _task_delivery(report, now, stale=stale, sweeper_logs=sweeper_logs)

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

    lost = int(report.get("lost_hint_runs") or 0)
    if lost > 0:
        dispatcher = str(report.get("dispatcher_function") or "")
        findings.append(_finding(
            "Runs missed their wake-ups",
            resource=f"The last check found {lost} {'run' if lost == 1 else 'runs'} with tasks whose wake-up was lost.",
            impact="Those tasks started or finished at their run's periodic check, up to 30 minutes late. A record "
                   "that keeps failing in the tasks stream holds back the runs of its shard for up to 24 hours.",
            fix=f"Read the logs of {dispatcher or 'the dispatcher function'}, which the tasks stream calls: a record "
                "that keeps failing names its cause there. The bell names the runs.",
            command=_command(_logs(dispatcher, region), needs=(dispatcher, region))))

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


def _task_delivery(report: Mapping[str, Any], now: datetime, *, stale: bool,
                   sweeper_logs: str) -> List[Dict[str, str]]:
    """The findings on the tasks table's stream and its mapping to the dispatcher (ADR #137 §6)."""
    dispatcher, table, uuid, region = (str(report.get(field) or "") for field in (
        "dispatcher_function", "tasks_table", "dispatch_mapping_uuid", "region"))
    the_dispatcher = dispatcher or "the dispatcher function"
    the_table = table or "the tasks table"
    late = ("Finished tasks do not start their dependants, and runs do not end, while the periodic check is also "
            "not running." if stale else
            "Finished tasks start their dependants, and runs end, only at their run's periodic check, up to 30 "
            "minutes late.")
    findings: List[Dict[str, str]] = []
    mapping = str(report.get("dispatch_mapping") or MAPPING_MISSING)
    unread = _unknown_detail(mapping)
    if mapping == MAPPING_MISSING:
        findings.append(_finding(
            "Task delivery is not set up",
            resource=f"There is no stream mapping for {the_dispatcher}.",
            impact=late,
            fix=f"Recreate the trigger as the platform template defines it (TasksDispatchMapping): in the Lambda "
                f"console, open {the_dispatcher}, then Add trigger → DynamoDB → the stream of {the_table}, starting "
                "position Trim horizon, batch size 10, with the template's four filters. Deploying the same version "
                "again does not restore it."))
    elif unread is not None:
        findings.append(_finding(
            "Task delivery could not be checked",
            resource=f"Reading the stream mapping of {the_dispatcher} failed ({unread}).",
            impact="If delivery is off, tasks start late.",
            fix="Check that the sweeper's role may call lambda:ListEventSourceMappings, then read the re-check's logs.",
            command=sweeper_logs))
    elif mapping != MAPPING_ENABLED and not _settling(mapping, report.get("dispatch_mapping_changed_at"), now):
        findings.append(_finding(
            "Task delivery is off",
            resource=f"The stream mapping of {the_dispatcher} is {mapping}.",
            changed_at=str(report.get("dispatch_mapping_changed_at") or ""),
            impact=late,
            fix=f"Re-enable the mapping: in the Lambda console, open {the_dispatcher}, then Configuration → Triggers.",
            command=_command(f"aws lambda update-event-source-mapping --uuid {uuid} --enabled --region {region}",
                             needs=(uuid, region)) if mapping == MAPPING_DISABLED else ""))

    result = str(report.get("dispatch_result") or "")
    if result.startswith(MAPPING_PROBLEM):
        findings.append(_finding(
            "Task delivery is failing",
            resource=f"The last call of the stream mapping of {the_dispatcher} failed: {result}.",
            impact="Tasks of the runs in the failing records start late, at their run's periodic check. The failed "
                   "records are retried until they succeed or expire after 24 hours.",
            fix=f"Read the logs of {the_dispatcher}: the error there names the cause.",
            command=_command(_logs(dispatcher, region), needs=(dispatcher, region))))

    stream = str(report.get("tasks_stream") or "")
    unread = _unknown_detail(stream)
    if unread is not None:
        findings.append(_finding(
            "Task changes could not be checked",
            resource=f"Reading the stream of {the_table} failed ({unread}).",
            impact="If the stream is off, tasks start late.",
            fix="Check that the sweeper's role may call dynamodb:DescribeTable, then read the re-check's logs.",
            command=sweeper_logs))
    elif stream != STREAM_ENABLED:
        findings.append(_finding(
            "Task changes are not recorded for delivery",
            resource=f"The stream of {the_table} is {stream or 'missing'}.",
            impact="Finished tasks wake no dependant. The periodic check still decides them, up to 30 minutes late.",
            fix=f"Turn the stream on again (view type NEW_AND_OLD_IMAGES). That makes a new stream: add a trigger on "
                f"it to {the_dispatcher} as the template defines it, then delete the trigger on the old one."))
    return findings
