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
from typing import Any, Optional, TYPE_CHECKING, TypeGuard

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
# Check_Task_Pushed state. Coupled constant — must match run_task/sfn.tpl.json;
# renaming it here requires the matching template edit in the same commit.
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


class XComManuallyResolvedError(XComError):
    """Upstream has no organic output — the Console wrote a synthetic marker
    when an operator resolved the task via UI (mark_success / skip / fail / stop).

    The marker fields (``_manually_resolved`` / ``_resolution`` / ``_reason`` /
    ``_operator`` / ``_pipeline_execution``) are metadata, not payload. Reading
    them as if they were data is almost always a bug — the whole reason a
    resolution needed a human is that the task never produced real output.

    Loud by default. To read the marker anyway (e.g. to inspect the operator's
    ``reason`` or route on the ``resolution``), pass ``raise_on_manual=False``.

    When the marker carries ``_pipeline_execution``, the error message names
    the specific pipeline run that produced it — useful for spotting cross-run
    bleed (the ``output#{pipeline}#{task}#{date}`` row is date-scoped, so a
    downstream task in a different same-date run reads the same marker until
    a new organic output overwrites it).
    """

    def __init__(self, task_name: str, resolution: str, operator: str, reason: str,
                 pipeline_execution: str = ""):
        self.task_name = task_name
        self.resolution = resolution
        self.operator = operator
        self.reason = reason
        self.pipeline_execution = pipeline_execution
        detail = f"resolution: {resolution}, operator: {operator}"
        if reason:
            detail += f", reason: {reason}"
        if pipeline_execution:
            detail += f", pipeline_execution: {pipeline_execution}"
        super().__init__(
            f"upstream '{task_name}' has no organic output — it was manually "
            f"resolved via Console ({detail}). "
            "Pass raise_on_manual=False to xcom.get() to read the marker anyway."
        )


# Marker field names — coupled with `console_api/routes/tasks.py::_write_synthetic_output_marker`.
# Changing any of these here requires the same edit there in the same commit.
_MANUAL_RESOLVED_FIELD = "_manually_resolved"
_MANUAL_RESOLUTION_FIELD = "_resolution"
_MANUAL_REASON_FIELD = "_reason"
_MANUAL_OPERATOR_FIELD = "_operator"
_MANUAL_PIPELINE_EXECUTION_FIELD = "_pipeline_execution"


def _is_manual_marker(value: Any) -> TypeGuard[dict]:
    """True when ``value`` is the synthetic marker written by
    ``console_api::_write_synthetic_output_marker``. The marker is a dict with
    ``_manually_resolved: True`` — other shapes (organic dict outputs that
    happen to include an unrelated ``_manually_resolved`` key set to False /
    a string / etc.) are NOT markers.

    Typed as :class:`TypeGuard[dict]` so mypy narrows ``value`` to ``dict`` in
    the True branch — lets callers pass ``output`` (typed ``Any | None``) to
    :func:`_raise_manual` without a redundant ``isinstance`` narrow."""
    return (
        isinstance(value, dict)
        and value.get(_MANUAL_RESOLVED_FIELD) is True
    )


def _raise_manual(task_name: str, marker: dict) -> None:
    """Raise :class:`XComManuallyResolvedError` from a marker dict. Callers
    check ``_is_manual_marker`` first — this is a small helper so both
    :func:`get` and :func:`pull` produce identical error shapes."""
    raise XComManuallyResolvedError(
        task_name,
        resolution=str(marker.get(_MANUAL_RESOLUTION_FIELD, "unknown")),
        # `_operator` was added in 1.0.0 — older marker records may lack it.
        # Fall back to the same string the backend writes when auth is
        # disabled ("unknown") so the SDK / UI / backend all present one
        # distinct label for "no identity captured".
        operator=str(marker.get(_MANUAL_OPERATOR_FIELD) or "unknown"),
        reason=str(marker.get(_MANUAL_REASON_FIELD) or ""),
        # `_pipeline_execution` added in 1.0.0 to disambiguate cross-run
        # marker bleed (the `output#{pipeline}#{task}#{date}` row is
        # date-scoped). Empty string when absent (older markers).
        pipeline_execution=str(marker.get(_MANUAL_PIPELINE_EXECUTION_FIELD) or ""),
    )


