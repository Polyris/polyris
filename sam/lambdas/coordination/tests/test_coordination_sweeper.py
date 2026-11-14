"""The sweeper (ADR #130 §8): lease, due rows, backoff, lost-hint warnings and zombies.

DynamoDB is moto with the tables of sam/template.yaml; Step Functions is the
FakeSfn of the test kit. The reconciler runs for real, in-process.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest
from coordination_testkit import NOW, RUN, execution_name, fact, task_row

import sweeper
from polyris.coordination import attention as att
from polyris.coordination import sweep
from polyris.coordination.times import format_utc
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


class FakeLambda:
    """The Lambda call of the self-check: the asset-fanout function's event source mappings."""

    def __init__(self, state: str = "Enabled", result: str = "OK"):
        self.state, self.result = state, result

    def list_event_source_mappings(self, *, FunctionName: str) -> Dict[str, Any]:
        if self.state == "error":
            from coordination_testkit import client_error
            raise client_error("AccessDeniedException", "ListEventSourceMappings")
        mapping = {"State": self.state, "LastProcessingResult": self.result}
        return {"EventSourceMappings": [] if self.state == "missing" else [mapping]}


@pytest.fixture
def make(ddb, sfn, clock):
    def build(**kwargs: Any) -> sweeper.Sweeper:
        kwargs.setdefault("lambda_client", FakeLambda())
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


def test_the_lease_holder_takes_its_lease_again(make, clock):
    """botocore resends a put whose answer was lost; the resend must not fail its own condition."""
    run = make()
    assert run._take_lease(clock.now) and run._take_lease(clock.now)


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


def test_a_row_whose_check_changed_nothing_backs_off_too(make, tokens, clock):
    """A warned row is re-checked with the same backoff, not every run, and does not hold the "behind" banner."""
    name = _waiting(tokens, "load", due=clock.now - timedelta(minutes=50))
    tokens.update_item(Key={"execution_name": name}, UpdateExpression="REMOVE wait_token")   # can never be signalled

    assert make().run()["healthy"] is True

    row = _row(tokens, name)
    assert (row[att.DUE], row["attempts"]) == (format_utc(clock.now + timedelta(minutes=10)), 1)
    assert _row(tokens, sweep.HEALTH_KEY)["oldest_overdue"] == ""


def test_every_page_of_due_rows_is_read(make, tokens, clock, mocker):
    # One shard, one row per page.
    names = [_waiting(tokens, f"t{i}", due=clock.now - timedelta(minutes=i + 1)) for i in range(3)]
    for name in names:
        tokens.update_item(Key={"execution_name": name}, UpdateExpression="SET #a = :p",
                           ExpressionAttributeNames={"#a": att.KEY}, ExpressionAttributeValues={":p": "waiting#0"})
    run = make()
    real = run.tasks.table.query
    mocker.patch.object(run.tasks.table, "query", side_effect=lambda **kw: real(**{"Limit": 1, **kw}))

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


# ── tasks held in their pause wait ──────────────────────────────────────


def _held(tokens, task: str, *, due: datetime, attempts: int = 0, **extra: Any) -> str:
    """A row as pause_waiter leaves it: waiting_paused, its pause token, a paused# entry."""
    name = execution_name(task)
    tokens.put_item(Item=task_row(task, status="waiting_paused", pause_token=f"pause-{task}",
                                  attention=att.attention(att.PAUSED, name), attention_at=format_utc(due),
                                  attempts=attempts, **extra))
    return name


def _pause_flag(tokens, paused: bool) -> None:
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": paused})


def _adoption_done(tokens, clock) -> None:
    """The engine row of a stack whose adoption finished long ago."""
    tokens.put_item(Item={"execution_name": sweep.HEALTH_KEY, "adoption_generation": sweep.ADOPTION_GENERATION,
                          "adopt_tasks_cursor": "", "adopt_tasks_until": format_utc(clock.now - timedelta(days=1)),
                          "adopt_facts_cursor": "", "adopt_facts_until": format_utc(clock.now - timedelta(days=1))})


