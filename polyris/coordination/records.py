"""Row shapes the coordination layer writes and reads (ADR #130 §1, §9).

Builders only — no I/O. Every writer (run_task's template aside) goes
through these, so the bell, the index and the evaluator see one shape.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Mapping, Optional

from ..partitions import GRANULARITIES, detect_granularity
from . import attention as att
from .asset_conditions import Fact
from .partition import partition_date
from .statuses import FAILED
from .sweep import next_push_retry
from .times import format_utc, parse_utc

WARNING_KINDS = frozenset({"coordination", "zombie", "push", "sweeper", "notify"})
WARNING_TTL = timedelta(days=8)
WARNING_PREFIX = "_notify_warn_"

RETRY_PUSH_PREFIX = "_retry_push_"
#: A push retry row lives a week; the sweeper gives up on it after a day (ADR #130 §7).
RETRY_TTL = timedelta(days=7)

CASCADES = frozenset({"auto", "none", "all"})
#: Facts written by ADR #130 writers. Older facts have no version.
FACT_VERSION = 2

#: The GSI on ``asset-events`` that asset conditions read (template.yaml).
#: Its items carry exactly what :func:`fact_from_item` needs.
PARTITION_INDEX = "asset-partition-index"
PARTITION_INDEX_PROJECTION = ("event_time", "partition_key", "granularity", "cascade", "source_dag", "uri")


def warning_item(
    *,
    kind: str,
    execution_name: str,
    task_name: str,
    pipeline_name: str,
    pipeline_execution: str,
    task_date: str,
    error: str,
    now: datetime,
) -> Dict[str, Any]:
    """A warning row for the console bell.

    Indexed by write time through the attention index, so warnings about
    tasks of any partition date reach the bell (defect B-N1). It carries no
    ``date`` attribute on purpose: the date-partitioned failure query must
    not list it a second time. ``task_date`` keeps the partition for display.

    Context the caller does not know is left out, never written as ``""``:
    ``pipeline_name`` and ``pipeline_execution`` are GSI keys, and DynamoDB
    rejects an empty string there — the warning would be lost.
    """
    if kind not in WARNING_KINDS:
        raise ValueError(f"unknown warning kind {kind!r}; expected one of {sorted(WARNING_KINDS)}")
    context = {"task_name": task_name, "pipeline_name": pipeline_name,
               "pipeline_execution": pipeline_execution, "task_date": task_date}
    return {
        "execution_name": f"{WARNING_PREFIX}{kind}_{execution_name}",
        "kind": kind,
        **{k: v for k, v in context.items() if v},
        "status": FAILED,
        "error": error,
        "finished_at": format_utc(now),
        att.KEY: att.attention(att.WARN, execution_name),
        att.DUE: format_utc(now),
        "ttl": int((now + WARNING_TTL).timestamp()),
    }


def push_retry_item(
    *,
    run_name: str,
    consumer: str,
    sfn_arn: str,
    run_input: str,
    error: str,
    now: datetime,
) -> Dict[str, Any]:
    """A push start that failed transiently, for the sweeper to retry (ADR #130 §7).

    Keyed by the run's deterministic name, so a repeated failure finds the
    same row and a retried start can never run the consumer twice.
    """
    if not (run_name.strip() and consumer.strip() and sfn_arn.strip()):
        raise ValueError("a push retry needs the run name, the consumer and its state machine")
    return {
        "execution_name": f"{RETRY_PUSH_PREFIX}{run_name}",
        "kind": "push",
        "consumer": consumer,
        "sfn_arn": sfn_arn,
        "run_name": run_name,
        "run_input": run_input,
        "error": error,
        "attempts": 0,
        "first_failed_at": format_utc(now),
        att.KEY: att.attention(att.RETRY, run_name),
        att.DUE: format_utc(next_push_retry(now, 0)),
        "ttl": int((now + RETRY_TTL).timestamp()),
    }


def asset_fact_item(
    *,
    asset_name: str,
    partition_key: str,
    granularity: str,
    cascade: str,
    source_dag: str,
    source_task: str,
    uri: str,
    now: datetime,
    ttl_days: int,
) -> Dict[str, Any]:
    """An ``asset-events`` row as every ADR #130 writer stores it.

    ``execution_date`` keeps the raw partition key for the readers that
    already use it (Principle #39); ``partition_date`` is the normalised date
    the partition index sorts on.
    """
    if not asset_name.strip():
        raise ValueError("asset_name must not be empty")
    if granularity not in GRANULARITIES:
        raise ValueError(f"unknown granularity {granularity!r}; expected one of {list(GRANULARITIES)}")
    if cascade not in CASCADES:
        raise ValueError(f"unknown cascade {cascade!r}; expected one of {sorted(CASCADES)}")
    if ttl_days < 1:
        raise ValueError("ttl_days must be at least 1")
    return {
        "asset_name": asset_name,
        "event_time": format_utc(now),
        "execution_date": partition_key,
        "partition_key": partition_key,
        "partition_date": partition_date(partition_key).isoformat(),
        "granularity": granularity,
        "cascade": cascade,
        "source_dag": source_dag,
        "source_task": source_task,
        "uri": uri,
        "fact_version": FACT_VERSION,
        "ttl": int((now + timedelta(days=ttl_days)).timestamp()),
    }


def fact_from_item(item: Mapping[str, Any], *, declared_granularity: Optional[str] = None) -> Fact:
    """Read an ``asset-events`` row, old or new, as a :class:`Fact`.

    Rows written before ADR #130 have no ``partition_date`` and no
    ``granularity``. Their date comes from ``execution_date``. Their
    granularity is the outlet's ``declared_granularity`` when the caller found
    it in the producer's registry entry, and otherwise the granularity whose
    format the key uses.
    """
    key = str(item.get("partition_key") or item.get("execution_date") or "")
    raw_date = item.get("partition_date")
    pd = date.fromisoformat(str(raw_date)) if raw_date else partition_date(key)
    granularity = str(item.get("granularity") or declared_granularity or detect_granularity(key))
    if granularity not in GRANULARITIES:
        raise ValueError(
            f"asset fact {item.get('asset_name')!r} has granularity {granularity!r}; "
            f"expected one of {list(GRANULARITIES)}"
        )
    return Fact(
        asset_name=str(item["asset_name"]),
        partition_date=pd,
        event_time=parse_utc(str(item["event_time"])),
        granularity=granularity,
    )
