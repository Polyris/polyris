"""Tests for polyris/upgrade.py — polyris-up's move of a platform from 1.2 to 1.3 (ADR #137, *Migration and upgrade*).

Boundaries (Principle #14): DynamoDB runs on moto. Step Functions is a real
boto3 client that ``fake_states.FakeStates`` answers, so botocore validates
each request against the service's model; the module's own code runs as it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from fake_states import ACCOUNT, REGION, FakeStates, refuse
from polyris import platform as P
from polyris import up, upgrade
from polyris.coordination.times import format_utc

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
PREFIX = "acme-dev-polyris-"
TOKENS = f"{PREFIX}pipeline-tokens"
STACK = {
    "StackName": "acme-dev",
    "Parameters": [{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                   {"ParameterKey": "Stage", "ParameterValue": "dev"}],
    "Outputs": [{"OutputKey": P.OUTPUT_TOKENS_TABLE, "OutputValue": TOKENS}],
}
#: 1.2's thirteen state machines: the engine's six Standard ones, its four Express ones, and three test tasks.
STANDARD_1_2 = ("dependency-wrapper", "registration-helper", "failure-handler", "pause-waiter",
                "dep-run-task-helper", "bulk-backfill")
EXPRESS_1_2 = ("notify-dependents", "restart-task-helper", "restart-wrapper", "register-pipeline")
TEST_TASKS = ("test-quick", "test-success", "test-failure")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def dynamodb(monkeypatch):
    """The platform's tasks table on moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TOKENS, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}])
        yield client


@pytest.fixture
def states() -> FakeStates:
    """A 1.2 platform's state machines, with nothing running."""
    world = FakeStates()
    for name in STANDARD_1_2 + TEST_TASKS:
        world.machine(PREFIX + name)
    for name in EXPRESS_1_2:
        world.machine(PREFIX + name, express=True)
    return world


def _target(stack=STACK) -> upgrade.Target:
    return upgrade.Target.of(stack, partition="aws", region=REGION, account=ACCOUNT)


def _health(dynamodb, *, finished: datetime | None = NOW - timedelta(days=3), **fields: str) -> None:
    """Write ``_engine_health`` as 1.2's sweeper does: its self-check, and the adoption that ``finished``."""
    item = {"execution_name": "_engine_health", "checked_at": format_utc(NOW - timedelta(minutes=2))}
    if finished is not None:
        item.update(adopt_tasks_cursor="", adopt_tasks_until=format_utc(finished - timedelta(minutes=20)),
                    adopt_facts_cursor="", adopt_facts_until=format_utc(finished))
    item.update(fields)
    attributes = {name: {"S": value} for name, value in item.items()}
    if finished is not None:
        attributes["adoption_generation"] = {"N": "1"}
    dynamodb.put_item(TableName=TOKENS, Item=attributes)


def _check(dynamodb, states, *, deployed="1.2.3", override=False) -> None:
    upgrade.check(sfn=states.client, dynamodb=dynamodb, target=_target(), deployed=deployed, override=override,
                  now=NOW)


def _refusal(dynamodb, states, **kw) -> upgrade.Refused:
    with pytest.raises(upgrade.Refused) as refused:
        _check(dynamodb, states, **kw)
    return refused.value


# ── The target ───────────────────────────────────────────────────────────────


