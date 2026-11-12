"""AssetFanout: wake the tasks that wait on a new asset fact (ADR #130 §6.1).

An event source mapping feeds it the asset-events stream (INSERT records
only). For each new fact it asks the reconciler about every task subscribed
to `asset:{name}`, passing the fact itself as a known event, so the decision
does not depend on how quickly the partition index catches up.

These are hints: a reconcile call that fails is logged and left to the
sweeper. A record is reported back for redelivery only when its subscribers
could not be read at all (transiently) or when the time budget ran out
before it was reached; redelivery is safe because every action is
idempotent. A non-transient read failure becomes a warning instead, so one
bad record never blocks its shard.
"""

from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

import env
from coordination_core.records import warning_item
from failures import CoordinationTransient, reraise_transient
from logger import log
from reconcile import Reconciler

#: Stop starting new records with this much of the invocation left.
RESERVE_MS = 10_000
_deserializer = TypeDeserializer()


def fact_of(record: Dict[str, Any]) -> Dict[str, Any]:
    """The asset-events item a stream record carries."""
    image = record["dynamodb"]["NewImage"]
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


class AssetFanout:
    def __init__(self, reconciler: Reconciler, *, subscriptions_table: str = env.SUBSCRIPTIONS_TABLE):
        self.reconciler = reconciler
        self.subscriptions = reconciler.resource.Table(subscriptions_table)

    def handle(self, records: List[Dict[str, Any]], remaining_ms: Callable[[], int]) -> Dict[str, Any]:
        """Process records in stream order; return the ones to redeliver.

        The stream redelivers from the first reported record, so processing
        stops there: later records are not handled twice.
        """
        for index, record in enumerate(records):
            if remaining_ms() < RESERVE_MS:
                log.warn("asset_fanout", "time budget reached; records left for redelivery", left=len(records) - index)
                return self._redeliver(records[index:])
            if record.get("eventName") != "INSERT":
                continue      # the mapping passes INSERTs only; anything else is not a new fact
            try:
                self.fan_out(fact_of(record))
            except CoordinationTransient as exc:
                log.error("asset_fanout", "subscribers could not be read; the record is redelivered",
                          sequence=str(record["dynamodb"]["SequenceNumber"]), error=str(exc))
                return self._redeliver(records[index:])
        return {"batchItemFailures": []}

    @staticmethod
    def _redeliver(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"batchItemFailures": [{"itemIdentifier": str(r["dynamodb"]["SequenceNumber"])} for r in records]}

    def fan_out(self, fact: Dict[str, Any]) -> Dict[str, int]:
        """Reconcile every task waiting on the fact's asset. Raises only CoordinationTransient."""
        asset = str(fact["asset_name"])
        subscribers = self._subscribers(asset)
        woken = lost = 0
        for name in subscribers:
            try:
                result = self.reconciler.reconcile(name, source="asset_fanout", known_events=[fact])
                woken += result["action"] == "signalled"
            except CoordinationTransient as exc:
                lost += 1
                # Principle #41: a lost hint delays the task; the sweeper re-checks it from facts.
                log.error("asset_fanout", "reconcile failed transiently; the sweeper re-checks the task",
                          execution_name=name, asset_name=asset, error=str(exc))
        counts = {"subscribers": len(subscribers), "woken": woken, "lost": lost}
        log.info("asset_fanout", "fanned out", asset_name=asset,
                 partition_key=str(fact.get("partition_key", fact.get("execution_date", ""))), **counts)
        return counts

    def _subscribers(self, asset: str) -> List[str]:
        """execution_names subscribed to `asset:{asset}` (consistent, all pages)."""
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("dependency_key").eq(f"asset:{asset}"),
            "ConsistentRead": True,
            "ProjectionExpression": "subscriber",
        }
        names: List[str] = []
        try:
            while True:
                response = self.subscriptions.query(**kwargs)
                names.extend(str(item["subscriber"]) for item in response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    return names
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            # Non-transient (AccessDenied, a missing table): redelivery cannot fix it and would block the shard.
            self._warn(asset, f"subscribers of asset {asset} could not be read: {type(exc).__name__}: {exc}")
            return []

    def _warn(self, asset: str, error: str) -> None:
        log.error("asset_fanout", "non-transient failure; the sweeper re-checks waiting tasks", error=error)
        item = warning_item(kind="coordination", execution_name=f"asset:{asset}", task_name="",
                            pipeline_name="", pipeline_execution="", task_date="", error=error,
                            now=self.reconciler.clock())
        try:
            self.reconciler.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            log.error("asset_fanout", "could not write the warning row", warning=item["execution_name"], error=str(exc))


_FANOUT: Optional[AssetFanout] = None


def _fanout() -> AssetFanout:
    global _FANOUT
    if _FANOUT is None:
        _FANOUT = AssetFanout(Reconciler(env.dynamodb(), env.stepfunctions()))
    return _FANOUT


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    remaining: Callable[[], int] = context.get_remaining_time_in_millis if context else (lambda: 10 ** 9)
    return _fanout().handle(event.get("Records", []), remaining)
