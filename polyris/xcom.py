"""XCom — data passing between tasks.

Two roles:

* :class:`XComArg` — the DSL handle returned by ``task()``,
  used to wire dependencies at pipeline-definition time.
* :func:`pull` — the runtime helper a task calls to fetch the whole output a
  dependency produced. It reads the canonical DynamoDB output store (key
  ``output#pipeline#task#date``); outputs offloaded to S3 are resolved
  transparently. The same call works in every task type (Lambda, SFN, ECS, Glue,
  Batch).

The context ``pull("A")`` needs — pipeline name, run date, table — defaults to
environment variables the runtime injects, so inside a task body you just call
``xcom.pull("upstream_task")`` with no arguments.
"""
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .task import TaskInstance

# Environment variables the runtime injects into ECS/Glue/Batch task containers so
# ``pull`` needs no arguments there; Lambda/SFN receive the same context as event
# fields instead (their env is fixed at deploy). See docs/features/DATA_PASSING.md.
ENV_PIPELINE = "POLYRIS_PIPELINE_NAME"
ENV_DATE = "POLYRIS_RUN_DATE"
ENV_TABLE = "POLYRIS_TOKENS_TABLE"
ENV_TASK_NAME = "POLYRIS_TASK_NAME"          # set by run_task wrapper for service tasks
ENV_RUN_ID = "POLYRIS_WRAPPER_RUN_ID"        # SFN wrapper execution ARN — used by xcom.push()

# Field name written to DDB by xcom.push() and read by run_task wrapper's
# Check_Task_Pushed state. Coupled constant — must match run_task/sfn.tpl.json.
# See XCOM_PLAN.md §2.8 for the full coupled-constants list.
_PUSH_MARKER_FIELD = "_pushed_by_task"


class XComError(RuntimeError):
    """Base class for all XCom errors."""


class XComMissingError(XComError):
    """Upstream task has no stored output (never ran, was skipped, or didn't return anything)."""

    def __init__(self, task_name: str, pipeline: Optional[str] = None, date: Optional[str] = None):
        self.task_name = task_name
        self.pipeline = pipeline
        self.date = date
        msg = f"no output stored for task '{task_name}'"
        if pipeline and date:
            msg += f" (pipeline '{pipeline}', date '{date}')"
        msg += " — did the task run and return anything?"
        super().__init__(msg)


class XComUpstreamFailedError(XComError):
    """Upstream task did not succeed (skipped/failed/aborted). Caller opted for loud errors."""

    def __init__(self, task_name: str, status: str):
        self.task_name = task_name
        self.status = status
        super().__init__(
            f"upstream '{task_name}' did not succeed (status: {status}). "
            f"Pass raise_on_failure=False to xcom.get() to read its output anyway."
        )


class XComTruncatedError(XComError):
    """Upstream output too large for inline transport, and DDB fallback also unavailable."""

    def __init__(self, task_name: str, size_bytes: Optional[int] = None):
        self.task_name = task_name
        self.size_bytes = size_bytes
        msg = f"output for task '{task_name}' is truncated"
        if size_bytes:
            msg += f" ({size_bytes} bytes)"
        msg += (
            " — use Claim Check pattern: write to S3 and return "
            "{'_s3_ref': 's3://...'}. See docs/features/DATA_PASSING.md#large-outputs."
        )
        super().__init__(msg)


# Backward-compat alias. Existing user code with `except PullError` continues to work
# because PullError now refers to XComMissingError (same behaviour for the historical
# "no output stored" and "no context" cases; also catches the new missing-dep raises
# from xcom.get()).
PullError = XComMissingError


class XComArg:
    """Represents the output of a task, used for data passing.

    ``task()`` returns an ``XComArg`` that can be passed to other tasks to declare
    a dependency.
    """
    def __init__(self, task_instance: 'TaskInstance', key: str = "return_value"):
        self.task_instance = task_instance
        self.key = key

    @property
    def task_id(self) -> str:
        return self.task_instance.task.task_id

    def __repr__(self):
        return f"XComArg({self.task_id}.{self.key})"


def _resolve(value: Optional[str], context: dict, keys: tuple, env: str, what: str) -> str:
    """Resolve a context field: explicit arg > task input (context) > env var."""
    if value is None:
        for k in keys:
            if context.get(k):
                value = context[k]
                break
    if value is None:
        value = os.environ.get(env)
    if not value:
        raise PullError(
            f"pull() needs the {what}. In a Lambda pass the event "
            f"(`xcom.pull('t', event)`); in ECS/Glue the runtime sets ${env}. "
            "You can also pass it explicitly."
        )
    return value


