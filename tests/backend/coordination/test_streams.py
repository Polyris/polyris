"""The harness's stream delivery (streams.py): filters, shards, Lambda's checkpoints and the faults of ADR #137."""

import zlib

import boto3
import pytest
from moto import mock_aws

from streams import TTL_IDENTITY, StreamAdapter

TABLE = "tasks"
INSERTS = [{"eventName": ["INSERT"]}]


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": "k", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "k", "AttributeType": "S"}],
                            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"})
        yield client


def put(ddb, key):
    ddb.put_item(TableName=TABLE, Item={"k": {"S": key}})


def keys(event):
    return [r["dynamodb"]["Keys"]["k"]["S"] for r in event["Records"]]


class Recorder:
    """A handler that records what it gets, and raises or reports failures as told."""

    def __init__(self, raise_for=(), fail_for=()):
        self.seen, self.raise_for, self.fail_for = [], set(raise_for), set(fail_for)

    def __call__(self, event, context):
        batch = keys(event)
        if self.raise_for & set(batch):
            raise RuntimeError("a transient error")
        self.seen += batch
        return {"batchItemFailures": [{"itemIdentifier": r["dynamodb"]["SequenceNumber"]}
                                      for r in event["Records"] if keys({"Records": [r]})[0] in self.fail_for]}


def on_shards(n):
    """Two keys on shard 0 and one on shard 1 of `n` shards."""
    by_shard = {}
    for i in range(100):
        by_shard.setdefault(zlib.crc32(f"key-{i}".encode()) % n, []).append(f"key-{i}")
    return by_shard[0][:2], by_shard[1][:1]


def test_the_mapping_s_filters_decide_what_is_delivered(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10)
    put(ddb, "a")
    ddb.delete_item(TableName=TABLE, Key={"k": {"S": "a"}})
    handler = Recorder()
    adapter.deliver(handler)
    assert handler.seen == ["a"]


def test_a_failing_shard_holds_only_itself(ddb):
    (first, second), (other,) = on_shards(2)
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10, shards=2, hold_on_error=True)
    for key in (first, other, second):
        put(ddb, key)
    adapter.deliver(Recorder(raise_for={first}))
    assert adapter.pending[0] and not adapter.pending[1]          # the other shard went on
    healed = Recorder()
    adapter.deliver(healed)
    assert healed.seen == [first, second] and not any(adapter.pending)


def test_a_partly_failed_batch_goes_on_from_its_first_failed_record(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10)
    for key in ("a", "b", "c"):
        put(ddb, key)
    first = Recorder(fail_for={"b"})
    adapter.deliver(first)
    again = Recorder()
    adapter.deliver(again)
    assert (first.seen, again.seen) == (["a", "b", "c"], ["b", "c"])   # as Lambda checkpoints at "b"


def test_without_hold_on_error_the_handler_s_error_reaches_the_test(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10)
    put(ddb, "a")
    with pytest.raises(RuntimeError):
        adapter.deliver(Recorder(raise_for={"a"}))


def test_a_dropped_record_is_never_delivered(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10)
    adapter.drop(lambda r: r["dynamodb"]["Keys"]["k"]["S"] == "b", times=1)
    for key in ("a", "b", "c"):
        put(ddb, key)
    handler = Recorder()
    adapter.deliver(handler)
    assert handler.seen == ["a", "c"] and [r["dynamodb"]["Keys"]["k"]["S"] for r in adapter.dropped] == ["b"]


def test_a_delayed_record_arrives_after_the_others_once_released(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10)
    adapter.delay(lambda r: r["dynamodb"]["Keys"]["k"]["S"] == "a")
    for key in ("a", "b"):
        put(ddb, key)
    handler = Recorder()
    adapter.deliver(handler)
    adapter.release()
    adapter.deliver(handler)
    assert handler.seen == ["b", "a"]


def test_a_deletion_by_ttl_carries_the_service_s_identity(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", [{"eventName": ["REMOVE"]}], batch_size=10)
    for key in ("expired", "deleted"):
        put(ddb, key)
        ddb.delete_item(TableName=TABLE, Key={"k": {"S": key}})
    adapter.mark_expired({"expired"})
    got = []
    adapter.deliver(lambda event, context: got.extend(event["Records"]) or {})
    identities = {r["dynamodb"]["Keys"]["k"]["S"]: r.get("userIdentity") for r in got}
    assert identities == {"expired": TTL_IDENTITY, "deleted": None}


def test_select_delivers_the_chosen_records_and_keeps_the_rest(ddb):
    adapter = StreamAdapter(ddb, TABLE, "k", INSERTS, batch_size=10)
    for key in ("a", "b"):
        put(ddb, key)
    chosen = Recorder()
    adapter.deliver(chosen, select=lambda r: r["dynamodb"]["Keys"]["k"]["S"] == "b")
    rest = Recorder()
    adapter.deliver(rest)
    assert (chosen.seen, rest.seen) == (["b"], ["a"])


def test_the_world_expires_rows_whose_ttl_passed_and_only_those(world):
    """World.expire_ttl deletes as DynamoDB's TTL does: invariant 11's scenarios rest on it."""
    from dags import chain
    from polyris.coordination import retention

    run = world.start_run(world.deploy(chain("orders", "extract")), "2026-11-12")
    world.deliver_task_hints()                            # a pass starts extract
    world.complete(run, "extract")
    assert world.expire_ttl() == []                       # every row's TTL lies ahead
    world.clock.advance(days=retention.HISTORY.days + 1)  # past the history's year and 90 days (D108)
    gone = world.expire_ttl()
    assert gone and world.row(run, "extract") is None
