"""What ``Start_Run`` writes for a run, and the conditions of those writes (ADR #137 §1, §3).

``Start_Run`` is the Lambda that a pipeline invokes with
``lambda:invoke.waitForTaskToken``. It checks the format first, reads the specs
of the run's spec version, then writes in this order:

1. the run row, ``creating``, with the attempt's token: an update, never a put,
   so a direct step's mark written first survives (``run_row_write``);
2. the run's input item, once (``input_item``);
3. one row per task, ``waiting``, or ``skipped`` with ``skip_origin = run``
   for a task in the run's ``skip_tasks`` (``task_items``);
4. the ``asset:{name}`` pointers of the tasks with ``wait_for`` (``pointer_items``);
5. last, ``active``, unless the run row carries the stop mark (``activation``).

Each row is a conditional put. An existing row is accepted only when it carries
this execution's name and ``StartTime`` (``task_row.of_run``), so a retry or a
redrive writes nothing twice, and a row of another run fails ``Start_Run`` with
the cause. Nothing starts for a run whose run row is missing or ``creating``.
These functions are pure; the Lambda (sam/lambdas/coordination/start_run.py)
sends what they build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Tuple

from ..constants import SkipOrigin, TaskSpecKey, TaskStatus
from . import asset_spec, attention, deadlines, ownership, records, retention, run_input, run_row, task_row, wake
from .task_spec import FORMAT_VERSION, MAX_TASKS, TaskSpec, decision_text
from .times import format_utc, parse_utc

#: The table of per-run pointers that AssetFanout reads (DependencySubscriptionsTable) and its keys.
POINTER_KEY = "dependency_key"
POINTER_SUBSCRIBER = "subscriber"
ASSET_POINTER_PREFIX = "asset:"


class Arg:
    """The keys of Start_Run's payload, which the generated pipeline builds (generators._start_run_branch)."""
    FORMAT_VERSION = "format_version"
    SPEC_ID = "spec_id"
    TASK_COUNT = "task_count"
    PIPELINE_NAME = "pipeline_name"
    EXECUTION_NAME = "execution_name"      # $states.context.Execution.Name
    EXECUTION_START = "execution_start"    # $states.context.Execution.StartTime
    TOKEN = "token"                        # $states.context.Task.Token
    RETRY_COUNT = "retry_count"            # $states.context.State.RetryCount
    REDRIVE_COUNT = "redrive_count"        # $states.context.Execution.RedriveCount
    DATE = "date"                          # the run's date, computed once by the generated JSONata
    INPUT = "input"                        # what the run was started with (RunArg)


class RunArg:
    """The keys of the payload's ``input``: the values a run was started with, as each 1.2 task got them."""
    VARIABLES = "variables"
    SKIP_TASKS = "skip_tasks"
    CASCADE_ALL = "cascade_all"                    # the two flags records.cascade_of reads
    SUPPRESS_ASSET_EVENT = "_suppress_asset_event"
    TRIGGERED_BY = "triggered_by"
    BACKFILL_ID = "backfill_id"


#: The errors on which the Start_Run state tries again (Principle #53): Lambda's own failures, its
#: throttling, and Start_Run's CoordinationTransient. None of them is run_row.FAILED_RUN_ERROR, the
#: dispatcher's answer of a failed run, and no wildcard (States.ALL, States.TaskFailed) is among them.
RETRY_ERRORS = ("Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException",
                "Lambda.TooManyRequestsException", "Lambda.Unknown", "Sandbox.Timedout", wake.TRANSIENT_ERROR)
RETRY_INTERVAL = timedelta(seconds=2)
RETRY_ATTEMPTS = 6
RETRY_BACKOFF = 2
RETRY_MAX_DELAY = timedelta(minutes=1)
#: The Lambda's timeout, from the largest pipeline polyris-deploy accepts (§8): MAX_TASKS rows, one
#: conditional put each, at an allowance ten times a put's usual 5-20 ms, so the asset pointers fit
#: too, and the reads of the specs and of the run row. sam/template.yaml declares it; a test pins it.
PUT_ALLOWANCE = timedelta(milliseconds=100)
READS_ALLOWANCE = timedelta(seconds=100)
TIMEOUT = MAX_TASKS * PUT_ALLOWANCE + READS_ALLOWANCE


