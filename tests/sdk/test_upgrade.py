"""Tests for polyris/upgrade.py — polyris-up's move of a platform from 1.2 to 1.3 (ADR #137, *Migration and upgrade*).

Boundaries (Principle #14): DynamoDB runs on moto, and where a test needs an
index that is still being built, botocore's Stubber answers ``DescribeTable``.
Step Functions is a real boto3 client that ``fake_states.FakeStates`` answers,
so botocore validates each request against the service's model; the module's
own code runs as it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from moto import mock_aws

from fake_states import ACCOUNT, REGION, FakeStates, refuse
from polyris import platform as P
from polyris import up, upgrade
from polyris.coordination.times import format_utc

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
PREFIX = "acme-dev-polyris-"
TOKENS = f"{PREFIX}pipeline-tokens"
SPECS = f"{PREFIX}pipeline-specs"
STACK = {
    "StackName": "acme-dev",
    "Parameters": [{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                   {"ParameterKey": "Stage", "ParameterValue": "dev"}],
    "Outputs": [{"OutputKey": P.OUTPUT_TOKENS_TABLE, "OutputValue": TOKENS},
                {"OutputKey": P.OUTPUT_SPECS_TABLE, "OutputValue": SPECS}],
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


def test_a_target_names_the_platforms_state_machines_and_its_tables():
    target = _target()
    assert (target.stack_name, target.tokens_table, target.specs_table) == ("acme-dev", TOKENS, SPECS)
    assert target.name_prefix == PREFIX
    assert target.machine("dependency-wrapper") == (
        f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:acme-dev-polyris-dependency-wrapper")
    bare = _target({"StackName": "bare"})
    assert (bare.tokens_table, bare.specs_table) == ("", "")


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


# ── What 1.2 left running ────────────────────────────────────────────────────

UPDATED = NOW - timedelta(hours=2)                 # when the 1.3 update ran
BEFORE = UPDATED - timedelta(hours=1)
AFTER = UPDATED + timedelta(minutes=30)
WRAPPER, RUN_TASK, BACKFILL = PREFIX + "dependency-wrapper", PREFIX + "dep-run-task-helper", PREFIX + "bulk-backfill"
ORDERS = PREFIX + "orders"


@pytest.fixture
def left(states) -> FakeStates:
    """A 1.2 run that an upgrade left in flight, with the backfill that started it."""
    states.machine(ORDERS)
    states.run(RUN_TASK, "extract-work", started=BEFORE)
    states.run(WRAPPER, "load-wrapper", started=BEFORE)
    states.run(WRAPPER, "extract-wrapper", started=BEFORE - timedelta(seconds=1))
    states.run(PREFIX + "pause-waiter", "load-pause", started=BEFORE)
    states.run(ORDERS, "orders-run-2026-11-22-ab12cd34", started=BEFORE)
    states.run(BACKFILL, "backfill-orders-2026-11", started=BEFORE)
    return states


def _stop(states, *, pipelines=(ORDERS,), started_before=UPDATED):
    slept: list = []
    stopped = upgrade.stop_leftovers(
        states.client, target=_target(), pipelines=[f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}"
                                                    for name in pipelines],
        started_before=started_before, sleep=slept.append)
    return stopped, slept


def test_the_stop_ends_what_1_2_left_running_and_what_starts_others_first(left):
    stopped, slept = _stop(left)

    assert left.running() == []
    # bulk_backfill, the pipelines' executions and the wrappers; then run_task.
    assert left.stopped == ["backfill-orders-2026-11", "orders-run-2026-11-22-ab12cd34", "load-wrapper",
                            "extract-wrapper", "load-pause", "extract-work"]
    assert stopped.runs == [upgrade.StoppedRun(machine=BACKFILL, name="backfill-orders-2026-11", started=BEFORE),
                            upgrade.StoppedRun(machine=ORDERS, name="orders-run-2026-11-22-ab12cd34", started=BEFORE)]
    assert stopped.engine == 4
    assert slept == [upgrade.STOP_SETTLE_S], "one more listing, which showed nothing new"
    assert {(stop["error"], stop["cause"]) for stop in left.stops} == {(upgrade.STOP_ERROR, upgrade.STOP_CAUSE)}


def test_the_stop_keeps_what_started_after_the_update(left):
    """A pipeline redeployed meanwhile, from CI or another terminal, keeps its first 1.3 run."""
    left.run(ORDERS, "orders-run-2026-11-23-0f0f0f0f", started=AFTER)
    left.run(RUN_TASK, "extract-work-of-1-3", started=AFTER)
    left.run(BACKFILL, "backfill-of-1-3", started=AFTER)

    _stop(left)

    assert left.running() == ["backfill-of-1-3", "extract-work-of-1-3", "orders-run-2026-11-23-0f0f0f0f"]


def test_the_stop_ends_every_execution_of_a_state_machine_1_3_deletes_whenever_it_started(left):
    """Once it is being deleted, a state machine of 1.2 can have no execution of 1.3."""
    left.run(WRAPPER, "started-during-the-update", started=AFTER)

    _stop(left)

    assert "started-during-the-update" in left.stopped


def test_without_the_time_of_the_update_the_stop_ends_only_what_1_3_deletes(left):
    stopped, _ = _stop(left, started_before=None)

    assert sorted(left.stopped) == ["extract-wrapper", "load-pause", "load-wrapper"]
    assert (stopped.runs, stopped.engine) == ([], 3)
    listed = {params["stateMachineArn"].rsplit(":", 1)[-1] for name, params in left.calls if name == "ListExecutions"}
    assert listed == {PREFIX + name for name in P.REMOVED_STANDARD_MACHINES}


def test_the_stop_lists_again_until_a_listing_shows_nothing_new(left):
    """A wrapper can start run_task between a listing and its stop, and ListExecutions shows a new
    execution only after a moment."""
    def a_wrapper_starts_its_work(name: str) -> None:
        if name == "load-wrapper":
            left.run(RUN_TASK, "load-work", started=BEFORE, hidden=1)

    left.on_stop = a_wrapper_starts_its_work

    stopped, slept = _stop(left)

    assert left.running() == [] and left.stopped[-1] == "load-work"
    assert stopped.engine == 5
    assert slept == [upgrade.STOP_SETTLE_S] * 2


def test_the_stop_ends_an_execution_once_though_listings_lag_behind_it(states):
    states.run(WRAPPER, "extract-wrapper", started=BEFORE, lingers=3)

    stopped, _ = _stop(states)

    assert states.stopped == ["extract-wrapper"] and stopped.engine == 1


def test_the_stop_is_idempotent(left):
    _stop(left)

    stopped, slept = _stop(left)

    assert (stopped.runs, stopped.engine, slept) == ([], 0, [])
    assert len(left.stops) == 6


def test_the_stop_reads_every_page_of_a_listing(states):
    states.page_size = 2
    for n in range(5):
        states.run(WRAPPER, f"wrapper-{n}", started=BEFORE - timedelta(minutes=n))

    stopped, _ = _stop(states)

    assert states.running() == [] and stopped.engine == 5


def test_the_stop_passes_over_what_is_gone(left):
    """A state machine that CloudFormation has deleted, a pipeline destroyed since it registered, and
    an execution that ended between its listing and its stop."""
    del left.machines[_target().machine("failure-handler")]
    left.gone_on_stop.add("load-pause")

    stopped, _ = _stop(left, pipelines=(ORDERS, PREFIX + "destroyed"))

    assert "load-pause" not in left.stopped and stopped.engine == 3
    assert len(stopped.runs) == 2


def test_the_stop_touches_no_other_state_machine(left):
    """Express executions end within five minutes on their own, and a test task's is a task's work."""
    left.run(PREFIX + "test-success", "tried-by-hand", started=BEFORE)

    _stop(left)

    assert left.running() == ["tried-by-hand"]
    listed = {params["stateMachineArn"].rsplit(":", 1)[-1] for name, params in left.calls if name == "ListExecutions"}
    assert listed == {PREFIX + name for name in STANDARD_1_2} | {ORDERS}


@pytest.mark.parametrize("operation", ["ListExecutions", "StopExecution"])
def test_a_denied_call_of_the_stop_reaches_the_caller(left, operation):
    """polyris-up says what could not be stopped and how to go on; nothing is passed over in silence."""
    left.denied.add(operation)

    with pytest.raises(ClientError, match="AccessDeniedException"):
        _stop(left)


def test_the_1_3_update_ran_when_it_created_pipeline_specs(dynamodb):
    assert upgrade.rows_engine_since(dynamodb, _target()) is None, "no such table: 1.3 was never installed"

    dynamodb.create_table(TableName=SPECS, BillingMode="PAY_PER_REQUEST",
                          KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                          AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
    since = upgrade.rows_engine_since(dynamodb, _target())

    assert since == dynamodb.describe_table(TableName=SPECS)["Table"]["CreationDateTime"]
    assert since.tzinfo is not None, "it is compared with an execution's start"
    assert upgrade.rows_engine_since(dynamodb, _target({"StackName": "a 1.2 stack names no such table"})) is None


def test_a_table_that_cannot_be_read_is_not_taken_for_a_missing_one(dynamodb):
    refuse(dynamodb, "DescribeTable")

    with pytest.raises(ClientError, match="AccessDeniedException"):
        upgrade.rows_engine_since(dynamodb, _target())


# ── The index that 1.3 adds ──────────────────────────────────────────────────


def _table(*indexes: tuple) -> dict:
    """DescribeTable's answer for the tasks table with these (name, status) indexes."""
    described = {"TableName": TOKENS}
    if indexes:
        described["GlobalSecondaryIndexes"] = [{"IndexName": name, "IndexStatus": status} for name, status in indexes]
    return {"Table": described}


