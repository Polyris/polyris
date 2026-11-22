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

from datetime import datetime
from typing import Dict, Optional

from .times import format_utc

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
ANSWERED = "answered_order"      # the token_order of the token the dispatcher answered: the answer is marked
STOP = "stop"                    # the stop mark: who asked, why and when
STOP_DONE = "stop_done"          # when the stop was done: every row final and the answer marked
RUN_VERSION = "run_version"      # raised by each write of §3's table, checked by every decision
LEFT_AT = "left_at"              # when a pass last left rows, for its budget or a conflict (§7)
EXECUTION = "run_execution"      # the pipeline execution's name
EXECUTION_ARN = "run_execution_arn"  # its ARN ($states.context.Execution.Id): the engine's check describes it (§4)
RUN_START = "run_start"          # its StartTime as Step Functions gives it: with the name, which run the row is
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
CHECKS = "checks"                # how many checks of the sweeper moved its due time, from which the next backs off
TTL = "ttl"

#: The sparse index of run rows by the UTC day their execution started (§5), which the run lists read, and what
#: it projects besides its keys: what the lists show (template.yaml; a parity test pins both). A change of its
#: projection would replace the index.
START_INDEX = "run-start-index"
INDEX_FIELDS = (PIPELINE, RUN_DATE, STATUS, FINISHED_AT, TRIGGERED_BY, SUMMARY, STEP_FAILURE)

#: Every field of the run row besides its key, its summary's time and its due time; a test builds the
#: largest row from it, so a field added here is measured.
FIELDS = (STATUS, TOKEN, TOKEN_ORDER, ANSWERED, STOP, STOP_DONE, RUN_VERSION, LEFT_AT, EXECUTION, EXECUTION_ARN, RUN_START,
          PIPELINE,
          RUN_DATE, START_DAY, STARTED_AT, FINISHED_AT, TRIGGERED_BY, SPEC_ID, TASK_COUNT, FORMAT, SUMMARY,
          STEP_FAILURE, REQUESTS, PAUSES, CHECKS, TTL)

# The run row stays under SIZE_MAX (§3, D114): a Step Functions token takes up to 2,048 characters, and
# every other free-text field is capped in UTF-8 bytes, as DynamoDB counts them, by the builders below.
SIZE_MAX = 5 * 1024
TOKEN_MAX = 2048
STOP_REASON_MAX = 500
BY_MAX = 128
TRIGGERED_BY_MAX = 128
STEP_MAX = 80
ERROR_MAX = 128
#: A task id that a request names; polyris-deploy refuses a longer one (TR-3).
TASK_ID_MAX = 128
#: The kinds of request (§3): the renewal of the history TTL, and Restart with dependants' reset.
RENEW_TTL = "renew_ttl"
RESET_DEPENDANTS = "reset_dependants"

#: The error with which the dispatcher answers a failed run (§9). The generated pipeline takes it in a
#: ``Catch`` ahead of ``Start_Run``'s, without a warning, and no ``Retry`` of ``Start_Run`` matches it.
FAILED_RUN_ERROR = "Polyris.RunFailed"


def key(pipeline_execution: str) -> str:
    """The run row's key."""
    return f"{KEY_PREFIX}{pipeline_execution}"


def input_key(pipeline_execution: str) -> str:
    """The key of the run's input item, whose contract is ``run_input.py``."""
    return f"{key(pipeline_execution)}{INPUT_SUFFIX}"


# ``run_version`` (§1, §3). A decision's ConditionCheck, and a pass's own raise, require it to be what the
# pass read (``VERSION_UNCHANGED``); the raise then sets the next value (``VERSION_NEXT``, ``version_values``).
# An operator's action, a new token and a request raise it whatever it is (``VERSION_RAISE``).
VERSION_NAMES: Dict[str, str] = {"#rv": RUN_VERSION}
VERSION_UNCHANGED = "#rv = :rv"
VERSION_NEXT = "#rv = :rv_next"
VERSION_RAISE = "#rv = if_not_exists(#rv, :rv_zero) + :rv_one"
VERSION_RAISE_VALUES: Dict[str, int] = {":rv_zero": 0, ":rv_one": 1}


