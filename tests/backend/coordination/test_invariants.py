"""The invariant check the World runs after every step (invariants.py) finds what it should.

Each test writes a state that breaks one invariant, or one that only looks
like it does, straight into the tables, and asks the check about it.
"""

import json

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from invariants import WAIT_STATE, Invariants

TOKENS, SUBSCRIPTIONS = "tokens", "subscriptions"
WRAPPER = "arn:aws:states:us-east-1:123456789012:execution:wrapper:load"
_serializer = TypeSerializer()


class Runtime:
    """The runtime's record of accepted callbacks, which invariant 1 and 4 read."""

    def __init__(self):
        self.callbacks = []

    def accept(self, execution=WRAPPER, state=WAIT_STATE):
        self.callbacks.append({"execution": execution, "state": state, "outcome": {}})


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName=TOKENS, BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}])
        client.create_table(TableName=SUBSCRIPTIONS, BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": "dependency_key", "KeyType": "HASH"},
                                       {"AttributeName": "subscriber", "KeyType": "RANGE"}],
                            AttributeDefinitions=[{"AttributeName": "dependency_key", "AttributeType": "S"},
                                                  {"AttributeName": "subscriber", "AttributeType": "S"}])
        yield client


@pytest.fixture
def runtime():
    return Runtime()


@pytest.fixture
def check(ddb, runtime):
    return Invariants(ddb, runtime, {"PipelineTokensTable": TOKENS, "DependencySubscriptionsTable": SUBSCRIPTIONS})


def _put(ddb, table, **item):
    ddb.put_item(TableName=table, Item={k: _serializer.serialize(v) for k, v in item.items()})


def _row(ddb, status, **extra):
    fields = {"execution_name": "load", "status": status, "started_at": "s1", "dependencies": json.dumps(["extract"]),
              "wait_for": "[]", "pipeline_execution_short": "-S", "wrapper_execution_arn": WRAPPER,
              "attention": "waiting#3", **extra}
    _put(ddb, TOKENS, **{k: v for k, v in fields.items() if v is not None})


def _pointer(ddb):
    _put(ddb, SUBSCRIPTIONS, dependency_key="extract--S", subscriber="load")


def test_a_consistent_waiting_row_passes(ddb, check):
    _row(ddb, "waiting")
    _pointer(ddb)
    check.observe()
    assert check.violations == []


def test_invariant_1_a_wrapper_signalled_twice(ddb, runtime, check):
    runtime.accept()
    runtime.accept()
    check.observe()
    assert check.violations == [f"invariant 1: wrapper {WRAPPER} accepted 2 dependency signals"]


def test_invariant_3_a_status_that_moves_back(ddb, check):
    _row(ddb, "running", attention=None)
    check.observe()
    _row(ddb, "deps_ready", attention=None)
    check.observe()
    assert check.violations == ["invariant 3: load went from running back to deps_ready"]


def test_invariant_3_a_new_wrapper_may_start_over(ddb, check):
    _row(ddb, "success", attention=None)
    check.observe()
    _row(ddb, "waiting", started_at="s2", wrapper_execution_arn=WRAPPER + "-restart")
    _pointer(ddb)
    check.observe()
    assert check.violations == []


def test_invariant_3_the_same_wrapper_registering_again_may_not(ddb, check):
    _row(ddb, "skipped", attention=None)                     # an operator resolved it
    check.observe()
    _row(ddb, "waiting", started_at="s2")                    # its wrapper timed out and registered again
    _pointer(ddb)
    check.observe()
    assert check.violations == ["invariant 3: load went from skipped back to waiting"]


def test_invariant_3_a_pause_hold_at_run_start_is_not_a_step_back(ddb, check):
    _row(ddb, "deps_ready", attention=None)
    check.observe()
    _row(ddb, "waiting_paused", pause_token="p", attention="paused#3")
    check.observe()
    assert check.violations == []


def test_invariant_4_pointers_gone_before_any_signal(ddb, check):
    _row(ddb, "waiting")
    check.observe()
    assert check.violations == ["invariant 4: load is still waiting, but its pointers ['extract--S'] are gone"]


def test_invariant_4_pointers_may_go_once_the_signal_was_accepted(ddb, runtime, check):
    _row(ddb, "waiting")
    runtime.accept()
    check.observe()
    assert check.violations == []


@pytest.mark.parametrize("status,extra,message", [
    ("waiting", {"attention": None}, "invariant 5: load is waiting without a waiting# entry"),
    ("waiting_paused", {"attention": None}, "invariant 5: load is waiting_paused without a waiting# entry"),
    ("waiting_paused", {"pause_token": "p"}, "invariant 5: load is held in its pause wait without a paused# entry"),
])
def test_invariant_5_a_row_missing_its_attention_entry(ddb, check, status, extra, message):
    _row(ddb, status, **extra)
    _pointer(ddb)
    check.observe()
    assert check.violations == [message]