BUILDING = _table(("attention-index", "ACTIVE"), ("run-start-index", "CREATING"))
BUILT = _table(("attention-index", "ACTIVE"), ("run-start-index", "ACTIVE"))


@pytest.fixture
def described():
    """A DynamoDB client whose DescribeTable gives the answers a test queues, in order."""
    client = boto3.client("dynamodb", region_name=REGION, aws_access_key_id="testing",
                          aws_secret_access_key="testing")
    with Stubber(client) as stubber:
        def queue(*answers: dict):
            for answer in answers:
                stubber.add_response("describe_table", answer, {"TableName": TOKENS})
            return client

        yield queue
        stubber.assert_no_pending_responses()


def _wait(client, target=None):
    slept: list = []
    return upgrade.wait_for_index(client, target or _target(), sleep=slept.append), slept


def test_an_index_that_is_active_needs_no_wait(described, capsys):
    assert _wait(described(BUILT)) == (True, [])
    assert capsys.readouterr().out == ""


def test_the_wait_says_that_it_waits_until_dynamodb_has_built_the_index(described, capsys):
    """CloudFormation reports the table updated while the index is still being built (TR-0 A3)."""
    assert _wait(described(BUILDING, BUILDING, BUILT)) == (True, [upgrade.INDEX_POLL_S] * 2)

    out = capsys.readouterr().out
    assert out.count(f"waiting for DynamoDB to build run-start-index on {TOKENS}") == 1
    assert out.rstrip().endswith("run-start-index is active")


def test_the_wait_ends_after_its_hour_and_says_what_follows(described, capsys, monkeypatch):
    monkeypatch.setattr(upgrade, "INDEX_WAIT_S", 2 * upgrade.INDEX_POLL_S)

    assert _wait(described(BUILDING, BUILDING, BUILDING)) == (False, [upgrade.INDEX_POLL_S] * 2)

    assert "run-start-index is still being built. The console's run lists say so" in capsys.readouterr().out


@pytest.mark.parametrize("table", [_table(("attention-index", "ACTIVE")), _table()], ids=["other indexes", "none"])
def test_a_tasks_table_without_the_index_is_said(described, capsys, table):
    assert _wait(described(table)) == (False, [])
    assert f"{TOKENS} has no run-start-index" in capsys.readouterr().out


def test_a_stack_that_names_no_tasks_table_has_no_index_to_wait_for(described):
    assert _wait(described(), _target({"StackName": "bare"})) == (False, [])