def version_values(read: int) -> Dict[str, int]:
    """The values of ``VERSION_UNCHANGED`` and ``VERSION_NEXT`` for a pass that read ``read``."""
    return {":rv": read, ":rv_next": read + 1}


#: Step Functions allows at most 99,999,999 attempts of a state, so a retry count stays below this.
_RETRIES = 100_000_000

#: Start_Run's write of its attempt's token: a later attempt's token replaces the stored one, the same
#: attempt may write its own again, an earlier one's never replaces a later one, and the stop mark refuses
#: them all (§1). With ``TOKEN_CONDITION_NAMES`` and ``:order``, the attempt's ``token_order``.
TOKEN_CONDITION = "attribute_not_exists(#stop) AND (attribute_not_exists(#order) OR #order <= :order)"
TOKEN_CONDITION_NAMES: Dict[str, str] = {"#stop": STOP, "#order": TOKEN_ORDER}


def token_order(*, redrive_count: int, retry_count: int) -> int:
    """The order of a ``Start_Run`` attempt's token: ``Execution.RedriveCount`` first, then ``State.RetryCount``.

    In TR-0, ``State.EnteredTime`` was the same for every retry of the state, so
    only these counters order the tokens; a redrive runs ``Start_Run`` again with
    ``RetryCount`` 0 and a higher ``RedriveCount``.
    """
    if redrive_count < 0 or not 0 <= retry_count < _RETRIES:
        raise ValueError(f"counters out of range: RedriveCount {redrive_count}, RetryCount {retry_count}")
    return redrive_count * _RETRIES + retry_count


def index_keys(execution_start: datetime) -> Dict[str, str]:
    """``run-start-index``'s keys of a run whose execution started at ``execution_start`` (§5).

    Both are strings, as the index declares them: ``start_day``, the UTC day of
    the start, and ``started_at``, a timestamp whose string order is time order.
    """
    started_at = format_utc(execution_start)
    return {START_DAY: started_at[:10], STARTED_AT: started_at}


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` UTF-8 bytes, ending in an ellipsis when cut, never inside a character."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit - 3].decode("utf-8", errors="ignore") + "…"


#: Who the stop mark names when the engine stops a run: its execution ended before its answer, or it never
#: became active (§4).
ENGINE = "the engine"


def stop_mark(*, by: str, reason: str, at: str) -> Dict[str, str]:
    """The stop mark: who asked, why and when (§4)."""
    return {"by": clip(by, BY_MAX), "reason": clip(reason, STOP_REASON_MAX), "at": at}


#: The keys of the step mark, which the generated pipeline writes in JSONata too (generators._mark_state).
STEP_MARK_STEP = "step"
STEP_MARK_ERROR = "error"
#: JSONata cuts a string by characters, and a character takes at most four bytes of UTF-8.
ERROR_CHARS = ERROR_MAX // 4


def step_mark(*, step: str, error: str) -> Dict[str, str]:
    """The mark of a failed direct step; its cause goes to the step warning, not to the run row (§1)."""
    return {STEP_MARK_STEP: clip(step, STEP_MAX), STEP_MARK_ERROR: clip(error, ERROR_MAX)}


def request(*, by: str, at: str, task: Optional[str] = None) -> Dict[str, str]:
    """A request on the run row: who asked and when, and the task whose dependants a reset concerns."""
    if task is not None and len(task.encode("utf-8")) > TASK_ID_MAX:
        raise ValueError(f"a task id of more than {TASK_ID_MAX} bytes cannot be named in a request")
    return {"by": clip(by, BY_MAX), "at": at, **({"task": task} if task is not None else {})}


def is_run_row(row_key: str) -> bool:
    """Whether ``row_key`` names a run row, not a run's input or a task row."""
    return row_key.startswith(KEY_PREFIX) and not row_key.endswith(INPUT_SUFFIX)