def retry_window() -> timedelta:
    """The longest a run row stays ``creating`` while its Start_Run is tried (§1): every attempt's
    timeout and every backoff at its longest. A run still ``creating`` after it never became active (§4)."""
    backoffs = sum((min(RETRY_INTERVAL * RETRY_BACKOFF ** attempt, RETRY_MAX_DELAY)
                    for attempt in range(RETRY_ATTEMPTS)), timedelta())
    return (RETRY_ATTEMPTS + 1) * TIMEOUT + backoffs


class FormatError(ValueError):
    """The pipeline was deployed with a format this platform does not support: redeploy it."""


class OtherRun(ValueError):
    """A row under this run's key belongs to another run."""


@dataclass(frozen=True)
class Call:
    """What the generated pipeline passes ``Start_Run``."""
    format_version: int
    spec_id: str
    task_count: int
    pipeline: str
    execution: str              # $states.context.Execution.Name
    execution_start: str        # $states.context.Execution.StartTime
    token: str                  # $states.context.Task.Token
    retry_count: int            # $states.context.State.RetryCount
    redrive_count: int          # $states.context.Execution.RedriveCount
    date: str                   # the run's date, as the generated JSONata computed it
    execution_input: Mapping[str, Any] = field(default_factory=dict)

    @property
    def order(self) -> int:
        return run_row.token_order(redrive_count=self.redrive_count, retry_count=self.retry_count)


