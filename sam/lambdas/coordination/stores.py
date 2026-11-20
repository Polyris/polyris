"""Reads of facts for the dispatcher and AssetFanout (ADR #130 §4, ADR #137 §7).

``FactStore`` reads an asset's facts from the partition index plus a
consistent read of the recent writes, which closes the index's lag;
``Registry`` gives the granularity of a fact written before ADR #130 from its
producer's registry entry.
"""

import json
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from boto3.dynamodb.conditions import Key

import env
from polyris.coordination.asset_conditions import Fact, merge_facts
from polyris.coordination.records import PARTITION_INDEX, fact_from_item
from polyris.coordination.times import format_utc


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
    """``FactReader`` over the partition index plus recent consistent reads (ADR #130 §4).

    A fact that cannot be read (a malformed partition, an unknown granularity)
    counts for no task, and ``on_unreadable`` is told about it: one bad fact
    must not fail the decision of every task waiting on its asset (B-F2).
    """

    def __init__(self, resource: Any, table_name: str, registry: Registry, now: datetime, *,
                 on_unreadable: Optional[Callable[[Dict[str, Any], Exception], None]] = None):
        self.table = resource.Table(table_name)
        self.registry = registry
        self.now = now
        self.on_unreadable = on_unreadable

    def read(self, item: Dict[str, Any]) -> Optional[Fact]:
        """The fact of an ``asset-events`` item, or None when it cannot be read."""
        try:
            return self.registry.fact(item)
        except ValueError as exc:
            if self.on_unreadable is not None:
                self.on_unreadable(item, exc)
            return None

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
                fact = self.read(item)
                if fact is not None:
                    yield fact
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
            facts.extend(f for f in map(self.read, response.get("Items", [])) if f is not None)
            if "LastEvaluatedKey" not in response:
                return facts
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