@pytest.mark.parametrize("flag", [False, None])          # resumed, or never paused at all
def test_a_held_task_of_a_run_no_longer_paused_is_released_with_a_warning(make, tokens, sfn, clock, flag):
    if flag is not None:
        _pause_flag(tokens, flag)
    load = _held(tokens, "load", due=clock.now - timedelta(minutes=1))

    result = make().run()

    assert (result["pause_checked"], result["pause_released"]) == (1, 1)
    assert sfn.succeeded == [{"token": "pause-load", "output": {"resumed": True}}]
    [warning] = _warnings(tokens, "sweeper")
    assert (warning["task_name"], warning["task_date"]) == ("load", "2026-11-12")
    assert "the resume did not reach it" in warning["error"]
    assert _row(tokens, load)[att.KEY] == att.attention(att.PAUSED, load)    # run_task drops it when it starts


def test_a_held_task_of_a_paused_run_stays_held_and_is_due_again(make, tokens, sfn, clock):
    _pause_flag(tokens, True)
    load = _held(tokens, "load", due=clock.now - timedelta(minutes=1), attempts=1)

    assert make().run()["pause_released"] == 0

    row = _row(tokens, load)
    assert (row[att.DUE], row["attempts"]) == (format_utc(clock.now + timedelta(minutes=20)), 2)
    assert sfn.succeeded == [] and _warnings(tokens, "sweeper") == []


def test_a_dead_pause_token_takes_the_row_out_of_the_paused_entries(make, tokens, sfn, clock):
    _adoption_done(tokens, clock)
    load = _held(tokens, "load", due=clock.now - timedelta(minutes=1))
    sfn.dead_tokens.add("pause-load")                   # released meanwhile, or its heartbeat ran out

    assert make().run()["pause_released"] == 0

    row = _row(tokens, load)
    assert att.KEY not in row and att.DUE not in row
    assert row["pause_token"] == "pause-load"           # run_task owns it
    assert _warnings(tokens, "sweeper") == []


def test_a_row_that_holds_no_pause_token_any_more_leaves_the_paused_entries(make, tokens, clock):
    _adoption_done(tokens, clock)
    load = _held(tokens, "load", due=clock.now - timedelta(minutes=1))
    tokens.update_item(Key={"execution_name": load}, UpdateExpression="SET #s = :r REMOVE pause_token",
                       ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":r": "running"})

    make().run()

    assert att.KEY not in _row(tokens, load)


def test_only_the_paused_entry_itself_is_ever_dropped(make, tokens, clock):
    load = _waiting(tokens, "load", due=clock.now + timedelta(minutes=5))      # registered again meanwhile

    assert make().tasks.drop_pause_check(load, att.attention(att.PAUSED, load)) is False
    assert make().tasks.drop_pause_check(load, att.attention(att.PAUSED, load), "pause-load") is False
    assert _row(tokens, load)[att.KEY] == att.attention(att.WAITING, load)


def test_a_failed_release_leaves_the_row_due(make, tokens, sfn, clock):
    from coordination_testkit import client_error

    load = _held(tokens, "load", due=clock.now - timedelta(minutes=1))
    sfn.errors["send_task_success"] = client_error("ThrottlingException", "SendTaskSuccess")

    assert make().run()["pause_released"] == 0
    assert _row(tokens, load)[att.DUE] == format_utc(clock.now - timedelta(minutes=1))
    assert _warnings(tokens, "sweeper") == []


def test_the_budget_stops_the_pause_checks_too(make, tokens, sfn, clock):
    _held(tokens, "load", due=clock.now - timedelta(minutes=1))
    ticks = iter([0.0, 10_000.0])                       # due_rows finds nothing, then the budget is gone

    assert make(monotonic=lambda: next(ticks, 10_000.0)).run()["pause_checked"] == 0
    assert sfn.succeeded == []


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