def test_a_target_names_the_platforms_state_machines_and_its_tasks_table():
    target = _target()
    assert target.stack_name == "acme-dev" and target.tokens_table == TOKENS
    assert target.machine("dependency-wrapper") == (
        f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:acme-dev-polyris-dependency-wrapper")
    assert _target({"StackName": "bare"}).tokens_table == ""


def test_the_check_lists_the_engines_standard_state_machines_of_1_2():
    assert set(upgrade.ENGINE_MACHINES) == set(STANDARD_1_2)


def test_the_flag_the_refusals_name_is_an_option_of_polyris_up():
    """The parser spells the flag as a literal (the docs test reads it there): keep the two the same."""
    assert up.build_up_parser().parse_args([upgrade.OVERRIDE_FLAG]).accept_stopped_runs is True


# ── The checks pass ──────────────────────────────────────────────────────────


def test_a_drained_1_2_platform_whose_adoption_settled_may_upgrade(dynamodb, states):
    _health(dynamodb)

    _check(dynamodb, states)

    listed = [params["stateMachineArn"] for name, params in states.calls if name == "ListExecutions"]
    assert listed == [_target().machine(name) for name in upgrade.ENGINE_MACHINES], "Standard ones only"
    assert all(params["statusFilter"] == "RUNNING" for _, params in states.calls)


def test_a_stack_without_a_version_is_1_2_when_it_has_the_health_row(dynamodb, states):
    """`sam deploy` stamps no version: the row that only 1.2 writes tells 1.2 from 1.1.4."""
    _health(dynamodb)
    _check(dynamodb, states, deployed=None)


def test_an_adoption_that_finished_48_hours_ago_may_upgrade(dynamodb, states):
    _health(dynamodb, finished=NOW - timedelta(hours=48))
    _check(dynamodb, states)


def test_what_is_no_run_in_flight_refuses_nothing(dynamodb, states):
    """A test task's execution is a task's work, an ended execution is over, and a state machine
    that is gone runs nothing."""
    _health(dynamodb)
    states.run(PREFIX + "test-success", "tried-by-hand", started=NOW)
    states.run(PREFIX + "dependency-wrapper", "yesterdays", started=NOW - timedelta(days=1)).status = "SUCCEEDED"
    del states.machines[_target().machine("pause-waiter")]

    _check(dynamodb, states)


# ── Each refusal ─────────────────────────────────────────────────────────────


def test_a_platform_older_than_1_2_is_refused_by_its_version(dynamodb, states):
    refused = _refusal(dynamodb, states, deployed="1.1.4")

    assert str(refused) == "acme-dev runs v1.1.4, and 1.3 upgrades a platform from 1.2 only."
    assert "Upgrade it to 1.2 first" in refused.todo[0] and len(refused.todo) == 1
    assert states.calls == [], "nothing else is checked"


def test_a_stack_without_a_version_and_without_the_health_row_is_older_than_1_2(dynamodb, states):
    refused = _refusal(dynamodb, states, deployed=None)

    assert "acme-dev looks older than 1.2.0" in str(refused) and "_engine_health" in str(refused)
    assert "Upgrade it to 1.2 first" in refused.todo[0]


def test_a_stack_that_names_no_version_and_no_tasks_table_is_older_than_1_2(dynamodb, states):
    """Without the table's name there is no health row to find, so nothing says it is 1.2."""
    with pytest.raises(upgrade.Refused, match="bare looks older than 1.2.0"):
        upgrade.check(sfn=states.client, dynamodb=dynamodb, target=_target({"StackName": "bare"}), deployed=None,
                      override=False, now=NOW)


@pytest.mark.parametrize("deployed", ["1.1.4", None])
def test_the_flag_never_covers_the_version(dynamodb, states, deployed):
    refused = _refusal(dynamodb, states, deployed=deployed, override=True)

    assert "1.3 upgrades a platform from 1.2 only" in str(refused)
    assert refused.todo[-1] == "--accept-stopped-runs does not cover this check."


@pytest.mark.parametrize("fields", [
    None,                                                       # the sweeper has not written the row yet
    {"finished": None},                                         # its self-check ran, its adoption not yet
    {"adopt_tasks_cursor": '{"execution_name": "t-2026"}'},     # the scan of task rows is under way
    {"adopt_facts_until": format_utc(NOW + timedelta(minutes=30))},   # the fact scan's hour of repeats still runs
], ids=["no row", "no adoption yet", "a scan under way", "repeats still run"])
def test_an_adoption_that_has_not_finished_is_refused(dynamodb, states, fields):
    if fields is not None:
        _health(dynamodb, **fields)

    refused = _refusal(dynamodb, states)

    assert str(refused) == ("1.2 has not finished adopting the rows and facts that older versions wrote on "
                            "acme-dev: its _engine_health row records no end.")
    assert "--accept-stopped-runs" in refused.todo[-1]
    assert "ListExecutions" not in states.operations(), "it stops at the first refusal"


def test_an_adoption_that_finished_less_than_48_hours_ago_is_refused_until_then(dynamodb, states):
    _health(dynamodb, finished=NOW - timedelta(hours=5, minutes=30))

    refused = _refusal(dynamodb, states)

    assert str(refused) == ("1.2 finished adopting older rows and facts on acme-dev 5 h ago. 1.3 can be installed "
                            "48 hours after that, from 2026-11-25 06:30 UTC.")
    assert "--accept-stopped-runs" in refused.todo[-1]


@pytest.mark.parametrize("machine", STANDARD_1_2)
def test_a_run_in_flight_is_refused(dynamodb, states, machine):
    _health(dynamodb)
    states.run(PREFIX + machine, "orders-run-2026-11-23-ab12cd34", started=NOW - timedelta(hours=1))

    refused = _refusal(dynamodb, states)

    assert str(refused) == f"A run is in flight on acme-dev: executions are running (1 of acme-dev-polyris-{machine})."
    assert "Nothing adopts a 1.2 run" in refused.todo[0] and "--accept-stopped-runs" in refused.todo[1]
    assert states.stopped == [], "a check stops nothing"


def test_the_refusal_counts_what_runs_on_each_state_machine(dynamodb, states):
    _health(dynamodb)
    states.page_size = 2
    for n in range(3):
        states.run(PREFIX + "dependency-wrapper", f"wrapper-{n}", started=NOW - timedelta(minutes=n))
    states.run(PREFIX + "dep-run-task-helper", "task-0", started=NOW)

    refused = _refusal(dynamodb, states)

    assert "(2 or more of acme-dev-polyris-dependency-wrapper, 1 of acme-dev-polyris-dep-run-task-helper)" in str(refused)


# ── The flag ─────────────────────────────────────────────────────────────────


def test_the_flag_skips_the_checks_of_the_adoption_and_of_the_drain(dynamodb, states):
    states.run(PREFIX + "dependency-wrapper", "orders-run-2026-11-23-ab12cd34", started=NOW)   # and no health row

    _check(dynamodb, states, override=True)

    assert states.calls == []


def test_the_flag_still_reads_the_health_row_of_a_stack_without_a_version(dynamodb, states):
    _health(dynamodb, finished=None)
    states.run(PREFIX + "dependency-wrapper", "orders-run-2026-11-23-ab12cd34", started=NOW)

    _check(dynamodb, states, deployed=None, override=True)


# ── A missing permission refuses, and names itself ───────────────────────────


def test_a_role_that_may_not_read_the_health_row_is_refused_with_the_permission(dynamodb, states):
    refuse(dynamodb, "GetItem")

    refused = _refusal(dynamodb, states)

    assert str(refused) == (f"polyris-up may not call dynamodb:GetItem on {TOKENS}, which its checks before the "
                            "upgrade to 1.3 need.")
    assert "IAM_PERMISSIONS.md" in refused.todo[0]


def test_a_role_that_may_not_list_executions_is_refused_with_the_permission(dynamodb, states):
    _health(dynamodb)
    states.denied.add("ListExecutions")

    refused = _refusal(dynamodb, states)

    assert str(refused) == (f"polyris-up may not call states:ListExecutions on {_target().machine('dependency-wrapper')}"
                            ", which its checks before the upgrade to 1.3 need.")


@pytest.mark.parametrize("failing", ["GetItem", "ListExecutions"])
def test_an_error_that_is_no_denial_is_not_a_refusal(dynamodb, states, failing):
    """A throttled call says nothing about the platform: it surfaces as the AWS error it is."""
    _health(dynamodb)
    refuse(dynamodb if failing == "GetItem" else states.client, failing, "ThrottlingException")

    with pytest.raises(ClientError, match="ThrottlingException"):
        _check(dynamodb, states)
