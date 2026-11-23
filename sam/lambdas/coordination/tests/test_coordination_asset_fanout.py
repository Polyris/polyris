"""AssetFanout on moto, fed real stream records of the template-built table (ADR #130 §6.1, ADR #137 §7)."""

import pytest
from datetime import timedelta

from coordination_testkit import DATE, NOW, RUN, TABLES, client_error, execution_name, fact

import asset_fanout
from failures import CoordinationTransient
from polyris.coordination import run_row

ORDERS = [{"asset_name": "ns/orders", "freshness_hours": None}]
NOW_LATER = NOW + timedelta(hours=1)
OTHER = "orders-run-2026-11-12-0000ffff"


@pytest.fixture
def fanout(rows):
    return asset_fanout.AssetFanout(rows.dispatcher,
                                    subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1],
                                    consumers_table=TABLES["ASSET_SUBSCRIPTIONS_TABLE"][1])


@pytest.fixture
def stream(ddb):
    """All records the asset-events stream holds so far, as the mapping hands them to Lambda."""
    import boto3
    from botocore.config import Config

    client = ddb.meta.client
    arn = client.describe_table(TableName=TABLES["ASSET_EVENTS_TABLE"][1])["Table"]["LatestStreamArn"]
    # The regional endpoint: botocore 1.44.4 sends Streams calls to {account}.streams-ddb.{region},
    # which moto (5.2.5) does not serve yet, and answers 404 "Not yet implemented".
    streams = boto3.client("dynamodbstreams", region_name="us-east-1",
                           config=Config(account_id_endpoint_mode="disabled"))

    def read():
        records = []
        for shard in streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"]:
            iterator = streams.get_shard_iterator(StreamArn=arn, ShardId=shard["ShardId"],
                                                  ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
            records += [{**r, "eventSourceARN": arn} for r in streams.get_records(ShardIterator=iterator)["Records"]]
        return records

    return read


def _spec(task, wait_for=ORDERS, deps=()):
    return {"task_id": task, "task_type": "sfn", "dependencies": list(deps), "trigger_rule": "all_success",
            "wait_for": wait_for}


def _status(tokens, task, run=RUN):
    return tokens.get_item(Key={"execution_name": execution_name(task, run)}, ConsistentRead=True)["Item"]["status"]


def _plenty():
    return 10 ** 6


def test_a_new_fact_starts_every_task_waiting_on_its_asset(fanout, rows, tokens, asset_events, stream, sfn):
    rows.start([_spec("load"), _spec("report")])
    asset_events.put_item(Item=fact("ns/orders", DATE))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert (_status(tokens, "load"), _status(tokens, "report")) == ("deps_ready", "deps_ready")
    assert len(sfn.started) == 2


def test_a_fact_for_another_partition_starts_nobody(fanout, rows, tokens, asset_events, stream, sfn):
    rows.start([_spec("load")])
    asset_events.put_item(Item=fact("ns/orders", "2026-11-11"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert _status(tokens, "load") == "waiting" and not sfn.started


def test_one_narrow_pass_per_run_covers_its_rows_that_wait_on_the_asset(fanout, rows, asset_events, stream, mocker):
    rows.start([_spec("load"), _spec("report")])
    rows.start([_spec("load")], execution=OTHER)
    asset_events.put_item(Item=fact("ns/orders", DATE))
    narrow = mocker.spy(fanout.dispatcher, "narrow_pass")
    fanout.handle(stream(), _plenty)
    calls = {c.args[0]: sorted(c.args[1]) for c in narrow.call_args_list}
    assert calls == {run_row.key(RUN): [execution_name("load"), execution_name("report")],
                     run_row.key(OTHER): [execution_name("load", OTHER)]}
    assert {c.kwargs["source"] for c in narrow.call_args_list} == {"asset_fanout"}


def test_a_pointer_without_its_run_finds_it_through_its_row(fanout, rows, tokens, subscriptions, asset_events,
                                                            stream):
    rows.start([_spec("load")])
    subscriptions.put_item(Item={"dependency_key": "asset:ns/orders", "subscriber": execution_name("load")})
    asset_events.put_item(Item=fact("ns/orders", DATE))
    fanout.handle(stream(), _plenty)
    assert _status(tokens, "load") == "deps_ready"


def test_a_pointer_whose_row_is_gone_is_deleted(fanout, subscriptions, asset_events, stream):
    subscriptions.put_item(Item={"dependency_key": "asset:ns/orders", "subscriber": "gone-2026-11-12-run"})
    asset_events.put_item(Item=fact("ns/orders", DATE))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert subscriptions.scan()["Items"] == []


def test_a_pointer_whose_row_cannot_be_read_is_left(fanout, subscriptions, asset_events, stream, mocker):
    subscriptions.put_item(Item={"dependency_key": "asset:ns/orders", "subscriber": "x"})
    asset_events.put_item(Item=fact("ns/orders", DATE))
    mocker.patch.object(fanout.tasks, "get_item", side_effect=client_error("AccessDeniedException", "GetItem"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert len(subscriptions.scan()["Items"]) == 1


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
    mocker.patch.object(fanout.tasks, "put_item", side_effect=client_error("AccessDeniedException"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert "could not write the warning row" in capsys.readouterr().out


def test_a_pass_that_fails_transiently_is_a_lost_hint_and_the_other_runs_still_pass(
        fanout, rows, tokens, asset_events, stream, mocker):
    rows.start([_spec("load")])
    rows.start([_spec("load")], execution=OTHER)
    asset_events.put_item(Item=fact("ns/orders", DATE))
    real = fanout.dispatcher.narrow_pass

    def flaky(run_key, keys, **kwargs):
        if run_key == run_row.key(RUN):
            raise CoordinationTransient("throttled")
        return real(run_key, keys, **kwargs)

    mocker.patch.object(fanout.dispatcher, "narrow_pass", side_effect=flaky)
    fan_out = mocker.spy(fanout, "fan_out")
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert fan_out.spy_return == {"subscribers": 2, "runs": 2, "passed": 1, "lost": 1}
    assert (_status(tokens, "load"), _status(tokens, "load", OTHER)) == ("waiting", "deps_ready")


def test_records_left_when_time_runs_out_are_redelivered(fanout, asset_events, stream, mocker):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    records = stream()
    spy = mocker.spy(fanout, "fan_out")
    result = fanout.handle(records, lambda: asset_fanout.RESERVE_MS - 1)
    assert len(result["batchItemFailures"]) == len(records)
    spy.assert_not_called()


def test_a_record_that_runs_out_of_time_leaves_the_rest_to_the_runs_checks_and_moves_on(
        fanout, rows, asset_events, stream, mocker):
    """One record with more waiting runs than fit in the invocation never blocks its shard (Principle #41)."""
    for run in (RUN, OTHER, "orders-run-2026-11-12-0000eeee"):
        rows.start([_spec("load")], execution=run)
    asset_events.put_item(Item=fact("ns/orders", DATE))
    narrow = mocker.patch.object(fanout.dispatcher, "narrow_pass", return_value={})
    fan_out = mocker.spy(fanout, "fan_out")
    assert fanout.handle(stream(), lambda: 60_000 if narrow.call_count < 2 else asset_fanout.RECORD_RESERVE_MS - 1) \
        == {"batchItemFailures": []}
    assert fan_out.spy_return == {"subscribers": 3, "runs": 3, "passed": 2, "lost": 1}


def test_subscription_pages_are_followed(fanout, mocker):
    pages = [{"Items": [{"subscriber": "a", "run": "_run_r"}], "LastEvaluatedKey": {"k": 1}},
             {"Items": [{"subscriber": "b"}]}]
    query = mocker.patch.object(fanout.subscriptions, "query", side_effect=pages)
    assert fanout._subscribers("ns/orders") == [("a", "_run_r"), ("b", None)]
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

    import dispatcher

    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_stepfunctions", sfn)
    monkeypatch.setattr(asset_fanout, "_FANOUT", None)
    monkeypatch.setattr(dispatcher, "_DISPATCHER", None)
    assert asset_fanout._fanout() is asset_fanout._fanout()
    assert asset_fanout._fanout().dispatcher is dispatcher._dispatcher()



# ── push: pipelines scheduled on the asset ─────────────────────────────

PUSHED_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:pushed"


@pytest.fixture
def pushed(consumers, registry):
    """Register `pushed` with `schedule` (the registry form) on every asset it names."""
    import json

    def register(schedule, name="pushed"):
        registry.put_item(Item={"pipeline_name": name, "asset_schedule": json.dumps(schedule)})
        names = [m if isinstance(m, str) else m.get("asset_name") for m in schedule["assets"]]
        for asset in filter(None, names):
            consumers.put_item(Item={"asset_name": asset, "pipeline_name": name,
                                     "sfn_arn": PUSHED_ARN.replace("pushed", name)})
    return register


def _push(fanout, asset_events, item):
    asset_events.put_item(Item=item)
    return fanout.push(item)


def test_an_unreadable_fact_a_schedule_reads_counts_for_nothing_and_warns_once(fanout, asset_events, pushed, sfn,
                                                                               tokens):
    pushed({"operator": "AND", "assets": ["ns/a", "ns/b"]})
    asset_events.put_item(Item=fact("ns/b", DATE, event_time="2026-11-12T03:00:00.000Z", granularity="fortnightly"))
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:00:00.000Z"))
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:05:00.000Z"))
    assert sfn.started == []
    [warning] = [i for i in tokens.scan()["Items"] if i["execution_name"].startswith("_notify_warn_coordination_fact-")]
    assert warning["execution_name"].endswith("fact-ns/b-2026-11-12T03:00:00.000Z")
    assert "cannot be read, so it counts for no task" in warning["error"]


def test_an_and_schedule_starts_once_per_date_when_complete(fanout, asset_events, pushed, sfn):
    pushed({"operator": "AND", "assets": ["ns/a", "ns/b"]})
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:00:00.000Z"))
    assert sfn.started == []
    _push(fanout, asset_events, fact("ns/b", DATE, event_time="2026-11-12T04:10:00.000Z"))
    _push(fanout, asset_events, fact("ns/b", DATE, event_time="2026-11-12T04:20:00.000Z"))   # a repeated event
    [run] = sfn.started
    assert run["arn"] == PUSHED_ARN and run["name"].startswith(f"pushed-{DATE}-and-")
    assert run["input"] == {"triggered_by": "asset", "trigger_assets": ["ns/a", "ns/b"], "source_dag": "producer",
                            "execution_date": DATE, "current_date": DATE, "PARTITION_ARG": DATE,
                            "variables": {"current_date": DATE, "PARTITION_ARG": DATE}}


def test_an_or_schedule_starts_once_per_event(fanout, asset_events, pushed, sfn):
    pushed({"operator": "OR", "assets": ["ns/a", "ns/b"]})
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:00:00.000Z"))
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:10:00.000Z"))
    assert len({r["name"] for r in sfn.started}) == 2
    assert all(r["input"]["trigger_assets"] == ["ns/a"] for r in sfn.started)


@pytest.mark.parametrize("subscribed_at,starts", [
    (None, True),                               # a row without one: no cutover
    ("2026-11-12T03:00:00.000Z", True),         # subscribed before the fact
    ("2026-11-12T05:00:00.000Z", False),        # subscribed after it (B-U2)
])
def test_a_consumer_reacts_only_to_facts_recorded_after_it_subscribed(fanout, asset_events, consumers, pushed, sfn,
                                                                      subscribed_at, starts):
    pushed({"operator": "AND", "assets": ["ns/a"]})
    if subscribed_at:
        consumers.update_item(Key={"asset_name": "ns/a", "pipeline_name": "pushed"},
                              UpdateExpression="SET subscribed_at = :t", ExpressionAttributeValues={":t": subscribed_at})

    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:00:00.000Z"))

    assert len(sfn.started) == (1 if starts else 0)


def test_a_weekly_fact_starts_the_run_of_its_monday(fanout, asset_events, pushed, sfn):
    pushed({"operator": "AND", "assets": ["ns/weekly"]})
    _push(fanout, asset_events, fact("ns/weekly", "2026-W46"))
    assert sfn.started[0]["input"]["execution_date"] == "2026-11-09"


def test_within_is_judged_as_of_the_triggering_fact(fanout, asset_events, pushed, sfn):
    """NOW is 06:00; the fact is from 03:00. A replay or late record must give the first answer."""
    pushed({"operator": "AND", "assets": [{"asset_name": "ns/a", "freshness_hours": 1}]})
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T03:00:00.000Z"))
    assert len(sfn.started) == 1


def test_cascade_all_starts_regardless_and_cascade_none_never(fanout, asset_events, pushed, sfn):
    pushed({"operator": "AND", "assets": ["ns/a", "ns/b"]})
    _push(fanout, asset_events, fact("ns/a", DATE, cascade="none", event_time="2026-11-12T04:00:00.000Z"))
    assert sfn.started == []
    _push(fanout, asset_events, fact("ns/a", DATE, cascade="all", event_time="2026-11-12T04:10:00.000Z"))
    [run] = sfn.started
    assert "-ev-" in run["name"]


def test_a_pipeline_never_triggers_itself(fanout, asset_events, pushed, sfn):
    pushed({"operator": "AND", "assets": ["ns/a"]}, name="producer")
    _push(fanout, asset_events, fact("ns/a", DATE))
    assert sfn.started == []


def test_a_consumer_with_an_unreadable_schedule_is_reported_and_keeps_its_subscription(
        fanout, asset_events, consumers, registry, tokens, sfn):
    """Dropping it would leave a pipeline that never runs again to re-register (ADR #130 §6.1)."""
    consumers.put_item(Item={"asset_name": "ns/a", "pipeline_name": "pushed", "sfn_arn": PUSHED_ARN})
    registry.put_item(Item={"pipeline_name": "pushed", "asset_schedule": "not json"})
    _push(fanout, asset_events, fact("ns/a", DATE))
    assert sfn.started == []
    assert tokens.get_item(Key={"execution_name": "_notify_warn_push_push:pushed"}).get("Item")
    assert consumers.get_item(Key={"asset_name": "ns/a", "pipeline_name": "pushed"}).get("Item")


@pytest.mark.parametrize("registered", [
    None,                                                   # its registry entry is gone
    {"pipeline_name": "pushed", "asset_schedule": "{}"},    # switched to a cron: registration writes "{}"
    {"pipeline_name": "pushed", "asset_schedule": '{"operator": "AND", "assets": ["ns/b"]}'},   # dropped ns/a
], ids=["no-entry", "cron", "asset-dropped"])
def test_a_subscription_its_pipeline_no_longer_has_starts_nothing_and_is_dropped(
        fanout, asset_events, consumers, registry, tokens, sfn, registered):
    """B-U3: a stale subscription starts nothing, warns nothing, and is deleted."""
    consumers.put_item(Item={"asset_name": "ns/a", "pipeline_name": "pushed", "sfn_arn": PUSHED_ARN,
                             "registered_at": "2026-11-11T05:00:00.000Z"})
    if registered:
        registry.put_item(Item=registered)
    _push(fanout, asset_events, fact("ns/a", DATE))
    assert sfn.started == []
    assert "Item" not in tokens.get_item(Key={"execution_name": "_notify_warn_push_push:pushed"})
    assert "Item" not in consumers.get_item(Key={"asset_name": "ns/a", "pipeline_name": "pushed"})


def test_a_subscription_registered_again_meanwhile_is_kept(fanout, consumers, registry, mocker):
    """The pipeline re-registered between the read and the delete: its row is current."""
    consumers.put_item(Item={"asset_name": "ns/a", "pipeline_name": "pushed", "sfn_arn": PUSHED_ARN,
                             "registered_at": "2026-11-12T05:00:00.000Z"})
    stale = {"asset_name": "ns/a", "pipeline_name": "pushed", "sfn_arn": PUSHED_ARN,
             "registered_at": "2026-11-11T05:00:00.000Z"}
    fanout._drop_subscription(stale, "test")
    assert consumers.get_item(Key={"asset_name": "ns/a", "pipeline_name": "pushed"}).get("Item")


def test_a_deployed_pipeline_without_a_registry_schedule_is_reported_and_keeps_its_subscription(
        fanout, asset_events, consumers, registry, tokens, sfn):
    """Its registration failed (and warned then); dropping the row would lose a pipeline that never runs again."""
    consumers.put_item(Item={"asset_name": "ns/a", "pipeline_name": "pushed", "sfn_arn": PUSHED_ARN})
    registry.put_item(Item={"pipeline_name": "pushed", "registered_at": "2026-11-11T05:00:00+00:00"})
    _push(fanout, asset_events, fact("ns/a", DATE))
    assert sfn.started == []
    assert "deploy it again" in tokens.get_item(Key={"execution_name": "_notify_warn_push_push:pushed"})["Item"]["error"]
    assert consumers.get_item(Key={"asset_name": "ns/a", "pipeline_name": "pushed"}).get("Item")


@pytest.mark.parametrize("code,outcome", [
    ("ExecutionAlreadyExists", "nothing"), ("StateMachineDoesNotExist", "warning"),
    ("AccessDeniedException", "warning"), ("ThrottlingException", "retry"),
])
def test_start_failures(fanout, asset_events, pushed, sfn, tokens, code, outcome):
    pushed({"operator": "AND", "assets": ["ns/a"]})
    sfn.errors["start_execution"] = client_error(code, "StartExecution")
    _push(fanout, asset_events, fact("ns/a", DATE))
    rows = [i for i in tokens.scan()["Items"] if i["execution_name"].startswith("_")]
    kinds = {"_notify_warn_push": "warning", "_retry_push_": "retry"}
    assert ([next(v for k, v in kinds.items() if r["execution_name"].startswith(k)) for r in rows] or ["nothing"]) == [outcome]


def test_a_consumer_whose_state_machine_is_gone_loses_its_subscription(fanout, asset_events, consumers, pushed, sfn):
    """B-U3: one warning, then nothing: the next fact no longer tries to start it."""
    pushed({"operator": "AND", "assets": ["ns/a"]})
    sfn.errors["start_execution"] = client_error("StateMachineDoesNotExist", "StartExecution")
    _push(fanout, asset_events, fact("ns/a", DATE))
    assert "Item" not in consumers.get_item(Key={"asset_name": "ns/a", "pipeline_name": "pushed"})


def test_a_retry_row_keeps_its_first_failure(fanout, asset_events, pushed, sfn, tokens):
    pushed({"operator": "AND", "assets": ["ns/a"]})
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:00:00.000Z"))
    [row] = [i for i in tokens.scan()["Items"] if i["execution_name"].startswith("_retry_push_")]
    fanout.dispatcher.clock = lambda: NOW_LATER
    _push(fanout, asset_events, fact("ns/a", DATE, event_time="2026-11-12T04:10:00.000Z"))
    [again] = [i for i in tokens.scan()["Items"] if i["execution_name"].startswith("_retry_push_")]
    assert again["first_failed_at"] == row["first_failed_at"]


def test_a_retry_row_out_of_reach_redelivers_the_record(fanout, asset_events, pushed, sfn, mocker):
    pushed({"operator": "AND", "assets": ["ns/a"]})
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    mocker.patch.object(fanout.tasks, "put_item", side_effect=client_error("ThrottlingException"))
    with pytest.raises(CoordinationTransient):
        _push(fanout, asset_events, fact("ns/a", DATE))


def test_unreadable_consumers(fanout, asset_events, tokens, mocker):
    mocker.patch.object(fanout.consumers, "query", side_effect=client_error("ThrottlingException", "Query"))
    with pytest.raises(CoordinationTransient):
        fanout.push(fact("ns/a", DATE))
    mocker.patch.object(fanout.consumers, "query", side_effect=client_error("AccessDeniedException", "Query"))
    assert fanout.push(fact("ns/a", DATE)) == {"consumers": 0, "started": 0}
    assert tokens.get_item(Key={"execution_name": "_notify_warn_coordination_asset:ns/a"}).get("Item")


def test_consumer_pages_and_registry_errors(fanout, mocker):
    pages = [{"Items": [{"pipeline_name": "p"}], "LastEvaluatedKey": {"k": 1}}, {"Items": []}]
    query = mocker.patch.object(fanout.consumers, "query", side_effect=pages)
    assert fanout._consumers("ns/a") == [{"pipeline_name": "p"}]
    assert query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}
    mocker.patch.object(fanout.registry_table, "get_item", side_effect=client_error("ThrottlingException"))
    with pytest.raises(CoordinationTransient):
        fanout._registry_entry("p")
    mocker.patch.object(fanout.registry_table, "get_item", side_effect=client_error("AccessDeniedException"))
    with pytest.raises(Exception, match="AccessDenied"):
        fanout._registry_entry("p")



def test_a_poison_record_is_reported_and_the_shard_moves_on(fanout, rows, tokens, asset_events, stream, sfn):
    rows.start([_spec("load")])
    asset_events.put_item(Item={**fact("ns/orders", DATE, event_time="2026-11-12T04:00:00.000Z"),
                                "execution_date": "not-a-partition", "partition_key": "not-a-partition",
                                "partition_date": "not-a-partition"})
    asset_events.put_item(Item=fact("ns/orders", DATE, event_time="2026-11-12T04:30:00.000Z"))
    assert fanout.handle(stream(), _plenty) == {"batchItemFailures": []}
    assert sfn.started                           # the good record after it was handled
    warning = [i for i in tokens.scan()["Items"] if i["execution_name"].startswith("_notify_warn_coordination_record:")]
    assert len(warning) == 1 and "not-a-partition" in warning[0]["error"]


def test_a_raw_transient_error_still_redelivers(fanout, asset_events, stream, mocker):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    records = stream()
    mocker.patch.object(fanout, "fan_out", side_effect=client_error("ThrottlingException"))
    assert len(fanout.handle(records, _plenty)["batchItemFailures"]) == len(records)


def test_one_consumers_failure_does_not_stop_the_others(fanout, asset_events, pushed, sfn, tokens, mocker):
    pushed({"operator": "AND", "assets": ["ns/a"]}, name="first")
    pushed({"operator": "AND", "assets": ["ns/a"]}, name="second")
    real = fanout._push_one

    def broken_first(fact, row):
        if row["pipeline_name"] == "first":
            raise KeyError("sfn_arn")
        return real(fact, row)

    mocker.patch.object(fanout, "_push_one", side_effect=broken_first)
    _push(fanout, asset_events, fact("ns/a", DATE))
    assert [r["arn"].rsplit(":", 1)[1] for r in sfn.started] == ["second"]
    assert tokens.get_item(Key={"execution_name": "_notify_warn_push_push:first"}).get("Item")


def test_a_retry_row_denied_becomes_a_warning_not_a_blocked_shard(fanout, asset_events, pushed, sfn, mocker, capsys):
    pushed({"operator": "AND", "assets": ["ns/a"]})
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    mocker.patch.object(fanout.tasks, "put_item", side_effect=client_error("AccessDeniedException"))
    assert _push(fanout, asset_events, fact("ns/a", DATE)) == {"consumers": 1, "started": 0}
    assert "its retry row could not be written" in capsys.readouterr().out
