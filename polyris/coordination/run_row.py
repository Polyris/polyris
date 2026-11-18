"""The run row: the one row a run keeps besides its task rows (ADR #137 §3).

Its key is ``_run_`` followed by the run's ``pipeline_execution``; the run's input
is an item of its own under the same key and ``#input``. Both start with ``_``,
so every console loop over task rows skips them (``is_internal_record``). These
names are the contract that ``Start_Run``, the dispatcher, the sweeper and the
console share (§9).

The run row carries no ``date``, ``pipeline_execution`` or ``backfill_id``, so
it enters neither date index; ``start_day`` alone puts it in
``run-start-index``, and its due time in ``attention-index`` (§3, §5).
"""

from __future__ import annotations

from ..constants import BackfillCascade

#: The tasks table's partition key, which every row carries.
TABLE_KEY = "execution_name"
KEY_PREFIX = "_run_"
INPUT_SUFFIX = "#input"

# A run row is only ever `creating` or `active`; nothing leads back to `creating` (§3).
STATUS = "status"
CREATING = "creating"
ACTIVE = "active"

TOKEN = "run_token"              # the token of Start_Run's latest attempt: the run's only token
TOKEN_ORDER = "token_order"      # that attempt's order: Execution.RedriveCount first, then State.RetryCount
ANSWERED = "answered_token"      # the token the dispatcher answered: the run's answer is marked
STOP = "stop"                    # the stop mark: who asked, why and when
STOP_DONE = "stop_done"          # when the stop was done: every row final and the answer marked
RUN_VERSION = "run_version"      # raised by each write of §3's table, checked by every decision
LEFT_AT = "left_at"              # when a pass last left rows, for its budget or a conflict (§7)
EXECUTION = "run_execution"      # the pipeline execution's name
PIPELINE = "pipeline_name"
RUN_DATE = "run_date"            # the run's logical date
START_DAY = "start_day"          # YYYY-MM-DD of the execution's start: run-start-index's partition key
STARTED_AT = "started_at"        # the execution's StartTime: run-start-index's sort key, always a string
FINISHED_AT = "finished_at"      # written with the answer
TRIGGERED_BY = "triggered_by"
SPEC_ID = "spec_id"              # the spec version the run runs (§8)
TASK_COUNT = "task_count"
FORMAT = "format_version"
SUMMARY = "summary"              # the count of the run's rows per status (§4)
STEP_FAILURE = "step_failure"    # the mark of a failed direct step (§1)
REQUESTS = "requests"            # the heavy actions in progress, by kind (§3)
PAUSES = "pauses"                # the pause intervals, at most ten (§5)
TTL = "ttl"

# The run's input item (``input_key``): Start_Run writes it once; the dispatcher reads it to build
# run_task's input, Mark Successful for the cascade, and the console for what it shows (§3).
VARIABLES = "variables"
SKIP_TASKS = "skip_tasks"
CASCADE = "cascade"              # how the run's facts reach their consumers: a BackfillCascade value (ADR #57)

#: The error with which the dispatcher answers a failed run (§9). The generated pipeline takes it in a
#: ``Catch`` ahead of ``Start_Run``'s, without a warning, and no ``Retry`` of ``Start_Run`` matches it.
FAILED_RUN_ERROR = "Polyris.RunFailed"


def key(pipeline_execution: str) -> str:
    """The run row's key."""
    return f"{KEY_PREFIX}{pipeline_execution}"


def input_key(pipeline_execution: str) -> str:
    """The key of the run's input item."""
    return f"{key(pipeline_execution)}{INPUT_SUFFIX}"


def cascade(*, suppress_asset_event: bool, cascade_all: bool) -> str:
    """The run's cascade from the flags its starter sets (``bulk_backfill``, ADR #57): ``none`` wins."""
    if suppress_asset_event:
        return BackfillCascade.NONE
    return BackfillCascade.ALL if cascade_all else BackfillCascade.AUTO


def is_run_row(row_key: str) -> bool:
    """Whether ``row_key`` names a run row, not a run's input or a task row."""
    return row_key.startswith(KEY_PREFIX) and not row_key.endswith(INPUT_SUFFIX)
