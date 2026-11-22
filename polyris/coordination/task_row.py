"""A task row of 1.3: its key and its fields (ADR #137 §3, §8).

``Start_Run`` writes one per task, the dispatcher decides from it and
``run_task`` records the work under its generation. Besides its status and
generation, a row carries only what the dispatcher decides on, as one JSON
text (``READINESS``), the spec version and its text's hash, the times its
deadlines read (``row_times``), and the fields the console shows. Every row
carries its run's execution name and ``StartTime``: a pass or a stop acts only
on the rows of its own run, and ``Start_Run`` accepts an existing row only when
it is its own run's.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional

from . import ownership, run_row, task_spec
from .deadlines import RowTimes
from .records import DAG_SNAPSHOT_PREFIX
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
READINESS = task_spec.ROW_READINESS
#: The fence of every write of a task row (ADR #137 §3, Rule 47): under the generation the writer holds. No branch
#: for a row without a generation, which 1.2's fence (ownership.FENCE) had.
FENCE = "#g = :g"
#: The index of a run's rows by the run's execution name (template.yaml). A pass reads it only for a run whose run
#: row is missing, which names no spec version to find them by (invariant 11).
RUN_ROWS_INDEX = "pipeline-execution-index"
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
# What run_task writes on the row (its template; a test compares the two).
RUNNING_AT = "running_at"
ATTEMPT = "attempt"                         # the task's runs, counted across Restarts
RETRY_ATTEMPT = "retry_attempt"
RUN_TASK_ARN = "run_task_helper_arn"        # the row's run_task execution, which Stop stops
TASK_EXECUTION_ARN = "task_execution_arn"
WAIT_DELAY_STARTED = "wait_delay_started_ms"
WAIT_DELAY_UNTIL = "wait_delay_until_ms"
NOTIFICATION_FAILED = "notification_failed"
#: What a run of the task writes on its row after its wait: the dispatcher's decisions, run_task and an
#: operator's action. A Restart and a reset remove them, so the row waits again as Start_Run wrote it; the
#: attempt count stays (ADR #137 §3).
RUN_FIELDS = (ENABLED_AT, COUNTDOWN_END, START_FAILED_FIRST, START_FAILED_LAST, START_FAILURES, LAST_CHECK, CHECKS,
              DECISION_SINCE, DECISION_TIMEOUT, REASONS, ERROR, TASK_RUN_ID, SKIP_ORIGIN, FINISHED_AT, RUNNING_AT,
              RETRY_ATTEMPT, RUN_TASK_ARN, TASK_EXECUTION_ARN, WAIT_DELAY_STARTED, WAIT_DELAY_UNTIL,
              NOTIFICATION_FAILED)


def short(pipeline_execution: str) -> str:
    """The run's short name in its rows' keys: its last 20 characters, without '.' and ':'."""
    return pipeline_execution[-SHORT_LENGTH:].replace(".", "").replace(":", "")


def is_task_key(name: str) -> bool:
    """Whether ``name`` keys a task row: not one of the platform's own rows (``_``), a store (``output#``,
    ``input#``) or a 1.2 run's DAG snapshot. A task_id may not start with ``_`` (``limits.spec_errors``)."""
    return not name.startswith("_") and "#" not in name and not name.startswith(DAG_SNAPSHOT_PREFIX)


def run_rows(client: Any, table: str, pipeline_execution: str) -> List[str]:
    """The keys of the task rows that ``RUN_ROWS_INDEX`` files under a run, every page of them. The index is read
    eventually: a row it does not show yet waits for a later read."""
    kwargs: Dict[str, Any] = {
        "TableName": table, "IndexName": RUN_ROWS_INDEX, "KeyConditionExpression": "#run = :run",
        "ExpressionAttributeNames": {"#run": PIPELINE_EXECUTION},
        "ExpressionAttributeValues": {":run": {"S": pipeline_execution}}, "ProjectionExpression": TABLE_KEY}
    keys: List[str] = []
    while True:
        page = client.query(**kwargs)
        keys += [str(item[TABLE_KEY]["S"]) for item in page.get("Items", []) if is_task_key(str(item[TABLE_KEY]["S"]))]
        if "LastEvaluatedKey" not in page:
            return keys
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def key(task: str, date: str, pipeline_execution: str) -> str:
    """The key of ``task``'s row in the run named ``pipeline_execution``."""
    return f"{task}-{date}-{short(pipeline_execution)}"


def of_run(item: Mapping[str, Any], pipeline_execution: str, run_start: str) -> bool:
    """Whether a row read back is the given run's: its execution's name and ``StartTime``."""
    return item.get(PIPELINE_EXECUTION) == pipeline_execution and item.get(RUN_START) == run_start


def row_times(item: Mapping[str, Any]) -> RowTimes:
    """What a row read back gives its deadlines (``deadlines.due_time``)."""
    readiness = task_spec.readiness_from_text(str(item[READINESS]))
    timeout = readiness.get("orchestration_timeout")
    decision_timeout = item.get(DECISION_TIMEOUT)
    return RowTimes(
        status=str(item[STATUS]),
        waiting_since=_moment(item, WAITING_SINCE),
        enabled_at=_moment(item, ENABLED_AT),
        has_wait_for=bool(readiness.get("wait_for")),
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
