"""AssetFanout on moto, fed real stream records of the template-built table (ADR #130 §6.1)."""

import pytest
from coordination_testkit import DATE, TABLES, client_error, execution_name, fact, task_row

import asset_fanout
from failures import CoordinationTransient

ORDERS = [{"asset_name": "ns/orders", "freshness_hours": None}]


@pytest.fixture
def fanout(reconciler):
    return asset_fanout.AssetFanout(reconciler, subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1])


@pytest.fixture
def stream(ddb):
    """All records the asset-events stream holds so far, as the mapping hands them to Lambda."""
    import boto3

    client = ddb.meta.client
    arn = client.describe_table(TableName=TABLES["ASSET_EVENTS_TABLE"][1])["Table"]["LatestStreamArn"]
    streams = boto3.client("dynamodbstreams", region_name="us-east-1")

    def read():
        records = []
        for shard in streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"]:
            iterator = streams.get_shard_iterator(StreamArn=arn, ShardId=shard["ShardId"],
                                                  ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            records += [{**r, "eventSourceARN": arn} for r in streams.get_records(ShardIterator=iterator)["Records"]]
        return records

    return read


def _waiting(tokens, subscriptions, task, wait_for=ORDERS):
    tokens.put_item(Item=task_row(task, wait_for=wait_for))
    subscriptions.put_item(Item={"dependency_key": "asset:ns/orders", "subscriber": execution_name(task)})


def _plenty():
    return 10 ** 6


def test_a_new_fact_wakes_every_task_waiting_on_its_asset(fanout, tokens, subscriptions, asset_events, stream, sfn):
    _waiting(tokens, subscriptions, "load")
    _waiting(tokens, subscriptions, "report")
    asset_events.put_item(Item=fact("ns/orders", DATE))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert sorted(s["token"] for s in sfn.succeeded) == ["token-load", "token-report"]


def test_a_fact_for_another_partition_wakes_nobody(fanout, tokens, subscriptions, asset_events, stream, sfn):
    _waiting(tokens, subscriptions, "load")
    asset_events.put_item(Item=fact("ns/orders", "2026-11-11"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert not sfn.succeeded


def test_the_fact_reaches_the_reconciler_as_a_known_event(fanout, tokens, subscriptions, asset_events, stream,
                                                          mocker):
    _waiting(tokens, subscriptions, "load")
    asset_events.put_item(Item=fact("ns/orders", DATE))
    spy = mocker.spy(fanout.reconciler, "reconcile")
    fanout.handle(stream(), _plenty)
    [call] = spy.call_args_list
    [known] = call.kwargs["known_events"]
    assert (known["asset_name"], known["partition_date"], call.kwargs["source"]) == ("ns/orders", DATE, "asset_fanout")


def test_records_other_than_inserts_are_ignored(fanout, tokens, subscriptions, asset_events, stream, sfn, mocker):
    item = fact("ns/orders", DATE)
    asset_events.put_item(Item=item)
    asset_events.update_item(Key={"asset_name": item["asset_name"], "event_time": item["event_time"]},
                             UpdateExpression="SET uri = :u", ExpressionAttributeValues={":u": "s3://moved"})
    records = stream()
    assert [r["eventName"] for r in records] == ["INSERT", "MODIFY"]
    spy = mocker.spy(fanout, "fan_out")
    fanout.handle(records, _plenty)
    assert spy.call_count == 1


def test_unreadable_subscribers_redeliver_the_record_and_every_later_one(fanout, asset_events, stream, mocker, sfn):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    asset_events.put_item(Item=fact("ns/orders", DATE, event_time="2026-11-12T05:00:00.000Z"))
    records = stream()
    mocker.patch.object(fanout.subscriptions, "query", side_effect=client_error("ThrottlingException", "Query"))
    result = fanout.handle(records, _plenty)
    assert result["batchItemFailures"] == [{"itemIdentifier": r["dynamodb"]["SequenceNumber"]} for r in records]


def test_a_non_transient_read_failure_becomes_a_warning_not_a_retry(fanout, tokens, asset_events, stream, mocker):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    mocker.patch.object(fanout.subscriptions, "query", side_effect=client_error("AccessDeniedException", "Query"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    warning = tokens.get_item(Key={"execution_name": "_notify_warn_coordination_asset:ns/orders"})["Item"]
    assert "AccessDeniedException" in warning["error"]


def test_a_warning_that_cannot_be_written_is_logged(fanout, tokens, asset_events, stream, mocker, capsys):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    mocker.patch.object(fanout.subscriptions, "query", side_effect=client_error("AccessDeniedException", "Query"))
    mocker.patch.object(fanout.reconciler.tasks.table, "put_item", side_effect=client_error("AccessDeniedException"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert "could not write the warning row" in capsys.readouterr().out


def test_a_lost_hint_is_left_to_the_sweeper_and_the_others_still_wake(fanout, tokens, subscriptions, asset_events,
                                                                      stream, sfn, mocker):
    _waiting(tokens, subscriptions, "load")
    _waiting(tokens, subscriptions, "report")
    asset_events.put_item(Item=fact("ns/orders", DATE))
    real = fanout.reconciler.reconcile

    def flaky(name, **kwargs):
        if name == execution_name("load"):
            raise CoordinationTransient("throttled")
        return real(name, **kwargs)

    mocker.patch.object(fanout.reconciler, "reconcile", side_effect=flaky)
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert [s["token"] for s in sfn.succeeded] == ["token-report"]


def test_records_left_when_time_runs_out_are_redelivered(fanout, asset_events, stream, mocker):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    records = stream()
    spy = mocker.spy(fanout, "fan_out")
    result = fanout.handle(records, lambda: asset_fanout.RESERVE_MS - 1)
    assert len(result["batchItemFailures"]) == len(records)
    spy.assert_not_called()


def test_subscription_pages_are_followed(fanout, mocker):
    pages = [{"Items": [{"subscriber": "a"}], "LastEvaluatedKey": {"k": 1}}, {"Items": [{"subscriber": "b"}]}]
    query = mocker.patch.object(fanout.subscriptions, "query", side_effect=pages)
    assert fanout._subscribers("ns/orders") == ["a", "b"]
    assert query.call_args_list[0].kwargs["ConsistentRead"] is True
    assert query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


def test_the_handler_reads_the_lambda_time_budget(fanout, monkeypatch, mocker):
    monkeypatch.setattr(asset_fanout, "_FANOUT", fanout)
    handle = mocker.spy(fanout, "handle")
    context = mocker.Mock(get_remaining_time_in_millis=lambda: 42)
    assert asset_fanout.handler({"Records": []}, context) == {"batchItemFailures": []}
    assert handle.call_args.args[1]() == 42
    assert asset_fanout.handler({}, None) == {"batchItemFailures": []}


def test_the_default_fanout_is_built_once(ddb, sfn, monkeypatch):
    import env

    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_stepfunctions", sfn)
    monkeypatch.setattr(asset_fanout, "_FANOUT", None)
    assert asset_fanout._fanout() is asset_fanout._fanout()