def test_a_woken_row_that_cannot_be_read_again_still_gets_its_warning(make, tokens, asset_events, sfn, clock, mocker):
    """The warning goes without the task's date; the due rows after it are still checked."""
    from coordination_testkit import client_error

    load = _waiting(tokens, "load", due=clock.now - timedelta(minutes=5))
    _waiting(tokens, "report", due=clock.now - timedelta(minutes=4))
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:10:00.000Z"))
    run = make()
    real = run.tasks.get

    def get(name: str) -> Any:            # fails only the re-read after the wake-up, not the reconciler's reads
        if any(s["token"] == _row(tokens, name).get("wait_token") for s in sfn.succeeded):
            raise client_error("ProvisionedThroughputExceededException", "GetItem")
        return real(name)

    mocker.patch.object(run.tasks, "get", side_effect=get)

    result = run.run()

    assert (result["checked"], result["missed"]) == (2, 2)
    assert {w["execution_name"] for w in _warnings(tokens, "sweeper")} >= {f"_notify_warn_sweeper_{load}"}


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
    from polyris.coordination.records import push_retry_item

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


def test_the_catch_up_skips_an_unreadable_fact_without_a_warning_per_consumer(make, tokens, asset_events, pushed,
                                                                             sfn):
    """The stream reported it once when it was recorded; the hourly replay must not repeat that per consumer."""
    asset_events.put_item(Item={**fact("ns/a", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"),
                                "partition_date": "20261112"})
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:10:00.000Z"))

    result = make().run()

    assert result["catch_up"] == "done"
    assert len(sfn.started) == 1                    # the readable fact's run; the other starts nothing
    assert not [i for i in tokens.scan()["Items"] if i["execution_name"].startswith("_notify_warn_")]


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


def test_a_catch_up_cut_short_inside_one_asset_goes_on_after_its_last_replayed_fact(
        make, tokens, asset_events, pushed, sfn, clock, registry):
    """An asset with more facts than one run can replay still finishes, over several runs."""
    import json

    registry.put_item(Item={"pipeline_name": "pushed", "asset_schedule": json.dumps({"operator": "OR", "assets": ["ns/a"]})})
    times = [f"2026-11-12T0{h}:00:00.000Z" for h in (3, 4, 5)]
    for event_time in times:
        asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time=event_time))
    ticks = iter([0.0, 0.0, 0.0, 0.0, 10_000.0])     # the run's deadline, the asset, two facts, the third past it

    first = make(monotonic=lambda: next(ticks, 10_000.0)).run()

    assert (first["catch_up"], first["facts_replayed"]) == ("partial", 2)
    assert _row(tokens, sweep.HEALTH_KEY)["catchup_within"] == {"asset": "ns/a", "after": times[1]}
    second = make().run()
    assert (second["catch_up"], second["facts_replayed"]) == ("done", 1)
    assert "catchup_within" not in _row(tokens, sweep.HEALTH_KEY)
    assert len(sfn.started) == 3                    # one OR run per fact, none twice


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


# ── adoption ────────────────────────────────────────────────────────────


def test_a_waiting_row_from_an_older_version_is_adopted_due_now(make, tokens, clock):
    old = execution_name("load")
    tokens.put_item(Item=task_row("load", wait_for=ASSET))            # no attention entry
    tokens.put_item(Item=task_row("done", status="success"))

    assert make().run()["tasks_adopted"] == 1

    row = _row(tokens, old)
    assert (row[att.KEY], row[att.DUE]) == (att.attention(att.WAITING, old), format_utc(clock.now))
    assert att.KEY not in _row(tokens, execution_name("done"))
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health["adoption_generation"] == sweep.ADOPTION_GENERATION
    assert health["adopt_tasks_until"] == format_utc(clock.now + sweep.ADOPTION_REPEAT)


def test_a_held_row_from_an_older_version_is_adopted_into_the_paused_entries(make, tokens, clock):
    held = execution_name("load")
    tokens.put_item(Item=task_row("load", status="waiting_paused", pause_token="pause-load"))   # no attention entry

    assert make().run()["tasks_adopted"] == 1

    row = _row(tokens, held)
    assert (row[att.KEY], row[att.DUE]) == (att.attention(att.PAUSED, held), format_utc(clock.now))


