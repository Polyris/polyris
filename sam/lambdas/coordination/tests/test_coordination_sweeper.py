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


# ── the handler ─────────────────────────────────────────────────────────


def test_the_handler_runs_a_sweeper_with_the_request_as_owner(make, mocker):
    run = mocker.patch.object(sweeper.Sweeper, "run", return_value={"checked": 0})
    mocker.patch.object(sweeper, "_reconciler")

    class Context:
        aws_request_id = "request-1"

    assert sweeper.handler({}, Context()) == {"checked": 0}
    run.assert_called_once()
