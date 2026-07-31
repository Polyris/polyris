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
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .task import TaskInstance

# Environment variables the runtime injects into ECS/Glue/Batch task containers so
# ``pull`` needs no arguments there; Lambda/SFN receive the same context as event
# fields instead (their env is fixed at deploy). See docs/features/DATA_PASSING.md.
ENV_PIPELINE = "POLYRIS_PIPELINE_NAME"
ENV_DATE = "POLYRIS_RUN_DATE"
ENV_TABLE = "POLYRIS_TOKENS_TABLE"


class PullError(RuntimeError):
    """Raised when a dependency's output cannot be pulled."""


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