def test_the_task_scan_repeats_for_an_hour_then_stops(make, tokens, clock):
    make().run()
    clock.now += timedelta(minutes=30)
    tokens.put_item(Item=task_row("late", wait_for=ASSET))           # written during the stack update
    assert make().run()["tasks_adopted"] == 1
    clock.now += timedelta(minutes=31)
    tokens.put_item(Item=task_row("later", wait_for=ASSET))
    assert make().run()["tasks_adopted"] == 0


def test_the_fact_scan_repeats_for_an_hour_then_stops(make, asset_events, clock):
    """A fact an old run_task definition writes during the stack update still gets its partition fields."""
    from coordination_testkit import legacy_fact

    make().run()
    clock.now += timedelta(minutes=30)
    asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    assert make().run()["facts_adopted"] == 1
    clock.now += timedelta(minutes=31)
    asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time="2026-11-12T06:00:00.000Z"))
    assert make().run()["facts_adopted"] == 0


def test_a_new_adoption_generation_scans_again(make, tokens, clock, monkeypatch):
    make().run()
    clock.now += timedelta(hours=2)
    tokens.put_item(Item=task_row("load", wait_for=ASSET))
    assert make().run()["tasks_adopted"] == 0
    monkeypatch.setattr(sweep, "ADOPTION_GENERATION", sweep.ADOPTION_GENERATION + 1)
    assert make().run()["tasks_adopted"] == 1


def test_an_old_fact_gets_its_partition_fields(make, asset_events, registry, clock):
    import json

    from coordination_testkit import legacy_fact

    registry.put_item(Item={"pipeline_name": "producer", "dag": json.dumps(
        {"nodes": [{"outlets": [{"name": "ns/orders", "granularity": "weekly"}]}]})})
    asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))

    assert make().run()["facts_adopted"] == 1

    item = asset_events.get_item(Key={"asset_name": "ns/orders", "event_time": "2026-11-12T05:00:00.000Z"})["Item"]
    assert (item["partition_key"], item["partition_date"], item["granularity"]) == ("2026-11-12", "2026-11-12", "weekly")
    assert "fact_version" not in item                    # a MODIFY: the stream mapping ignores it
    assert make().run()["facts_adopted"] == 0             # the fact scan runs once per generation


def test_an_old_fact_nobody_can_read_is_left_alone_with_a_warning(make, tokens, asset_events):
    """Principle #51: skipped, and said so in the bell."""
    from coordination_testkit import legacy_fact

    asset_events.put_item(Item=legacy_fact("ns/orders", "yesterday", event_time="2026-11-12T05:00:00.000Z"))

    assert make().run()["facts_adopted"] == 0
    [warning] = _warnings(tokens, "coordination")
    assert warning["execution_name"] == "_notify_warn_coordination_adopt:ns/orders:2026-11-12T05:00:00.000Z"
    assert "yesterday" in warning["error"] and warning["pipeline_name"] == "producer"


def test_adoption_checks_its_budget_per_row_and_resumes_after_the_last_one(make, tokens, asset_events, clock):
    """Principle #52: one page of old rows must not run the sweeper past its timeout."""
    from coordination_testkit import legacy_fact

    for i in range(3):
        tokens.put_item(Item=task_row(f"t{i}", wait_for=ASSET))
        asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time=f"2026-11-12T05:0{i}:00.000Z"))
    run = make()
    calls = iter(range(10 ** 6))
    run.monotonic = lambda: 0.0 if next(calls) < 2 else 10.0        # the budget lasts two rows

    assert run.adopt(deadline=1.0) == {"tasks_adopted": 2, "facts_adopted": 0}
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health["adopt_tasks_cursor"] and not health["adopt_tasks_until"]

    run.monotonic = lambda: 0.0
    assert run.adopt(deadline=1.0) == {"tasks_adopted": 1, "facts_adopted": 3}      # each row once
    assert len([i for i in tokens.scan()["Items"] if i["execution_name"].startswith("t") and att.KEY in i]) == 3
    assert all("partition_date" in i for i in asset_events.scan()["Items"])
    assert _row(tokens, sweep.HEALTH_KEY)["adopt_facts_until"] == format_utc(clock.now + sweep.ADOPTION_REPEAT)