def call_from(payload: Mapping[str, Any]) -> Call:
    """The call from ``Start_Run``'s payload. The format is checked before anything else."""
    version = payload.get(Arg.FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise FormatError(f"this platform runs pipelines of format {FORMAT_VERSION}, and this pipeline was "
                          f"deployed with format {version!r}: redeploy it with polyris-deploy")
    try:
        call = Call(
            format_version=int(version), spec_id=str(payload[Arg.SPEC_ID]), task_count=int(payload[Arg.TASK_COUNT]),
            pipeline=str(payload[Arg.PIPELINE_NAME]), execution=str(payload[Arg.EXECUTION_NAME]),
            execution_start=str(payload[Arg.EXECUTION_START]), token=str(payload[Arg.TOKEN]),
            retry_count=int(payload[Arg.RETRY_COUNT]), redrive_count=int(payload.get(Arg.REDRIVE_COUNT) or 0),
            date=str(payload[Arg.DATE]), execution_input=dict(payload.get(Arg.INPUT) or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Start_Run's payload is malformed: {exc!r}") from exc
    if not call.date:
        raise ValueError(f"{call.pipeline}: Start_Run's payload is malformed: the run has no date")
    if call.task_count < 1:
        raise ValueError(f"{call.pipeline}: a run of no task has no Start_Run")
    return call


def run_row_write(call: Call, *, now: datetime) -> Dict[str, Any]:
    """The update that writes the run row ``creating`` with this attempt's token.

    It never moves a row back to ``creating``, never lets an earlier attempt's
    token replace a later one, refuses a stopped run (``run_row.TOKEN_CONDITION``)
    and a row of another run, raises ``run_version``, clears the answered mark,
    and makes the run row due at once: a new token is checked by the sweeper.
    """
    key = run_row.key(call.execution)
    started = parse_utc(call.execution_start)
    index_keys = run_row.index_keys(started)
    values: Dict[str, Any] = {
        ":creating": run_row.CREATING, ":token": call.token, ":order": call.order, ":execution": call.execution,
        ":run_start": call.execution_start,
        ":started_at": index_keys[run_row.STARTED_AT], ":start_day": index_keys[run_row.START_DAY],
        ":pipeline": call.pipeline, ":run_date": call.date,
        ":triggered_by": run_row.clip(triggered_by(call), run_row.TRIGGERED_BY_MAX),
        ":spec_id": call.spec_id, ":task_count": call.task_count, ":format": call.format_version,
        ":ttl": retention.ttl(now, retention.HISTORY),
        ":attention": attention.attention(attention.WAITING, key), ":due": format_utc(now),
        **run_row.VERSION_RAISE_VALUES,
    }
    names = {"#status": run_row.STATUS, "#ttl": run_row.TTL, **run_row.TOKEN_CONDITION_NAMES, **run_row.VERSION_NAMES}
    return {
        "Key": {run_row.TABLE_KEY: key},
        "UpdateExpression": (
            "SET #status = if_not_exists(#status, :creating), run_token = :token, #order = :order, "
            "run_execution = :execution, run_start = :run_start, started_at = :started_at, start_day = :start_day, "
            "pipeline_name = :pipeline, run_date = :run_date, triggered_by = :triggered_by, spec_id = :spec_id, "
            "task_count = :task_count, format_version = :format, #ttl = :ttl, "
            f"attention = :attention, attention_at = :due, {run_row.VERSION_RAISE} REMOVE answered_order"),
        "ConditionExpression": (f"({run_row.TOKEN_CONDITION}) AND (attribute_not_exists(run_execution) "
                                "OR (run_execution = :execution AND run_start = :run_start))"),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


def input_item(call: Call, *, now: datetime) -> Dict[str, Any]:
    """The run's input item: variables, skip list and cascade, with the execution that wrote it."""
    given = call.execution_input
    item = run_input.to_item(
        run_input.RunInput(dict(given.get(RunArg.VARIABLES) or {}), skipped(call), records.cascade_of(given)),
        pipeline_execution=call.execution, ttl=retention.ttl(now, retention.HISTORY))
    return {**item, task_row.PIPELINE_EXECUTION: call.execution, task_row.RUN_START: call.execution_start}


def task_items(call: Call, specs: Mapping[str, TaskSpec], *, now: datetime) -> List[Dict[str, Any]]:
    """One row per task: ``waiting``, or ``skipped`` (origin ``run``) when the run's input names it."""
    skip = set(skipped(call))
    unknown = skip - set(specs)
    if unknown:
        raise ValueError(f"{call.pipeline}: skip_tasks names tasks the pipeline does not have: {sorted(unknown)}")
    date, stamp = call.date, format_utc(now)
    items = []
    for task_id, spec in sorted(specs.items()):
        item: Dict[str, Any] = {
            task_row.TABLE_KEY: task_row.key(task_id, date, call.execution),
            task_row.TASK_NAME: task_id, task_row.PIPELINE: call.pipeline,
            task_row.PIPELINE_EXECUTION: call.execution, task_row.RUN_START: call.execution_start,
            task_row.DATE: date, task_row.TASK_DATE: date,
            task_row.GENERATION: ownership.FIRST,
            task_row.SPEC_ID: call.spec_id, task_row.SPEC_HASH: spec.text_hash, task_row.DECISION: decision_text(spec),
            task_row.TASK_TYPE: spec.fields[TaskSpecKey.TASK_TYPE],
            task_row.TRIGGERED_BY: triggered_by(call),
            task_row.TTL: retention.ttl(now, retention.HISTORY),
        }
        backfill = call.execution_input.get(RunArg.BACKFILL_ID)
        if backfill:
            item[task_row.BACKFILL_ID] = str(backfill)
        if task_id in skip:
            item.update({task_row.STATUS: TaskStatus.SKIPPED.value, task_row.SKIP_ORIGIN: SkipOrigin.RUN,
                         task_row.FINISHED_AT: stamp})
        else:
            item.update({task_row.STATUS: TaskStatus.WAITING.value, task_row.WAITING_SINCE: stamp})
            if not spec.fields[TaskSpecKey.DEPENDENCIES]:
                # Allowed from the start of its wait (deadlines.enabled_since), so its wait_for limit counts.
                item[task_row.ENABLED_AT] = stamp
            due = deadlines.due_time(task_row.row_times(item), ())
            if due is not None:
                item[attention.KEY] = attention.attention(attention.WAITING, item[task_row.TABLE_KEY])
                item[attention.DUE] = format_utc(due)
        items.append(item)
    return items


def pointer_items(call: Call, specs: Mapping[str, TaskSpec]) -> List[Dict[str, str]]:
    """The ``asset:{name}`` pointers of the waiting tasks with ``wait_for``, for AssetFanout (§7)."""
    skip = set(skipped(call))
    pointers = []
    for task_id, spec in sorted(specs.items()):
        wait_for = spec.fields.get(TaskSpecKey.WAIT_FOR)
        if wait_for and task_id not in skip:
            pointers += asset_pointers(task_row.key(task_id, call.date, call.execution), wait_for)
    return pointers


def asset_pointers(subscriber: str, wait_for: Any) -> List[Dict[str, str]]:
    """The pointers of one row: one per asset its ``wait_for`` names. The dispatcher deletes them once the row
    has left the wait (Rule 40)."""
    return [{POINTER_KEY: f"{ASSET_POINTER_PREFIX}{name}", POINTER_SUBSCRIBER: subscriber}
            for name in sorted(asset_spec.asset_names(asset_spec.parse(wait_for)))]


def triggered_by(call: Call) -> str:
    return str(call.execution_input.get(RunArg.TRIGGERED_BY) or "unknown")


def skipped(call: Call) -> Tuple[str, ...]:
    """The tasks the run was started without: the run skips them (``skip_origin = run``)."""
    return tuple(str(task) for task in call.execution_input.get(RunArg.SKIP_TASKS) or ())


def task_condition() -> Dict[str, Any]:
    """The condition of each task row's put; on a refusal, ``task_row.of_run`` tells a retry from another run."""
    return {"ConditionExpression": "attribute_not_exists(#key)", "ExpressionAttributeNames": {"#key": task_row.TABLE_KEY},
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD"}


def accept_existing(existing: Mapping[str, Any], call: Call, what: str) -> None:
    """A row that a condition refused: this run's, written by an earlier attempt, or another run's."""
    if not task_row.of_run(existing, call.execution, call.execution_start):
        raise OtherRun(f"{what} belongs to another run ({existing.get(task_row.PIPELINE_EXECUTION)!r}, "
                       f"started {existing.get(task_row.RUN_START)!r}): this run cannot start")


def activation(call: Call) -> Dict[str, Any]:
    """The update that marks the run ``active``, unless the run row carries the stop mark (§4)."""
    return {
        "Key": {run_row.TABLE_KEY: run_row.key(call.execution)},
        "UpdateExpression": f"SET #status = :active, {run_row.VERSION_RAISE}",
        "ConditionExpression": "#status = :creating AND attribute_not_exists(#stop) AND run_execution = :execution",
        "ExpressionAttributeNames": {"#status": run_row.STATUS, "#stop": run_row.STOP, **run_row.VERSION_NAMES},
        "ExpressionAttributeValues": {":active": run_row.ACTIVE, ":creating": run_row.CREATING,
                                      ":execution": call.execution, **run_row.VERSION_RAISE_VALUES},
    }


def is_this_run(run_item: Mapping[str, Any], call: Call) -> bool:
    """Whether a run row read back is this execution's: its name and its StartTime as Step Functions gave it."""
    return run_item.get(run_row.EXECUTION) == call.execution and run_item.get(run_row.RUN_START) == call.execution_start


def activation_refused(run_item: Mapping[str, Any], call: Call) -> bool:
    """After a refused activation, read back: True when the run is already this run's and active (a retry),
    False when it carries the stop mark; anything else is another run's row."""
    if not is_this_run(run_item, call):
        raise OtherRun(f"the run row of {call.execution} belongs to another run")
    if run_row.STOP in run_item:
        return False
    return run_item.get(run_row.STATUS) == run_row.ACTIVE
