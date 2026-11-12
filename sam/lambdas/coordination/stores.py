"""DynamoDB access for the coordination Lambdas (ADR #130 §3, §4, §7).

Every read a decision rests on is strongly consistent and goes to a base
table. GSIs are used only to find candidates (the run's rows for resume) and
for asset-partition lookups, whose lag is closed by a consistent read of
recent writes. Every write is conditional on what the reconciler read; a
failed condition means another owner already moved the row and is reported
as ``False``, never as an error.
"""

import json
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import env
from coordination_core import attention as att
from coordination_core.asset_conditions import Fact, merge_facts
from coordination_core.records import PARTITION_INDEX, fact_from_item
from coordination_core.statuses import ABORTED, RECONCILABLE, SKIPPED, WAITING, WAITING_PAUSED
from coordination_core.times import format_utc
from failures import CoordinationTransient, is_condition_failed

NOT_FOUND = "not_found"
_BATCH = 100


class DependencyFact(NamedTuple):
    """What the reconciler reads of one dependency row."""

    status: str
    skip_origin: str
    #: When the dependency finished; "" while it has not.
    finished_at: str
_UNPROCESSED_RETRIES = 5


def _conditional(call: Callable[[], Any]) -> bool:
    try:
        call()
    except ClientError as exc:
        if is_condition_failed(exc):
            return False
        raise
    return True


class TaskStore:
    """Rows of ``pipeline-tokens``."""

    def __init__(self, resource: Any, table_name: str):
        self.resource = resource
        self.table_name = table_name
        self.table = resource.Table(table_name)

    def get(self, execution_name: str) -> Optional[Dict[str, Any]]:
        return self.table.get_item(Key={"execution_name": execution_name}, ConsistentRead=True).get("Item")

    def dependency_facts(self, execution_names: Sequence[str]) -> Dict[str, DependencyFact]:
        """``{execution_name: DependencyFact}``; a missing row has status ``not_found``."""
        result: Dict[str, DependencyFact] = {name: DependencyFact(NOT_FOUND, "", "") for name in execution_names}
        unique = list(dict.fromkeys(execution_names))
        for start in range(0, len(unique), _BATCH):
            pending: Dict[str, Any] = {self.table_name: {
                "Keys": [{"execution_name": n} for n in unique[start:start + _BATCH]],
                "ConsistentRead": True,
                "ProjectionExpression": "execution_name, #s, skip_origin, finished_at",
                "ExpressionAttributeNames": {"#s": "status"},
            }}
            for attempt in range(_UNPROCESSED_RETRIES):
                response = self.resource.batch_get_item(RequestItems=pending)
                for item in response.get("Responses", {}).get(self.table_name, []):
                    result[item["execution_name"]] = DependencyFact(
                        str(item.get("status", NOT_FOUND)), str(item.get("skip_origin", "")), str(item.get("finished_at", "")))
                pending = response.get("UnprocessedKeys") or {}
                if not pending:
                    break
                time.sleep(min(0.05 * 2 ** attempt, 1.0))
            if pending:
                raise CoordinationTransient("dependency rows stayed unprocessed after retries")
        return result

    def paused(self, pipeline_execution: str) -> bool:
        item = self.table.get_item(
            Key={"execution_name": f"_pause_{pipeline_execution}"},
            ConsistentRead=True,
            ProjectionExpression="paused",
        ).get("Item")
        return bool(item and item.get("paused"))

    def park(self, execution_name: str, token: str, started_at: str) -> bool:
        """waiting → waiting_paused, pinned to the token and registration read."""
        return _conditional(lambda: self.table.update_item(
            Key={"execution_name": execution_name},
            UpdateExpression="SET #s = :paused",
            ConditionExpression=(
                "wait_token = :t AND started_at = :s AND #s = :waiting AND attribute_not_exists(pause_token)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":paused": WAITING_PAUSED, ":t": token, ":s": started_at, ":waiting": WAITING},
        ))

    def close_skipped(self, execution_name: str, token: str, started_at: str, reason: str, *, now: datetime) -> bool:
        """A rule skip whose wrapper already finished (ADR #130 §3 step 3).

        The same fields the wrapper writes for a rule skip: ``skip_origin``
        lets an all_success cascade tell it from a manual skip (ADR #115).
        """
        return self._close(execution_name, token, started_at,
                           "SET #s = :skipped, skip_origin = :rule, skip_reason = :reason, finished_at = :now",
                           {":skipped": SKIPPED, ":rule": "rule", ":reason": reason, ":now": format_utc(now)})

    def close_aborted(self, execution_name: str, token: str, started_at: str, error: str, *, now: datetime) -> bool:
        """A zombie: its wrapper is gone, so nothing will ever move the row (ADR #130 §3 step 3)."""
        return self._close(execution_name, token, started_at,
                           "SET #s = :aborted, #e = :error, finished_at = :now",
                           {":aborted": ABORTED, ":error": error, ":now": format_utc(now)})

    def _close(self, execution_name: str, token: str, started_at: str, update: str, values: Dict[str, Any]) -> bool:
        names = {"#s": "status", "#att": att.KEY, "#due": att.DUE}
        if ":error" in values:
            names["#e"] = "error"   # DynamoDB rejects unused expression names
        return _conditional(lambda: self.table.update_item(
            Key={"execution_name": execution_name},
            # The same shape as the wrapper's own post-wait writes (Claim_* states).
            UpdateExpression=update + " REMOVE wait_token, #att, #due",
            ConditionExpression="wait_token = :t AND started_at = :s AND #s IN (:waiting, :paused)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={**values, ":t": token, ":s": started_at,
                                       ":waiting": WAITING, ":paused": WAITING_PAUSED},
        ))

    def clear_attention(self, execution_name: str, status: str, started_at: Optional[str]) -> bool:
        """Drop a row that left the waiting statuses from the attention index (ADR #130 §3 step 6)."""
        values: Dict[str, Any] = {":status": status}
        condition = "attribute_exists(execution_name) AND #s = :status"
        if started_at is None:
            condition += " AND attribute_not_exists(started_at)"
        else:
            condition += " AND started_at = :s"
            values[":s"] = started_at
        return _conditional(lambda: self.table.update_item(
            Key={"execution_name": execution_name},
            UpdateExpression="REMOVE #att, #due",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status", "#att": att.KEY, "#due": att.DUE},
            ExpressionAttributeValues=values,
        ))

    def mark_due(self, execution_name: str, started_at: str, due: datetime, attempts: Optional[int] = None) -> bool:
        """Set the next sweeper check of a row still in the attention index."""
        update = "SET #due = :due"
        values: Dict[str, Any] = {":due": format_utc(due), ":s": started_at}
        if attempts is not None:
            update += ", attempts = :n"
            values[":n"] = attempts
        return _conditional(lambda: self.table.update_item(
            Key={"execution_name": execution_name},
            UpdateExpression=update,
            ConditionExpression="started_at = :s AND attribute_exists(#att)",
            ExpressionAttributeNames={"#att": att.KEY, "#due": att.DUE},
            ExpressionAttributeValues=values,
        ))

    def put(self, item: Dict[str, Any]) -> None:
        self.table.put_item(Item=item)

    def run_rows(self, pipeline_execution: str) -> List[Dict[str, Any]]:
        """The run's rows still in a reconcilable status (candidates; GSI read)."""
        rows: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "IndexName": env.PIPELINE_EXECUTION_INDEX,
            "KeyConditionExpression": Key("pipeline_execution").eq(pipeline_execution),
        }
        while True:
            response = self.table.query(**kwargs)
            rows.extend(r for r in response.get("Items", [])
                        if r.get("status") in RECONCILABLE and not str(r["execution_name"]).startswith("_"))
            if "LastEvaluatedKey" not in response:
                return rows
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