def test_an_adoption_that_lost_a_race_counts_nothing(make, tokens, asset_events, mocker):
    from coordination_testkit import client_error, legacy_fact

    tokens.put_item(Item=task_row("load", wait_for=ASSET))
    asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    run = make()
    race = client_error("ConditionalCheckFailedException", "UpdateItem")
    mocker.patch.object(run.tasks.table, "update_item", side_effect=race)
    mocker.patch.object(run.facts, "update_item", side_effect=race)

    assert run.adopt(deadline=float("inf")) == {"tasks_adopted": 0, "facts_adopted": 0}


def test_other_adoption_write_errors_fail_the_run(make, tokens, asset_events, mocker):
    from coordination_testkit import client_error, legacy_fact

    asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    run = make()
    mocker.patch.object(run.facts, "update_item", side_effect=client_error("AccessDeniedException", "UpdateItem"))
    with pytest.raises(Exception, match="AccessDeniedException"):
        run.adopt(deadline=float("inf"))
    tokens.put_item(Item=task_row("load", wait_for=ASSET))
    mocker.patch.object(run.tasks.table, "update_item", side_effect=client_error("AccessDeniedException", "UpdateItem"))
    with pytest.raises(Exception, match="AccessDeniedException"):
        run.adopt(deadline=float("inf"))


# ── the self-check ──────────────────────────────────────────────────────


def test_the_self_check_reports_what_the_console_reads(make, tokens, clock):
    from polyris.coordination.records import push_retry_item

    _waiting(tokens, "load", due=clock.now + timedelta(minutes=5))
    tokens.put_item(Item=push_retry_item(run_name="r", consumer="c", sfn_arn="arn", run_input="{}",
                                         error="x", now=clock.now))

    assert make().run()["healthy"] is True

    health = _row(tokens, sweep.HEALTH_KEY)
    assert (health["checked_at"], health["fanout_mapping"], health["stream"]) == (format_utc(clock.now), "Enabled", "ENABLED")
    assert (health["fanout_result"], health["failed_steps"]) == ("OK", [])
    assert (health["oldest_overdue"], health["push_retry_backlog"]) == ("", 1)
    assert sweep.engine_problems(health, clock.now) == []


def test_a_mapping_whose_last_call_failed_is_reported(make, tokens, clock):
    assert make(lambda_client=FakeLambda(result="PROBLEM: Function call failed")).run()["healthy"] is False
    health = _row(tokens, sweep.HEALTH_KEY)
    assert sweep.engine_problems(health, clock.now) == [
        "Asset events are not delivered: the stream mapping's last call failed (PROBLEM: Function call failed)."]


def test_every_step_of_a_run_is_a_method():
    assert all(callable(getattr(sweeper.Sweeper, step, None)) for step in sweep.STEPS)


def test_a_failing_step_skips_neither_the_others_nor_the_self_check(make, tokens, clock, mocker):
    """Principle #38: the failure reaches the banner, and the next run tries the step again."""
    from coordination_testkit import client_error

    run = make()
    mocker.patch.object(run, "catch_up", side_effect=client_error("ThrottlingException", "Query"))
    adopt = mocker.spy(run, "adopt")

    result = run.run()

    assert adopt.call_count == 1 and result["healthy"] is False
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health["checked_at"] == format_utc(clock.now)
    assert [s.split(": ")[0] for s in health["failed_steps"]] == ["catch_up"]
    [problem] = sweep.engine_problems(health, clock.now)
    assert problem.startswith("The sweeper could not finish the hourly push catch-up on its last run: ClientError:")
    assert "ThrottlingException" in problem
    assert "Item" not in tokens.get_item(Key={"execution_name": sweep.LEASE_KEY})     # released


@pytest.mark.parametrize("kind", [att.WAITING, att.PAUSED])
def test_an_overdue_row_is_reported(make, tokens, clock, mocker, kind):
    run = make()
    mocker.patch.object(run, "due_rows", return_value={})            # a run that never got to them
    mocker.patch.object(run, "paused_rows", return_value={})
    (_waiting if kind == att.WAITING else _held)(tokens, "load", due=clock.now - timedelta(minutes=50))

    assert run.run()["healthy"] is False
    assert _row(tokens, sweep.HEALTH_KEY)["oldest_overdue"] == format_utc(clock.now - timedelta(minutes=50))


