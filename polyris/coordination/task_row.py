"""A task row of 1.3: its key and its fields (ADR #137 §3, §8).

``Start_Run`` writes one per task, the dispatcher decides from it and
``run_task`` records the work under its generation. Besides its status and
generation, a row carries only what the dispatcher decides on, as one JSON
text (``DECISION``), the spec version and its text's hash, the times its
deadlines read (``row_times``), and the fields the console shows. Every row
carries its run's execution name and ``StartTime``: a pass or a stop acts only
on the rows of its own run, and ``Start_Run`` accepts an existing row only when
it is its own run's.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping, Optional

from . import ownership, run_row, task_spec
from .deadlines import RowTimes
from .times import parse_utc

# The key: {task}-{date}-{the last 20 characters of the run's name, without '.' and ':'}, as 1.2's.
TABLE_KEY = run_row.TABLE_KEY
SHORT_LENGTH = 20

STATUS = "status"
GENERATION = ownership.FIELD
SKIP_ORIGIN = "skip_origin"
TASK_NAME = "task_name"
PIPELINE = "pipeline_name"
PIPELINE_EXECUTION = "pipeline_execution"   # the run's execution name
RUN_START = "run_start"                     # the run's execution StartTime, as Step Functions gives it
DATE = "date"
TASK_DATE = "task_date"                     # the date again: attention-index projects it, and not `date` (§6)
SPEC_ID = run_row.SPEC_ID
SPEC_HASH = task_spec.ROW_TEXT_HASH
DECISION = task_spec.ROW_DECISION
TRIGGERED_BY = "triggered_by"
BACKFILL_ID = "backfill_id"
TASK_TYPE = "task_type"
TASK_RUN_ID = "task_run_id"                 # the name of the row's run_task execution (invariant 4)
STARTED_AT = "started_at"
FINISHED_AT = "finished_at"
TTL = "ttl"
# The times the deadlines read (deadlines.RowTimes), all UTC strings but the counters.
WAITING_SINCE = "waiting_since"
ENABLED_AT = "enabled_at"
COUNTDOWN_END = "countdown_end"
START_FAILED_FIRST = "start_failed_first"
START_FAILED_LAST = "start_failed_last"
START_FAILURES = "start_failures"
STATUS_SINCE = "status_since"
LAST_CHECK = "last_check"
CHECKS = "checks"
DECISION_SINCE = "decision_since"
DECISION_TIMEOUT = "decision_timeout"       # seconds, the timeout in force when the wait began
REASONS = "reasons"                         # why the row waits, with no time in them
ERROR = "error"


def short(pipeline_execution: str) -> str:
    """The run's short name in its rows' keys: its last 20 characters, without '.' and ':'."""
    return pipeline_execution[-SHORT_LENGTH:].replace(".", "").replace(":", "")


def key(task: str, date: str, pipeline_execution: str) -> str:
    """The key of ``task``'s row in the run named ``pipeline_execution``."""
    return f"{task}-{date}-{short(pipeline_execution)}"


def of_run(item: Mapping[str, Any], pipeline_execution: str, run_start: str) -> bool:
    """Whether a row read back is the given run's: its execution's name and ``StartTime``."""
    return item.get(PIPELINE_EXECUTION) == pipeline_execution and item.get(RUN_START) == run_start


def row_times(item: Mapping[str, Any]) -> RowTimes:
    """What a row read back gives its deadlines (``deadlines.due_time``)."""
    decision = task_spec.decision_from_text(str(item[DECISION]))
    timeout = decision.get("orchestration_timeout")
    decision_timeout = item.get(DECISION_TIMEOUT)
    return RowTimes(
        status=str(item[STATUS]),
        waiting_since=_moment(item, WAITING_SINCE),
        enabled_at=_moment(item, ENABLED_AT),
        has_wait_for=bool(decision.get("wait_for")),
        orchestration_timeout=None if timeout is None else timedelta(seconds=int(timeout)),
        countdown_end=_moment(item, COUNTDOWN_END),
        start_failed_first=_moment(item, START_FAILED_FIRST),
        start_failed_last=_moment(item, START_FAILED_LAST),
        start_failures=int(item.get(START_FAILURES, 0)),
        status_since=_moment(item, STATUS_SINCE),
        last_check=_moment(item, LAST_CHECK),
        checks=int(item.get(CHECKS, 0)),
        decision_since=_moment(item, DECISION_SINCE),
        decision_timeout=None if decision_timeout is None else timedelta(seconds=int(decision_timeout)),
    )


def _moment(item: Mapping[str, Any], name: str) -> Optional[Any]:
    value = item.get(name)
    return None if value is None else parse_utc(str(value))