# Backward-compat alias. Existing user code with `except PullError` continues to work
# because PullError now refers to XComMissingError.
#
# 0.99 → 1.0.0 behaviour change: PullError was previously raised for both
# "no output stored" AND "output was truncated and unavailable". In 1.0.0 the
# truncation case gets its own type — XComTruncatedError — when raised via
# xcom.get() (which layers inject → DDB fallback and only raises truncation
# after BOTH fail). xcom.pull() still raises PullError on truncation, matching
# the pre-1.0.0 shape.
#
# Actionable for callers: code catching `except PullError:` continues to catch
# every missing-row case; code that wants to react specifically to
# "truncated and unrecoverable" must migrate to xcom.get() and
# `except XComTruncatedError:`.
#
# Alias mention rule: this is the single canonical explanation of the alias —
# tutorials, examples, and other docs must use `XComMissingError` and refer
# here rather than re-explaining. See docs/CLAUDE.md "Back-compat aliases".
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
    raise_on_manual: bool,
) -> Any:
    """Fallback path for :func:`get` — reads from DDB via :func:`pull`.

    Wraps pull()'s legacy ``PullError`` messages into the canonical XCom error
    types so callers get a consistent taxonomy regardless of which read path
    served the request. Threads ``raise_on_manual`` through so the marker
    check fires on the DDB path too.
    """
    try:
        return pull(
            task_name, event,
            ddb_client=ddb_client, s3_client=s3_client,
            raise_on_manual=raise_on_manual,
        )
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
    raise_on_manual: bool = True,
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
        raise_on_manual: raise :class:`XComManuallyResolvedError` when the DDB
            row is the synthetic marker written by the Console for a manually-
            resolved task (mark_success / skip / fail / stop via UI). Default
            ``True`` — the marker is metadata, not organic payload, so treating
            it as data is almost always a bug. Pass ``False`` to receive the
            marker dict as-is.
        ddb_client / s3_client: boto3 clients; created on demand if omitted.

    Returns:
        The upstream task's output (parsed JSON), whatever its size.

    Raises:
        PullError: if the task stored nothing, or its output is unavailable.
        XComManuallyResolvedError: the stored row is a Console-written manual-
            resolution marker (subject to ``raise_on_manual``).
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

    # Manual-resolution marker check runs LAST — after _s3_ref resolution and
    # _truncated handling. An organic dict that happens to include a stray
    # `_manually_resolved` key (extremely unlikely user shape) is not a marker
    # unless the field is literally True — `_is_manual_marker` enforces that.
    if raise_on_manual and _is_manual_marker(data):
        _raise_manual(task_name, data)

    return data


def get(
    event: Optional[dict] = None,
    task_name: Optional[str] = None,
    *,
    raise_on_missing: bool = True,
    raise_on_failure: bool = True,
    raise_on_manual: bool = True,
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
        raise_on_manual: raise :class:`XComManuallyResolvedError` when the
            upstream was resolved by an operator via Console UI (mark_success /
            skip / fail / stop) — its recorded "output" is the synthetic
            marker, not real data. Default ``True``: silently returning a
            marker in place of payload is almost always a bug. Pass ``False``
            to receive the marker dict for introspection (e.g. reading the
            operator's ``_reason``).
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
        XComManuallyResolvedError: upstream carries a Console-written manual-
            resolution marker (subject to ``raise_on_manual``). Task's status
            (``success`` after ``mark_success``; ``skip`` / ``failed`` / etc.
            after the matching action) is not the story — human intervention is.

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

            # Manual-resolution marker check runs BEFORE the status check.
            # A human's Mark success / Skip / Fail / Stop is the story either
            # way — reporting "status: skip" for a manual skip is less useful
            # than "manually resolved (skip) by <operator>". Marker-first lets
            # the caller catch a single, more informative error type.
            if raise_on_manual and _is_manual_marker(output):
                _raise_manual(task_name, output)

            # Non-success upstream (all_done trigger path)
            if status != "success":
                if raise_on_failure:
                    raise XComUpstreamFailedError(task_name, status)
                # else: fall through and return whatever output was recorded

            # Truncated in inject path — try DDB, which may hold the full value
            # (runtime injection cap is ~25KB per dep; DDB result field allows ~350KB)
            if isinstance(output, dict) and output.get("_truncated"):
                try:
                    return _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing, raise_on_manual)
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
    return _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing, raise_on_manual)


# Size cap for a single DDB item (soft limit — DDB hard is 400KB, we leave
# headroom for metadata fields). Matches the runtime ~350KB result cap the
# wrapper enforces in Save_Success.
_PUSH_MAX_BYTES = 350_000