def _resolve_s3_pointer(ref: str, s3_client: Any) -> Any:
    """Fetch and parse a JSON object offloaded to S3 (``s3://bucket/key``)."""
    without_scheme = ref[len("s3://"):] if ref.startswith("s3://") else ref
    bucket, _, key = without_scheme.partition("/")
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def _get_from_ddb(
    task_name: str,
    event: Optional[dict],
    ddb_client: Any,
    s3_client: Any,
    raise_on_missing: bool,
) -> Any:
    """Fallback path for :func:`get` — reads from DDB via :func:`pull`.

    Wraps pull()'s legacy ``PullError`` messages into the canonical XCom error
    types so callers get a consistent taxonomy regardless of which read path
    served the request.
    """
    try:
        return pull(task_name, event, ddb_client=ddb_client, s3_client=s3_client)
    except PullError as e:
        msg = str(e)
        if "truncated" in msg:
            # Elevate to XComTruncatedError so message points at Claim Check pattern
            raise XComTruncatedError(task_name) from e
        if not raise_on_missing and ("no output stored" in msg or "needs the" in msg):
            return None
        raise  # PullError == XComMissingError alias; propagates as expected


def pull(
    task_name: str,
    context: Optional[dict] = None,
    *,
    pipeline: Optional[str] = None,
    date: Optional[str] = None,
    table: Optional[str] = None,
    ddb_client: Any = None,
    s3_client: Any = None,
) -> Any:
    """Fetch the whole output that dependency ``task_name`` produced.

    Works the same in every task type — only how the context reaches the task
    differs (an AWS constraint, hidden here):

    * **Lambda** — pass the handler event: ``xcom.pull("upstream", event)``.
      (Its context lives in the event, since Lambda env is fixed at deploy.)
    * **ECS / Glue** — just ``xcom.pull("upstream")``; the runtime provides the
      context via environment.

    Args:
        task_name: the upstream task to read the output of (a declared dependency).
        context: the task's input (e.g. a Lambda event) to read context from.
        pipeline / date / table: override context explicitly (each otherwise comes
            from ``context`` then the ``POLYRIS_*`` env vars).
        ddb_client / s3_client: boto3 clients; created on demand if omitted.

    Returns:
        The upstream task's output (parsed JSON), whatever its size.

    Raises:
        PullError: if the task stored nothing, or its output is unavailable.
    """
    ctx = context or {}
    pipeline = _resolve(pipeline, ctx, ("pipeline_name", "pipeline"), ENV_PIPELINE, "pipeline name")
    date = _resolve(date, ctx, ("date", "current_date"), ENV_DATE, "run date")
    table = _resolve(table, ctx, ("_polyris_table",), ENV_TABLE, "table name")

    if ddb_client is None:
        import boto3  # pragma: no cover - boto3 client factory; tests inject a client
        ddb_client = boto3.client("dynamodb")  # pragma: no cover

    key = f"output#{pipeline}#{task_name}#{date}"
    resp = ddb_client.get_item(
        TableName=table,
        Key={"execution_name": {"S": key}},
        ProjectionExpression="#r",
        ExpressionAttributeNames={"#r": "result"},
    )
    item = resp.get("Item")
    if not item or "result" not in item:
        raise PullError(
            f"no output stored for task '{task_name}' "
            f"(pipeline '{pipeline}', date '{date}') — did it return anything?"
        )

    try:
        data: Any = json.loads(item["result"]["S"])
    except (ValueError, KeyError) as e:
        raise PullError(
            f"stored output for task '{task_name}' is not readable JSON: {e}"
        ) from e

    # Large outputs are offloaded to S3 and stored as an _s3_ref pointer — resolve it (matches console_api's retrieve_result).
    if isinstance(data, dict) and "_s3_ref" in data:
        if s3_client is None:
            import boto3  # pragma: no cover - boto3 client factory; tests inject a client
            s3_client = boto3.client("s3")  # pragma: no cover
        data = _resolve_s3_pointer(data["_s3_ref"], s3_client)

    if isinstance(data, dict) and data.get("_truncated"):
        raise PullError(
            f"output for task '{task_name}' was truncated and is unavailable "
            "(this build predates transparent S3 offload for large outputs)."
        )
    return data


