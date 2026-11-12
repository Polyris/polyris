"""The sweeper (ADR #130 §8): lease, due rows, backoff, lost-hint warnings and zombies.

DynamoDB is moto with the tables of sam/template.yaml; Step Functions is the
FakeSfn of the test kit. The reconciler runs for real, in-process.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from coordination_testkit import NOW, execution_name, fact, task_row

import sweeper
from coordination_core import attention as att
from coordination_core import sweep
from coordination_core.times import format_utc
from failures import CoordinationTransient
from reconcile import Reconciler

ASSET = [{"asset_name": "ns/orders", "freshness_hours": None}]


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW + timedelta(minutes=30))


@pytest.fixture
def make(ddb, sfn, clock):
    def build(**kwargs: Any) -> sweeper.Sweeper:
        return sweeper.Sweeper(Reconciler(ddb, sfn, clock=clock), owner="me", **kwargs)
    return build


def _waiting(tokens, task: str, *, due: datetime, attempts: int = 0, **extra: Any) -> str:
    name = execution_name(task)
    tokens.put_item(Item=task_row(task, wait_for=ASSET, attention=att.attention(att.WAITING, name),
                                  attention_at=format_utc(due), attempts=attempts, **extra))
    return name


def _row(tokens, name: str) -> Dict[str, Any]:
    return tokens.get_item(Key={"execution_name": name}, ConsistentRead=True)["Item"]


def _warnings(tokens, kind: str) -> List[Dict[str, Any]]:
    return [i for i in tokens.scan()["Items"] if i["execution_name"].startswith(f"_notify_warn_{kind}_")]


# ── the lease ───────────────────────────────────────────────────────────


def test_a_run_without_the_lease_does_nothing(make, tokens, clock):
    tokens.put_item(Item={"execution_name": sweep.LEASE_KEY, "owner": "other",
                          "expires_at": format_utc(clock.now + timedelta(minutes=2))})
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))

    assert make().run() == {"skipped": "lease held"}
    assert _row(tokens, load)[att.DUE] == format_utc(clock.now - timedelta(minutes=5))


def test_an_expired_lease_is_taken_and_released_after_the_run(make, tokens, clock):
    tokens.put_item(Item={"execution_name": sweep.LEASE_KEY, "owner": "crashed",
                          "expires_at": format_utc(clock.now - timedelta(seconds=1))})

    result = make().run()

    assert result["checked"] == 0
    assert "Item" not in tokens.get_item(Key={"execution_name": sweep.LEASE_KEY})


def test_a_lease_taken_over_meanwhile_is_left_to_its_new_owner(make, tokens, clock, mocker):
    run = make()
    real = run.due_rows

    def steal(**kwargs: Any) -> Dict[str, Any]:
        tokens.put_item(Item={"execution_name": sweep.LEASE_KEY, "owner": "next", "expires_at": "x"})
        return real(**kwargs)

    mocker.patch.object(run, "due_rows", side_effect=steal)
    run.run()
    assert _row(tokens, sweep.LEASE_KEY)["owner"] == "next"


def test_a_lease_that_cannot_be_written_fails_the_run(make, tokens, mocker):
    from coordination_testkit import client_error

    run = make()
    mocker.patch.object(run.tasks.table, "put_item", side_effect=client_error("AccessDeniedException", "PutItem"))
    with pytest.raises(Exception, match="AccessDeniedException"):
        run.run()          # a failed scheduled run is visible in its logs and the health check


# ── due rows ────────────────────────────────────────────────────────────


def test_due_rows_are_checked_and_due_again_with_backoff(make, tokens, clock):
    first = _waiting(tokens, "load", due=clock.now - timedelta(minutes=20))
    second = _waiting(tokens, "report", due=clock.now - timedelta(minutes=1), attempts=2)
    later = _waiting(tokens, "audit", due=clock.now + timedelta(minutes=1))

    result = make().run()

    assert (result["checked"], result["woken"]) == (2, 0)
    assert (_row(tokens, first)[att.DUE], _row(tokens, first)["attempts"]) == (
        format_utc(clock.now + timedelta(minutes=10)), 1)
    assert (_row(tokens, second)[att.DUE], _row(tokens, second)["attempts"]) == (
        format_utc(clock.now + timedelta(minutes=30)), 3)
    assert _row(tokens, later)[att.DUE] == format_utc(clock.now + timedelta(minutes=1))


def test_every_page_of_due_rows_is_read(make, tokens, clock, mocker):
    # One shard, one row per page.
    names = [_waiting(tokens, f"t{i}", due=clock.now - timedelta(minutes=i + 1)) for i in range(3)]
    for name in names:
        tokens.update_item(Key={"execution_name": name}, UpdateExpression="SET #a = :p",
                           ExpressionAttributeNames={"#a": att.KEY}, ExpressionAttributeValues={":p": "waiting#0"})
    run = make()
    real = run.tasks.table.query
    mocker.patch.object(run.tasks.table, "query", side_effect=lambda **kw: real(**kw, Limit=1))

    assert run.run()["checked"] == 3
    assert all(_row(tokens, n)["attempts"] == 1 for n in names)


def test_the_budget_stops_a_run_and_leaves_the_rest_due(make, tokens, clock):
    _waiting(tokens, "load", due=clock.now - timedelta(minutes=20))
    later = _waiting(tokens, "report", due=clock.now - timedelta(minutes=10))
    ticks = iter([0.0, 0.0, 10_000.0])

    result = make(monotonic=lambda: next(ticks, 10_000.0)).run()

    assert (result["checked"], result["budget_reached"]) == (1, True)
    assert _row(tokens, later)[att.DUE] == format_utc(clock.now - timedelta(minutes=10))


def test_a_transient_failure_leaves_the_row_due(make, tokens, clock, mocker):
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    run = make()
    mocker.patch.object(run.reconciler, "reconcile", side_effect=CoordinationTransient("throttled"))

    assert run.run()["transient"] == 1
    assert _row(tokens, load)[att.DUE] == format_utc(clock.now - timedelta(minutes=5))


def test_a_failed_backoff_write_leaves_the_row_due(make, tokens, clock, mocker):
    from coordination_testkit import client_error

    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    run = make()
    mocker.patch.object(run.tasks, "mark_due", side_effect=client_error("ProvisionedThroughputExceededException"))

    assert run.run()["checked"] == 1
    assert _row(tokens, load)[att.DUE] == format_utc(clock.now - timedelta(minutes=5))


# ── woken by the sweeper ────────────────────────────────────────────────


def test_a_task_that_could_run_before_its_due_time_wakes_with_a_warning(make, tokens, asset_events, sfn, clock):
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:10:00.000Z"))

    result = make().run()

    assert (result["woken"], result["missed"]) == (1, 1)
    assert sfn.succeeded[0]["token"] == "token-load"
    [warning] = _warnings(tokens, "sweeper")
    assert warning["execution_name"] == f"_notify_warn_sweeper_{load}"
    assert (warning["task_date"], warning["pipeline_name"]) == ("2026-11-12", "orders")
    assert "2026-11-12T05:10:00.000Z" in warning["error"]


def test_a_warning_that_cannot_be_written_still_counts_the_miss(make, tokens, asset_events, clock, mocker):
    from coordination_testkit import client_error

    _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:10:00.000Z"))
    run = make()
    mocker.patch.object(run.tasks, "put", side_effect=client_error("AccessDeniedException", "PutItem"))

    assert run.run()["missed"] == 1


def test_a_fact_that_came_after_the_due_time_is_no_miss(make, tokens, asset_events, clock):
    _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time=format_utc(clock.now - timedelta(minutes=1))))

    assert make().run()["missed"] == 0
    assert _warnings(tokens, "sweeper") == []


# ── the liveness deadline ───────────────────────────────────────────────


def test_past_its_deadline_a_task_whose_wrapper_is_gone_is_aborted(make, tokens, sfn, clock):
    clock.now = NOW + timedelta(hours=74)          # started 05:00; 3 x 24 h + 1 h later
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    sfn.executions[_row(tokens, load)["wrapper_execution_arn"]] = "ABORTED"

    make().run()

    row = _row(tokens, load)
    assert row["status"] == "aborted" and "can no longer be woken" in row["error"]
    assert att.KEY not in row
    assert [w["kind"] for w in _warnings(tokens, "zombie")] == ["zombie"]


def test_past_its_deadline_a_task_whose_wrapper_runs_keeps_waiting(make, tokens, sfn, clock):
    clock.now = NOW + timedelta(hours=74)
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    sfn.executions[_row(tokens, load)["wrapper_execution_arn"]] = "RUNNING"

    make().run()

    row = _row(tokens, load)
    assert row["status"] == "waiting" and row["attempts"] == 1


def test_a_shorter_orchestration_timeout_brings_the_deadline_forward(make, tokens, sfn, clock):
    clock.now = NOW + timedelta(hours=5)            # 3 x 1 h + 1 h after 05:00 is 09:00
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5), orchestration_timeout=3600)

    make().run()                                     # no wrapper execution recorded: it is gone

    assert _row(tokens, load)["status"] == "aborted"


def test_before_its_deadline_the_wrapper_is_not_checked(make, tokens, sfn, clock):
    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    sfn.errors["describe_execution"] = AssertionError("the wrapper must not be checked yet")

    make().run()

    assert _row(tokens, load)["status"] == "waiting"


def test_a_row_without_started_at_is_never_past_its_deadline(make, tokens, clock):
    clock.now = NOW + timedelta(days=30)
    name = execution_name("load")
    row = task_row("load", wait_for=ASSET, attention=att.attention(att.WAITING, name),
                   attention_at=format_utc(clock.now - timedelta(minutes=5)))
    del row["started_at"]
    tokens.put_item(Item=row)

    make().run()

    assert _row(tokens, name)["status"] == "waiting"


# ── push retries ────────────────────────────────────────────────────────

PUSHED_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:pushed"
RUN_NAME = "pushed-2026-11-12-and-0a1b2c3d"


def _retry_row(tokens, clock, *, failed_hours_ago: float = 1, attempts: int = 0) -> str:
    from coordination_core.records import push_retry_item

    item = push_retry_item(run_name=RUN_NAME, consumer="pushed", sfn_arn=PUSHED_ARN,
                           run_input='{"triggered_by": "asset"}', error="ThrottlingException",
                           now=clock.now - timedelta(hours=failed_hours_ago))
    item[att.DUE] = format_utc(clock.now - timedelta(minutes=1))
    item["attempts"] = attempts
    tokens.put_item(Item=item)
    return item["execution_name"]


def test_a_due_push_retry_starts_the_run_and_drops_its_row(make, tokens, sfn, clock):
    row = _retry_row(tokens, clock)

    result = make().run()

    assert (result["push_retried"], result["push_started"]) == (1, 1)
    assert sfn.started == [{"arn": PUSHED_ARN, "name": RUN_NAME, "input": {"triggered_by": "asset"}}]
    assert "Item" not in tokens.get_item(Key={"execution_name": row})


def test_a_run_started_meanwhile_counts_as_started(make, tokens, sfn, clock):
    row = _retry_row(tokens, clock)
    sfn.started.append({"arn": PUSHED_ARN, "name": RUN_NAME, "input": {}})

    assert make().run()["push_started"] == 1
    assert "Item" not in tokens.get_item(Key={"execution_name": row})


def test_a_transient_start_failure_backs_off(make, tokens, sfn, clock):
    from coordination_testkit import client_error

    row = _retry_row(tokens, clock, attempts=2)
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")

    make().run()

    retry = _row(tokens, row)
    assert (retry["attempts"], retry[att.DUE]) == (3, format_utc(clock.now + timedelta(minutes=40)))
    assert "ThrottlingException" in retry["error"]


def test_a_permanent_start_failure_becomes_a_warning(make, tokens, sfn, clock):
    from coordination_testkit import client_error

    row = _retry_row(tokens, clock)
    sfn.errors["start_execution"] = client_error("StateMachineDoesNotExist", "StartExecution")

    make().run()

    assert "Item" not in tokens.get_item(Key={"execution_name": row})
    [warning] = _warnings(tokens, "push")
    assert warning["pipeline_name"] == "pushed" and "StateMachineDoesNotExist" in warning["error"]


def test_after_24_hours_the_sweeper_gives_up_with_a_warning(make, tokens, sfn, clock):
    row = _retry_row(tokens, clock, failed_hours_ago=24)

    assert make().run()["push_given_up"] == 1
    assert sfn.started == [] and "Item" not in tokens.get_item(Key={"execution_name": row})
    [warning] = _warnings(tokens, "push")
    assert "gave up" in warning["error"] and "ThrottlingException" in warning["error"]


def test_failed_retry_bookkeeping_leaves_the_row_for_the_next_run(make, tokens, sfn, clock, mocker):
    from coordination_testkit import client_error

    row = _retry_row(tokens, clock)
    run = make()
    real_update, real_delete = run.tasks.table.update_item, run.tasks.table.delete_item
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    mocker.patch.object(run.tasks.table, "update_item", side_effect=lambda **kw: (
        real_update(**kw) if kw["Key"]["execution_name"] == "_engine_health"
        else (_ for _ in ()).throw(client_error("InternalServerError", "UpdateItem"))))
    run.run()
    assert _row(tokens, row)["attempts"] == 0                     # unchanged, still due

    del sfn.errors["start_execution"]
    mocker.patch.object(run.tasks.table, "delete_item", side_effect=lambda **kw: (
        real_delete(**kw) if kw["Key"]["execution_name"] == sweep.LEASE_KEY
        else (_ for _ in ()).throw(client_error("InternalServerError", "DeleteItem"))))
    run.run()
    assert _row(tokens, row)["run_name"] == RUN_NAME               # started, but the row stays to be dropped later


def test_a_retry_row_dropped_meanwhile_is_skipped(make, tokens, sfn, clock, mocker):
    row = _retry_row(tokens, clock)
    run = make()
    mocker.patch.object(run.tasks, "get", return_value=None)

    assert run.push_retries(deadline=float("inf"))["push_retried"] == 0
    assert sfn.started == [] and _row(tokens, row)


def test_a_push_warning_that_cannot_be_written_still_drops_the_row(make, tokens, sfn, clock, mocker):
    from coordination_testkit import client_error

    row = _retry_row(tokens, clock, failed_hours_ago=24)
    run = make()
    mocker.patch.object(run.tasks, "put", side_effect=client_error("AccessDeniedException", "PutItem"))

    assert run.run()["push_given_up"] == 1
    assert "Item" not in tokens.get_item(Key={"execution_name": row})


def test_the_budget_stops_push_retries_too(make, tokens, sfn, clock):
    _retry_row(tokens, clock)
    ticks = iter([0.0, 10_000.0])

    result = make(monotonic=lambda: next(ticks, 10_000.0)).run()

    assert result["push_retried"] == 0 and sfn.started == []


# ── the catch-up ────────────────────────────────────────────────────────


@pytest.fixture
def pushed(consumers, registry):
    import json

    registry.put_item(Item={"pipeline_name": "pushed", "asset_schedule": json.dumps({"operator": "AND", "assets": ["ns/a"]})})
    consumers.put_item(Item={"asset_name": "ns/a", "pipeline_name": "pushed", "sfn_arn": PUSHED_ARN})


def test_the_catch_up_starts_a_run_whose_stream_record_was_lost(make, tokens, asset_events, pushed, sfn, clock):
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))

    result = make().run()

    assert (result["catch_up"], result["facts_replayed"]) == ("done", 1)
    [run] = sfn.started
    assert run["name"].startswith("pushed-2026-11-12-and-")
    assert _row(tokens, sweep.HEALTH_KEY)["catchup_at"] == format_utc(clock.now)


def test_the_catch_up_runs_hourly_and_never_starts_a_run_twice(make, tokens, asset_events, pushed, sfn, clock):
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    make().run()
    clock.now += timedelta(minutes=30)
    assert make().run()["catch_up"] == "not due"
    clock.now += timedelta(minutes=30)
    assert make().run()["catch_up"] == "done"
    assert len(sfn.started) == 1


def test_the_catch_up_replays_only_new_facts_of_the_last_48_hours(make, asset_events, pushed, sfn, clock):
    from coordination_testkit import legacy_fact

    asset_events.put_item(Item=legacy_fact("ns/a", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    asset_events.put_item(Item=fact("ns/a", "2026-11-09", event_time="2026-11-09T05:00:00.000Z"))

    assert make().run()["facts_replayed"] == 0
    assert sfn.started == []


def test_a_catch_up_cut_short_by_the_budget_goes_on_from_its_cursor(make, tokens, consumers, asset_events,
                                                                     pushed, sfn, clock):
    consumers.put_item(Item={"asset_name": "ns/b", "pipeline_name": "other", "sfn_arn": PUSHED_ARN})
    ticks = iter([0.0, 0.0, 10_000.0])          # the run's deadline, ns/a, then ns/b past it

    first = make(monotonic=lambda: next(ticks, 10_000.0)).run()

    assert first["catch_up"] == "partial"
    assert _row(tokens, sweep.HEALTH_KEY)["catchup_cursor"] == "ns/a"
    assert make().run()["catch_up"] == "done"                     # a cursor makes it due at once
    assert "catchup_cursor" not in _row(tokens, sweep.HEALTH_KEY)


def test_failed_catch_up_bookkeeping_only_means_starting_over(make, tokens, pushed, mocker):
    from coordination_testkit import client_error

    run = make()
    mocker.patch.object(run.tasks.table, "update_item", side_effect=client_error("InternalServerError", "UpdateItem"))

    assert run.run()["catch_up"] == "done"
    assert run.tasks.get(sweep.HEALTH_KEY) is None


def test_every_page_of_subscriptions_and_facts_is_read(make, consumers, asset_events, pushed, sfn, mocker):
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:30:00.000Z"))
    consumers.put_item(Item={"asset_name": "ns/z", "pipeline_name": "other", "sfn_arn": PUSHED_ARN})
    run = make()
    scan, query = run.consumers.scan, run.facts.query
    mocker.patch.object(run.consumers, "scan", side_effect=lambda **kw: scan(**kw, Limit=1))
    mocker.patch.object(run.facts, "query", side_effect=lambda **kw: query(**kw, Limit=1))

    assert run.run()["facts_replayed"] == 2


# ── the handler ─────────────────────────────────────────────────────────


def test_the_handler_runs_a_sweeper_with_the_request_as_owner(make, mocker):
    run = mocker.patch.object(sweeper.Sweeper, "run", return_value={"checked": 0})
    mocker.patch.object(sweeper, "_reconciler")

    class Context:
        aws_request_id = "request-1"

    assert sweeper.handler({}, Context()) == {"checked": 0}
    run.assert_called_once()
