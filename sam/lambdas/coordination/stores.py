"""Reads of facts and of a run's rows for the dispatcher and AssetFanout (ADR #130 §4, ADR #137 §7).

``FactStore`` reads an asset's facts from the partition index plus a
consistent read of the recent writes, which closes the index's lag. A fact
written before ADR #130 has no stored granularity: its key's format gives it
(``records.fact_from_item``); the registry lookup of 1.2 went with 1.1.4's
compatibility (ADR #137, *What is removed*).
"""

from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from boto3.dynamodb.conditions import Key

import env
from polyris.coordination.asset_conditions import Fact, merge_facts
from polyris.coordination import task_row
from polyris.coordination.records import PARTITION_INDEX, fact_from_item
from polyris.coordination.times import format_utc


class FactStore:
    """``FactReader`` over the partition index plus recent consistent reads (ADR #130 §4).

    A fact that cannot be read (a malformed partition, an unknown granularity)
    counts for no task, and ``on_unreadable`` is told about it: one bad fact
    must not fail the decision of every task waiting on its asset (B-F2).
    """

    def __init__(self, resource: Any, table_name: str, now: datetime, *,
                 on_unreadable: Optional[Callable[[Dict[str, Any], Exception], None]] = None):
        self.table = resource.Table(table_name)
        self.now = now
        self.on_unreadable = on_unreadable

    def read(self, item: Dict[str, Any]) -> Optional[Fact]:
        """The fact of an ``asset-events`` item, or None when it cannot be read."""
        try:
            return fact_from_item(item)
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


def run_row_keys(client: Any, table: str, pipeline_execution: str) -> List[str]:
    """The keys of the task rows that ``task_row.RUN_ROWS_INDEX`` files under a run, every page of them. The index
    is read eventually: a row it does not show yet waits for a later read."""
    kwargs: Dict[str, Any] = {
        "TableName": table, "IndexName": task_row.RUN_ROWS_INDEX, "KeyConditionExpression": "#run = :run",
        "ExpressionAttributeNames": {"#run": task_row.PIPELINE_EXECUTION},
        "ExpressionAttributeValues": {":run": {"S": pipeline_execution}}, "ProjectionExpression": task_row.TABLE_KEY}
    keys: List[str] = []
    while True:
        page = client.query(**kwargs)
        keys += [str(item[task_row.TABLE_KEY]["S"]) for item in page.get("Items", [])
                 if task_row.is_task_key(str(item[task_row.TABLE_KEY]["S"]))]
        if "LastEvaluatedKey" not in page:
            return keys
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