def get(
    event: Optional[dict] = None,
    task_name: Optional[str] = None,
    *,
    raise_on_missing: bool = True,
    raise_on_failure: bool = True,
    ddb_client: Any = None,
    s3_client: Any = None,
) -> Any:
    """Read an upstream task's output. Uniform API for every task type.

    Recommended reader — replaces ``event["upstream"][task]["output"]`` (which
    still works but returns raw markers on missing / failed / truncated cases)
    and ``xcom.pull()`` (low-level DDB reader). ``get()`` returns the same value
    ``pull()`` would but with loud, typed errors by default and transparent
    fallback from a truncated inline inject to the full-size DDB row.

    Lookup order:

    1. If ``event["upstream"][task_name]`` exists (Lambda / SFN pre-fetched
       inject), use it. Auto-resolves ``{"_s3_ref": "s3://..."}`` pointers.
       On a ``{"_truncated": true}`` marker, falls back to DDB (which may hold
       the full value up to 350KB).
    2. Otherwise (Glue / ECS / Batch / EMR, or an undeclared Lambda dep),
       reads DDB directly via :func:`pull`.

    Args:
        event: Lambda handler event, or ``None`` for service tasks.
        task_name: upstream ``task_id`` to read.
        raise_on_missing: raise :class:`XComMissingError` for a dep with no
            recorded output (default ``True``). Pass ``False`` to get ``None``
            for a soft check.
        raise_on_failure: raise :class:`XComUpstreamFailedError` when the
            upstream's status != ``"success"`` (default ``True``). Pass
            ``False`` when using ``trigger_rule="all_done"`` and you want to
            read output regardless of upstream outcome.
        ddb_client, s3_client: injected for tests; created on demand otherwise.

    Returns:
        The upstream output — whatever shape it stored (``dict``, ``list``,
        primitive, ``None``).

    Raises:
        ValueError: ``task_name`` was not provided.
        XComMissingError: dep has no recorded output (subject to ``raise_on_missing``).
        XComUpstreamFailedError: upstream status != ``"success"``
            (subject to ``raise_on_failure``).
        XComTruncatedError: both the event inject and DDB rows returned
            truncation markers — the actual data is unavailable through this
            path. Use the Claim Check pattern (write to S3, return
            ``{"_s3_ref": "s3://..."}``).

    Examples:
        Lambda handler (declared dep)::

            def handler(event, context):
                sales = xcom.get(event, "extract_sales")
                # sales is whatever extract_sales returned

        Optional dep (``all_done`` trigger)::

            def handler(event, context):
                sales = xcom.get(event, "extract_sales")
                bonus = xcom.get(event, "bonus", raise_on_failure=False) or {}

        Glue / ECS / Batch task (no event to pass)::

            from polyris import xcom
            sales = xcom.get(None, "extract_sales")
    """
    if not task_name:
        raise ValueError("xcom.get() requires task_name")

    # 1. Pre-fetched inject path (Lambda / SFN receive event.upstream from run_task)
    if event and isinstance(event, dict):
        upstream = event.get("upstream")
        if isinstance(upstream, dict) and task_name in upstream:
            entry = upstream[task_name]

            # Non-dict entry: malformed / legacy shape — pass through unchanged
            if not isinstance(entry, dict):
                return entry

            status = entry.get("status", "unknown")
            output = entry.get("output")

            # Missing dep: Get_Dep_Output writes {"output": {}, "status": "unknown"}
            # when the upstream DDB row is absent (dep didn't run, was skipped, etc.).
            # Only checks status here — an empty {} with status="success" is a valid
            # user-returned value and must NOT be treated as missing.
            if status == "unknown":
                if raise_on_missing:
                    raise XComMissingError(task_name)
                return None

            # Non-success upstream (all_done trigger path)
            if status != "success":
                if raise_on_failure:
                    raise XComUpstreamFailedError(task_name, status)
                # else: fall through and return whatever output was recorded

            # Truncated in inject path — try DDB, which may hold the full value
            # (runtime injection cap is ~25KB per dep; DDB result field allows ~350KB)
            if isinstance(output, dict) and output.get("_truncated"):
                try:
                    return _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing)
                except XComMissingError:
                    # DDB also empty — the original truncation stands
                    raise XComTruncatedError(task_name, output.get("_size"))

            # S3 Claim Check pointer — resolve transparently
            if isinstance(output, dict) and "_s3_ref" in output:
                if s3_client is None:
                    import boto3  # pragma: no cover - boto3 client factory; tests inject a client
                    s3_client = boto3.client("s3")  # pragma: no cover
                return _resolve_s3_pointer(output["_s3_ref"], s3_client)

            return output

    # 2. Fallback: read from DDB (Glue/ECS/Batch/EMR, or undeclared Lambda dep)
    return _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing)