def push(
    value: Any,
    *,
    pipeline: Optional[str] = None,
    task: Optional[str] = None,
    date: Optional[str] = None,
    table: Optional[str] = None,
    run_id: Optional[str] = None,
    ddb_client: Any = None,
) -> None:
    """Store this task's output for downstream reading.

    Required for service tasks (Glue / ECS / Batch / EMR) whose wrapper only
    sees the AWS API response (JobRunId / TaskArn / QueryExecutionId / etc.) —
    without this call, downstream tasks receive that AWS metadata instead of
    the actual work output.

    Lambda tasks should prefer ``return value`` — see the Lambda note below.

    Args:
        value: JSON-serializable output (``dict``, ``list``, primitive, ``None``).
        pipeline / task / date / table / run_id: context, resolved from
            ``POLYRIS_*`` env vars if omitted. The wrapper injects these envs
            into every service-task container / job.
        ddb_client: injected for tests; created on demand otherwise.

    Raises:
        XComError: value is not JSON-serializable, or serialized > 350KB.
        XComError: required env var missing (usually means the wrapper wasn't
            re-deployed — actionable hint in the error message).
        ClientError: DDB UpdateItem failed (permission / throttling).

    Note (Lambda):
        Calling ``xcom.push()`` from a Lambda handler races with the wrapper's
        Save_Success write. If the push finishes AFTER the handler returns,
        the wrapper may or may not detect the marker depending on timing. Prefer
        ``return value`` from Lambda. This function emits a ``UserWarning`` if
        ``AWS_LAMBDA_FUNCTION_NAME`` is set. See DATA_PASSING.md.

    Note (cross-account):
        Cross-account tasks (task role in a different AWS account than the
        polyris deployment) cannot call ``xcom.push()`` without additional IAM
        setup — the ``pipeline-tokens`` table is in the polyris account. See docs.

    Coupled with SFN template:
        The ``_pushed_by_task`` marker field and ``pushed_run_id`` field written
        here are read by Check_Task_Pushed in ``run_task/sfn.tpl.json``. Do not
        rename either without a matching template update in the same commit.
    """
    import warnings

    # Lambda-runtime warning — race with wrapper's Save_Success is real. Emit
    # once per call so users see it in CloudWatch logs; stacklevel=2 points the
    # warning at the caller's line, not this module.
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        warnings.warn(
            "xcom.push() called from a Lambda handler. Prefer `return value` — "
            "an async push after the handler returns may race with the wrapper's "
            "Save_Success write. See docs/features/DATA_PASSING.md#lambda-write-pattern.",
            UserWarning,
            stacklevel=2,
        )

    # Serialize first — fail fast on non-JSON types before touching DDB.
    try:
        serialized = json.dumps(value)
    except TypeError as e:
        raise XComError(
            f"xcom.push() value must be JSON-serializable: {e}. "
            "Convert datetimes to ISO strings, Decimals to floats, custom classes "
            "to dicts, etc. before pushing."
        ) from e

    if len(serialized) > _PUSH_MAX_BYTES:
        raise XComError(
            f"xcom.push() value is {len(serialized)} bytes, exceeds "
            f"{_PUSH_MAX_BYTES} byte DDB item limit. Write the payload to S3 and "
            "push {'_s3_ref': 's3://your-bucket/your-key.json'} instead — the "
            "downstream xcom.get() / pull() will resolve the pointer "
            "transparently. See docs/features/DATA_PASSING.md#large-outputs."
        )

    ctx: dict = {}
    pipeline = _resolve(pipeline, ctx, (), ENV_PIPELINE, "pipeline name")
    date = _resolve(date, ctx, (), ENV_DATE, "run date")
    table = _resolve(table, ctx, (), ENV_TABLE, "table name")

    # POLYRIS_TASK_NAME and POLYRIS_WRAPPER_RUN_ID are NEW env vars — an
    # older wrapper (pre-1.0.0) does not inject them. Convert the generic
    # PullError from _resolve into an actionable XComError that names the
    # remediation.
    try:
        task = _resolve(task, ctx, (), ENV_TASK_NAME, "task name")
    except PullError:
        raise XComError(
            f"xcom.push() requires the '{ENV_TASK_NAME}' environment variable. "
            "This env var is injected by the polyris wrapper for service tasks. "
            "If you're seeing this in production, your pipeline is running under "
            "an older wrapper — run `sam deploy` on the polyris SAM template to "
            "update it (no per-pipeline redeploy needed). For local testing, pass "
            "task=... explicitly."
        ) from None

    try:
        run_id = _resolve(run_id, ctx, (), ENV_RUN_ID, "wrapper run id")
    except PullError:
        raise XComError(
            f"xcom.push() requires the '{ENV_RUN_ID}' environment variable "
            "(prevents stale push marker corruption across backfill runs). "
            "This env var is injected by the polyris wrapper — run `sam deploy` "
            "on the polyris SAM template to update it."
        ) from None

    if ddb_client is None:
        import boto3  # pragma: no cover - boto3 client factory; tests inject a client
        ddb_client = boto3.client("dynamodb")  # pragma: no cover

    key_name = f"output#{pipeline}#{task}#{date}"
    ddb_client.update_item(
        TableName=table,
        Key={"execution_name": {"S": key_name}},
        UpdateExpression=(
            "SET #r = :r, #p = :p, pushed_at = :now, pushed_run_id = :rid, "
            "task_name = :tn ADD push_count :one"
        ),
        ExpressionAttributeNames={"#r": "result", "#p": _PUSH_MARKER_FIELD},
        ExpressionAttributeValues={
            ":r": {"S": serialized},
            ":p": {"BOOL": True},
            ":now": {"S": datetime.now(timezone.utc).isoformat()},
            ":rid": {"S": run_id},
            ":tn": {"S": task},
            ":one": {"N": "1"},
        },
    )