@pytest.mark.parametrize("state,shown", [("Disabled", "Disabled"), ("missing", "missing"),
                                         ("error", "unknown (AccessDeniedException)")])
def test_a_mapping_that_does_not_deliver_is_reported(make, tokens, state, shown):
    assert make(lambda_client=FakeLambda(state)).run()["healthy"] is False
    assert _row(tokens, sweep.HEALTH_KEY)["fanout_mapping"] == shown


def test_a_stream_that_cannot_be_read_is_reported(make, tokens, mocker):
    from coordination_testkit import client_error

    run = make()
    mocker.patch.object(run.reconciler.resource.meta.client, "describe_table",
                        side_effect=client_error("AccessDeniedException", "DescribeTable"))
    assert run.run()["healthy"] is False
    assert _row(tokens, sweep.HEALTH_KEY)["stream"] == "unknown (AccessDeniedException)"


def test_a_disabled_stream_is_reported(make, tokens, mocker):
    run = make()
    mocker.patch.object(run.reconciler.resource.meta.client, "describe_table",
                        return_value={"Table": {"StreamSpecification": {"StreamEnabled": False}}})
    run.run()
    assert _row(tokens, sweep.HEALTH_KEY)["stream"] == "DISABLED"


def test_a_health_row_that_cannot_be_written_does_not_fail_the_run(make, tokens, mocker):
    from coordination_testkit import client_error

    run = make()
    real = run.tasks.table.update_item
    mocker.patch.object(run.tasks.table, "update_item", side_effect=lambda **kw: (
        (_ for _ in ()).throw(client_error("InternalServerError", "UpdateItem"))
        if kw["Key"]["execution_name"] == sweep.HEALTH_KEY else real(**kw)))

    assert "healthy" in run.run()


def test_a_self_check_reading_that_fails_is_reported_and_the_row_still_written(make, tokens, mocker):
    from coordination_testkit import client_error

    run = make()
    real = run.tasks.table.query

    def query(**kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("Limit") == 1:                          # the oldest overdue row
            raise client_error("ProvisionedThroughputExceededException", "Query")
        return real(**kwargs)

    mocker.patch.object(run.tasks.table, "query", side_effect=query)

    assert run.run()["healthy"] is False
    [failure] = _row(tokens, sweep.HEALTH_KEY)["failed_steps"]
    assert failure.startswith("self_check: the oldest overdue task could not be read: ClientError")
    [problem] = sweep.engine_problems(_row(tokens, sweep.HEALTH_KEY), run.reconciler.clock())
    assert problem.startswith("The sweeper could not finish its self-check on its last run: the oldest overdue task")


def test_every_page_of_the_retry_backlog_is_counted(make, tokens, clock, mocker):
    from polyris.coordination.records import push_retry_item

    for i in range(3):
        item = push_retry_item(run_name=f"r{i}", consumer="c", sfn_arn="arn", run_input="{}", error="x", now=clock.now)
        item[att.KEY] = "retry#0"
        tokens.put_item(Item=item)
    run = make()
    real = run.tasks.table.query
    mocker.patch.object(run.tasks.table, "query", side_effect=lambda **kw: real(**kw, Limit=1)
                        if kw.get("Select") == "COUNT" else real(**kw))
    assert run._backlog() == 3


# ── the handler ─────────────────────────────────────────────────────────


def test_the_lambda_client_is_created_once(monkeypatch):
    import env

    monkeypatch.setattr(env, "_lambda", None)
    assert env.lambda_() is env.lambda_()


def test_the_handler_runs_a_sweeper_with_the_request_as_owner(make, mocker):
    run = mocker.patch.object(sweeper.Sweeper, "run", return_value={"checked": 0})
    mocker.patch.object(sweeper, "_reconciler")

    class Context:
        aws_request_id = "request-1"

    assert sweeper.handler({}, Context()) == {"checked": 0}
    run.assert_called_once()
