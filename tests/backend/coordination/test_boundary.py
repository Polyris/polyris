"""The harness's boto3 boundary (boundary.py): its log, its interleavings and its partial batches."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from boundary import READS, WRITES, Boundary, acting_as, as_component

TABLE = "tasks"


@pytest.fixture
def client():
    with mock_aws():
        c = boto3.client("dynamodb", region_name="us-east-1")
        c.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                       KeySchema=[{"AttributeName": "k", "KeyType": "HASH"}],
                       AttributeDefinitions=[{"AttributeName": "k", "AttributeType": "S"}])
        yield c


def put(client, key, status):
    client.put_item(TableName=TABLE, Item={"k": {"S": key}, "status": {"S": status}})


def status(client, key):
    return client.get_item(TableName=TABLE, Key={"k": {"S": key}}, ConsistentRead=True)["Item"]["status"]["S"]


def test_a_component_s_calls_are_logged_and_others_are_not(client):
    boundary = Boundary([client])
    put(client, "load", "waiting")                     # outside any component: the World's own call
    with acting_as("dispatcher"):
        status(client, "load")
        put(client, "load", "deps_ready")
    assert [(c.component, c.operation, c.tables, c.ok) for c in boundary.log] == [
        ("dispatcher", "GetItem", [TABLE], True), ("dispatcher", "PutItem", [TABLE], True)]


def test_a_refused_write_is_logged_with_its_error_and_still_raises(client):
    boundary = Boundary([client])
    put(client, "load", "running")
    with acting_as("run_task"), pytest.raises(ClientError):
        client.update_item(TableName=TABLE, Key={"k": {"S": "load"}}, UpdateExpression="SET #s = :s",
                           ConditionExpression="#s = :was", ExpressionAttributeNames={"#s": "status"},
                           ExpressionAttributeValues={":s": {"S": "success"}, ":was": {"S": "deps_ready"}})
    [call] = boundary.log
    assert (call.sent, call.error, call.ok) == (True, "ConditionalCheckFailedException", False)


def test_a_call_a_later_hook_stops_before_it_is_sent_is_logged_unsent(client):
    boundary = Boundary([client])

    def refuse(**_):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "injected"}}, "GetItem")

    client.meta.events.register("before-parameter-build.dynamodb.GetItem", refuse)
    with acting_as("sweeper"), pytest.raises(ClientError):
        client.get_item(TableName=TABLE, Key={"k": {"S": "load"}})
    [call] = boundary.log
    assert (call.sent, call.ok) == (False, False)


def test_an_action_between_a_read_and_the_callers_next_write_wins_the_race(client):
    """An operator's action lands after a pass read the row and before the pass writes: the pass's
    conditional write then fails, as the transaction of ADR #137 §1 must."""
    boundary = Boundary([client])
    put(client, "load", "waiting")

    def operator_skips():
        with acting_as("console"):
            put(client, "load", "skipped")

    boundary.between(after_read=lambda c: c.component == "dispatcher" and c.operation == "GetItem", then=operator_skips)
    with acting_as("dispatcher"):
        seen = status(client, "load")
        with pytest.raises(ClientError, match="ConditionalCheckFailed"):
            client.update_item(TableName=TABLE, Key={"k": {"S": "load"}}, UpdateExpression="SET #s = :s",
                               ConditionExpression="#s = :was", ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":s": {"S": "deps_ready"}, ":was": {"S": seen}})
    assert (seen, status(client, "load")) == ("waiting", "skipped")
    assert [c.component for c in boundary.log] == ["dispatcher", "console", "dispatcher"]


def test_an_action_before_a_write_is_sent_runs_first_and_only_as_often_as_asked(client):
    boundary = Boundary([client])
    order = []
    boundary.between(before_write=lambda c: c.operation == "PutItem", then=lambda: order.append("action"), times=1)
    with acting_as("dispatcher"):
        put(client, "a", "waiting")
        order.append("first write done")
        put(client, "b", "waiting")
    assert order == ["action", "first write done"]


def test_a_batch_get_answers_part_of_its_keys_as_unprocessed(client):
    boundary = Boundary([client])
    for key in ("a", "b", "c"):
        put(client, key, "waiting")
    boundary.unprocess(table=TABLE, keys=("k",), keep=1)
    keys = [{"k": {"S": key}} for key in ("a", "b", "c")]
    with acting_as("dispatcher"):
        first = client.batch_get_item(RequestItems={TABLE: {"Keys": keys}})
        second = client.batch_get_item(RequestItems={TABLE: {"Keys": keys}})
    assert len(first["Responses"][TABLE]) == 1
    assert len(first["UnprocessedKeys"][TABLE]["Keys"]) == 2
    assert len(second["Responses"][TABLE]) == 3 and not second.get("UnprocessedKeys")


def test_as_component_names_a_handler_s_calls(client):
    boundary = Boundary([client])
    handler = as_component("lambda:reconcile", lambda event, context: put(client, "load", "waiting"))
    handler({}, None)
    assert [c.component for c in boundary.log] == ["lambda:reconcile"]


def test_reads_and_writes_are_told_apart():
    assert "GetItem" in READS and "TransactWriteItems" in WRITES and not READS & WRITES


def test_a_write_whose_answer_is_lost_was_sent_but_not_answered(client):
    """As World.inject_failure(after=True) models it: the write applies, and its caller sees an error."""
    boundary = Boundary([client])

    def lose_the_answer(**_):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "injected"}}, "PutItem")

    client.meta.events.register("after-call.dynamodb.PutItem", lose_the_answer)
    with acting_as("run_task"), pytest.raises(ClientError):
        put(client, "load", "success")
    [call] = boundary.log
    assert (call.sent, call.error, call.answered, call.ok) == (True, None, False, False)
    assert status(client, "load") == "success"
