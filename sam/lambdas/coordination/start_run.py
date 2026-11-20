"""Start_Run (ADR #137 §1): write a run's rows, then mark the run active.

The generated pipeline invokes this function with
``lambda:invoke.waitForTaskToken``; the dispatcher answers the token once every
row of the run is final. The pure core, ``polyris.coordination.start_run``,
builds every write and its condition; this module sends them in its order and
reads back what a condition refused.

A transient AWS error raises ``CoordinationTransient``, which the state's
``Retry`` repeats with a new token. Anything else fails the state into its
``Catch``, which writes the warning: a format this platform does not run, specs
that are missing or do not match, a row of another run, or the stop mark.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

import env
import failures
from logger import log
from polyris.coordination import run_row, spec_store
from polyris.coordination import start_run as core

_deserializer = TypeDeserializer()


class Stopped(Exception):
    """The engine stopped this run before it became active (ADR #137 §4): this attempt starts nothing."""


def handler(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    call = core.call_from(event)
    try:
        return _start(call, datetime.now(timezone.utc))
    except (ClientError, BotoCoreError) as exc:
        failures.reraise_transient(exc)
        raise


def _start(call: core.Call, now: datetime) -> Dict[str, Any]:
    ddb = env.dynamodb()
    specs = spec_store.read(env.dynamodb_client(), env.SPECS_TABLE, call.spec_id, expected_count=call.task_count)
    tokens = ddb.Table(env.TOKENS_TABLE)
    if not _write_run_row(tokens, call, now):
        log.info("start_run", "a later attempt holds the run", execution=call.execution, order=call.order)
        return {"run": call.execution, "started": False}
    _put(tokens, core.input_item(call, now=now), call, "the run's input")
    items = core.task_items(call, specs, now=now)
    for item in items:
        _put(tokens, item, call, f"the row of {item['task_name']}")
    pointers = core.pointer_items(call, specs, now=now)
    subscriptions = ddb.Table(env.SUBSCRIPTIONS_TABLE)
    for pointer in pointers:
        subscriptions.put_item(Item=pointer)
    _activate(tokens, call, now)
    log.info("start_run", "run active", execution=call.execution, tasks=len(items), pointers=len(pointers))
    return {"run": call.execution, "started": True, "tasks": len(items)}


def _write_run_row(tokens: Any, call: core.Call, now: datetime) -> bool:
    """Write the run row ``creating`` with this attempt's token; False when a later attempt holds it."""
    try:
        tokens.update_item(**core.run_row_write(call, now=now))
        return True
    except ClientError as exc:
        if not failures.is_condition_failed(exc):
            raise
    row = _run_row(tokens, call)
    if run_row.STOP in row:
        raise Stopped(f"{call.execution}: the run was stopped before it became active; this attempt starts nothing")
    if not core.is_this_run(row, call):
        raise core.OtherRun(f"{call.execution}: the run row belongs to another run ({row.get(run_row.EXECUTION)!r}, "
                            f"started {row.get(run_row.RUN_START)!r})")
    if int(row.get(run_row.TOKEN_ORDER, -1)) > call.order:
        return False
    # The row's token is never quoted: the message reaches the console's warnings.
    raise RuntimeError(f"{call.execution}: the run row refused attempt {call.order} for no known reason "
                       f"(status {row.get(run_row.STATUS)!r}, attempt {row.get(run_row.TOKEN_ORDER)!r})")


def _put(tokens: Any, item: Mapping[str, Any], call: core.Call, what: str) -> None:
    """Put a row of this run; an existing one must be this run's, written by an earlier attempt."""
    try:
        tokens.put_item(Item=dict(item), **core.task_condition())
    except ClientError as exc:
        if not failures.is_condition_failed(exc):
            raise
        old = exc.response.get("Item") or {}
        core.accept_existing({k: _deserializer.deserialize(v) for k, v in old.items()}, call, what)


def _activate(tokens: Any, call: core.Call, now: datetime) -> None:
    try:
        tokens.update_item(**core.activation(call, now=now))
    except ClientError as exc:
        if not failures.is_condition_failed(exc):
            raise
        if not core.activation_refused(_run_row(tokens, call), call):
            raise Stopped(f"{call.execution}: the run was stopped before it became active; it starts nothing")


def _run_row(tokens: Any, call: core.Call) -> Dict[str, Any]:
    response = tokens.get_item(Key={run_row.TABLE_KEY: run_row.key(call.execution)}, ConsistentRead=True)
    return dict(response.get("Item") or {})
