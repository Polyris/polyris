"""A table's stream as an event source mapping delivers it to its function (ADR #130 §6.1, ADR #137 §7).

Records are read from moto's stream, filtered with the mapping's FilterCriteria
(stream_filters.py), split into shards by their partition key, and handed to
the handler in batches of the mapping's BatchSize, shard by shard.

- A batch the handler reports partly failed (`batchItemFailures`) goes on from
  its first failed record, as Lambda checkpoints it: that record and the ones
  after it are delivered again, and the shard waits until then.
- A handler that raises holds its whole batch, and its shard, when the mapping
  retries (`hold_on_error`); otherwise the error reaches the test.
- The faults of ADR #137 *Tests*: `drop` loses matching records, `delay` holds
  them until `release`, and a deletion `mark_expired` names carries the TTL
  service's `userIdentity`, as DynamoDB marks a deletion by TTL.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import boto3
from botocore.config import Config

from stream_filters import matches_any

Record = Dict[str, Any]
#: How DynamoDB marks a deletion by TTL in its stream records.
TTL_IDENTITY = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}


@dataclass
class _Fault:
    matches: Callable[[Record], bool]
    left: Optional[int]


@dataclass
class StreamAdapter:
    ddb: Any
    table: str
    key: str                                  # the partition key, by which records are sharded
    patterns: List[Dict[str, Any]]
    batch_size: int
    shards: int = 1
    hold_on_error: bool = False
    page_size: int = 1000                     # GetRecords' Limit; AWS gives at most 1,000 records a page
    pending: List[List[Record]] = field(default_factory=list)
    dropped: List[Record] = field(default_factory=list)
    delayed: List[Record] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    _seen: Set[str] = field(default_factory=set)
    _drops: List[_Fault] = field(default_factory=list)
    _delays: List[_Fault] = field(default_factory=list)
    _expired: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.pending = [[] for _ in range(self.shards)]
        self.arn = self.ddb.describe_table(TableName=self.table)["Table"]["LatestStreamArn"]
        # The regional endpoint: botocore 1.44.4 sends Streams calls to {account}.streams-ddb.{region},
        # which moto (5.2.5) does not serve yet, and answers 404 "Not yet implemented".
        self.streams = boto3.client("dynamodbstreams", region_name="us-east-1",
                                    config=Config(account_id_endpoint_mode="disabled"))

    @classmethod
    def for_mapping(cls, stack: Any, mapping: str, *, table: str, key: str, shards: int = 1,
                    hold_on_error: bool = False) -> "StreamAdapter":
        """The adapter for one of the template's event source mappings, with its filters and batch size;
        `table` is the logical id of the table whose stream the mapping reads."""
        props = stack.resources[mapping]["Properties"]
        patterns = [json.loads(f["Pattern"]) for f in props.get("FilterCriteria", {}).get("Filters", [])]
        return cls(stack.ddb, stack.tables[table], key, patterns, int(props["BatchSize"]), shards, hold_on_error)

    # ── faults ─────────────────────────────────────────────────────────

    def drop(self, matches: Callable[[Record], bool], *, times: Optional[int] = None) -> None:
        """Lose the matching records before the mapping reads them."""
        self._drops.append(_Fault(matches, times))

    def delay(self, matches: Callable[[Record], bool], *, times: Optional[int] = None) -> None:
        """Hold the matching records back until `release`."""
        self._delays.append(_Fault(matches, times))

    def release(self) -> None:
        """Put the delayed records back in their shards, at the end."""
        for record in self.delayed:
            self.pending[self._shard(record)].append(record)
        self.delayed = []

    def heal(self) -> None:
        """No more records are lost or held, and the held ones arrive (World.run_to_the_end); a lost one stays lost."""
        self._drops.clear()
        self._delays.clear()
        self.release()

    def mark_expired(self, keys: Set[str]) -> None:
        """The deletions of these partition keys are TTL's own."""
        self._expired |= keys

    # ── delivery ───────────────────────────────────────────────────────

    def deliver(self, handler: Callable[[Any, Any], Any], *, select: Optional[Callable[[Record], bool]] = None) -> int:
        """Deliver every pending record, or those `select` picks, shard by shard; return how many the handler
        took, so a caller can tell when the stack has settled."""
        self._read()
        taken = 0
        for shard, records in enumerate(self.pending):
            chosen = [r for r in records if select is None or select(r)]
            held = [r for r in records if r not in chosen]
            left = self._deliver_shard(handler, chosen)
            taken += len(chosen) - len(left)
            self.pending[shard] = held + left
        return taken

    def _deliver_shard(self, handler: Callable[[Any, Any], Any], records: List[Record]) -> List[Record]:
        """Deliver one shard's records in order; return those still pending."""
        for start in range(0, len(records), self.batch_size):
            batch = records[start:start + self.batch_size]
            try:
                result = handler({"Records": batch}, None) or {}
            except Exception as exc:  # noqa: BLE001 — as the mapping retries a batch whose function raised
                if not self.hold_on_error:
                    raise
                self.errors.append(f"{type(exc).__name__}: {exc}")
                return records[start:]
            failed = {f["itemIdentifier"] for f in result.get("batchItemFailures", [])}
            if failed:
                first = next(i for i, r in enumerate(batch) if r["dynamodb"]["SequenceNumber"] in failed)
                return records[start + first:]
        return []

    def _read(self) -> None:
        """Read every new record, page by page (GetRecords gives at most 1,000 at a time), then trim them."""
        for shard in self.streams.describe_stream(StreamArn=self.arn)["StreamDescription"]["Shards"]:
            iterator = self.streams.get_shard_iterator(StreamArn=self.arn, ShardId=shard["ShardId"],
                                                       ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            while iterator:
                page = self.streams.get_records(ShardIterator=iterator, Limit=self.page_size)
                if not page["Records"]:
                    break
                for record in page["Records"]:
                    self._take_in(record)
                iterator = page.get("NextShardIterator")
        self._trim()

    def _take_in(self, record: Record) -> None:
        sequence = record["dynamodb"]["SequenceNumber"]
        if sequence in self._seen:
            return
        self._seen.add(sequence)
        record = {**record, "eventSourceARN": self.arn}
        if record["eventName"] == "REMOVE" and self._partition(record) in self._expired:
            record["userIdentity"] = dict(TTL_IDENTITY)
        if self._take(self._drops, record):
            self.dropped.append(record)
        elif self.patterns and not matches_any(self.patterns, record):
            return                               # the mapping's filters drop it, as Lambda does
        elif self._take(self._delays, record):
            self.delayed.append(record)
        else:
            self.pending[self._shard(record)].append(record)

    def _trim(self) -> None:
        """Drop the records just read from moto's shard; this adapter holds them now. moto copies a table, its
        stream included, in every transaction, so a scenario's stream would make each transaction slower than
        the last (each success of run_task is one). The sequence numbers go on from where they were, as a
        stream's trimming leaves them."""
        from moto.core import DEFAULT_ACCOUNT_ID
        from moto.dynamodb.models import dynamodb_backends

        table = dynamodb_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].tables.get(self.table)
        if table is not None and not hasattr(table, "stream_shard"):
            # moto renamed its private field: nothing would be trimmed, and each transaction would copy the whole
            # stream again, the harness slowing down with no cause shown (the review of TR-5).
            raise AssertionError("moto's table has no stream_shard any more: update StreamAdapter._trim")
        shard = getattr(table, "stream_shard", None)
        if shard is not None and shard.items:
            shard.starting_sequence_number += len(shard.items)
            shard.items = []

    @staticmethod
    def _take(faults: List[_Fault], record: Record) -> bool:
        for fault in faults:
            if (fault.left is None or fault.left > 0) and fault.matches(record):
                if fault.left is not None:
                    fault.left -= 1
                return True
        return False

    def _partition(self, record: Record) -> str:
        return next(iter(record["dynamodb"]["Keys"][self.key].values()))

    def _shard(self, record: Record) -> int:
        return zlib.crc32(self._partition(record).encode()) % self.shards