class SubscriptionStore:
    """Pointers in ``dependency-subscriptions``."""

    def __init__(self, resource: Any, table_name: str):
        self.table = resource.Table(table_name)

    def delete(self, dependency_key: str, subscriber: str) -> None:
        self.table.delete_item(Key={"dependency_key": dependency_key, "subscriber": subscriber})


class Registry:
    """Outlet granularity declared in producers' registry entries.

    Only facts written before ADR #130 lack a stored granularity. One
    instance lives for one invocation, so a redeploy is seen on the next one.
    """

    def __init__(self, resource: Any, table_name: str):
        self.table = resource.Table(table_name)
        self._cache: Dict[Tuple[str, str], Optional[str]] = {}

    def declared_granularity(self, source_dag: str, asset_name: str) -> Optional[str]:
        key = (source_dag, asset_name)
        if key not in self._cache:
            self._cache[key] = self._lookup(source_dag, asset_name)
        return self._cache[key]

    def _lookup(self, source_dag: str, asset_name: str) -> Optional[str]:
        if not source_dag:
            return None
        item = self.table.get_item(Key={"pipeline_name": source_dag}, ConsistentRead=True).get("Item")
        raw = (item or {}).get("dag")
        if not raw:
            return None
        dag = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(dag, dict):
            raise ValueError(f"registry entry {source_dag!r} has a 'dag' of type {type(dag).__name__}, expected an object")
        for node in dag.get("nodes") or []:
            for outlet in node.get("outlets") or []:
                # Entries deployed by older SDKs list outlets as bare names.
                if isinstance(outlet, dict) and outlet.get("name") == asset_name and outlet.get("granularity"):
                    return str(outlet["granularity"])
        return None

    def fact(self, item: Dict[str, Any]) -> Fact:
        """Read an ``asset-events`` item; a missing granularity comes from the registry."""
        declared = None if item.get("granularity") else self.declared_granularity(
            str(item.get("source_dag", "")), str(item["asset_name"])
        )
        return fact_from_item(item, declared_granularity=declared)


class FactStore:
    """``FactReader`` over the partition index plus recent consistent reads (ADR #130 §4)."""

    def __init__(self, resource: Any, table_name: str, registry: Registry, now: datetime):
        self.table = resource.Table(table_name)
        self.registry = registry
        self.now = now

    def facts(self, asset_name: str, *, last: date, first: Optional[date]) -> Iterable[Fact]:
        return merge_facts(self._indexed(asset_name, last, first), self._recent(asset_name), first, last)

    def _indexed(self, asset_name: str, last: date, first: Optional[date]) -> Iterator[Fact]:
        condition = Key("asset_name").eq(asset_name) & (
            Key("partition_date").between(first.isoformat(), last.isoformat())
            if first is not None else Key("partition_date").lte(last.isoformat())
        )
        kwargs: Dict[str, Any] = {
            "IndexName": PARTITION_INDEX,
            "KeyConditionExpression": condition,
            "ScanIndexForward": False,
        }
        while True:
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                yield self.registry.fact(item)
            if "LastEvaluatedKey" not in response:
                return
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _recent(self, asset_name: str) -> List[Fact]:
        since = format_utc(self.now - env.RECENT_WRITES)
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("asset_name").eq(asset_name) & Key("event_time").gte(since),
            "ConsistentRead": True,
        }
        facts: List[Fact] = []
        while True:
            response = self.table.query(**kwargs)
            facts.extend(self.registry.fact(item) for item in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return facts
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
