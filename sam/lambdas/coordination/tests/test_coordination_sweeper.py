"""The sweeper (ADR #130 §8, ADR #137 §6, §7): lease, one full pass per due run, the move of the run row's due time,
lost-hint warnings, rows whose run row is missing, push retries, the catch-up and the self-check.

DynamoDB is moto with the tables of sam/template.yaml; Step Functions is the
FakeSfn of the test kit. The dispatcher runs for real, in-process, over runs
that Start_Run wrote.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from coordination_testkit import DATE, NOW, RUN, TABLES, client_error, fact

import sweeper
from dispatcher import Dispatcher
from polyris.coordination import attention as att
from polyris.coordination import run_row, start_run, sweep, task_row
from polyris.coordination.times import format_utc
from failures import CoordinationTransient

RUN_KEY = run_row.key(RUN)
OTHER = "orders-run-2026-11-12-bbbb0002"
MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris-orders"
CHAIN = [{"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"},
         {"task_id": "load", "task_type": "sfn", "dependencies": ["extract"], "trigger_rule": "all_success"}]


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW + timedelta(minutes=30))


MAPPING_UUID = "48529b7b-abda-4306-8550-8d3bbc1391c3"
DISPATCH_UUID = "7d0c1f52-2e5a-4bb1-9d5e-0b9f0e3c6a10"
MAPPING_CHANGED = datetime(2026, 11, 12, 5, 40, tzinfo=timezone.utc)


class FakeLambda:
    """The Lambda call of the self-check: each function's event source mappings, by name."""

    def __init__(self, state: Any = "Enabled", result: str = "OK", *, tasks: Any = "Enabled", tasks_result: str = "OK"):
        self.by_function = {"asset-fanout": (state, result, MAPPING_UUID), "reconcile": (tasks, tasks_result, DISPATCH_UUID)}

    def list_event_source_mappings(self, *, FunctionName: str) -> Dict[str, Any]:
        state, result, uuid = self.by_function[FunctionName]
        if state == "error":
            raise client_error("AccessDeniedException", "ListEventSourceMappings")
        mapping = {"State": state, "LastProcessingResult": result, "UUID": uuid, "LastModified": MAPPING_CHANGED}
        return {"EventSourceMappings": [] if state == "missing" else [mapping]}


@pytest.fixture
def make(ddb, sfn, clock, rows):
    dispatcher = Dispatcher(rows.client, sfn, resource=ddb, tokens_table=TABLES["TOKENS_TABLE"][1],
                            subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1], specs_table=TABLES["SPECS_TABLE"][1],
                            asset_events_table=TABLES["ASSET_EVENTS_TABLE"][1],
                            pipelines_table=TABLES["PIPELINES_TABLE"][1], run_task_arn="arn:run-task",
                            clock=clock, sleep=lambda _: None)

    def build(**kwargs: Any) -> sweeper.Sweeper:
        kwargs.setdefault("lambda_client", FakeLambda())
        kwargs.setdefault("fanout_function", "asset-fanout")
        kwargs.setdefault("dispatcher_function", "reconcile")
        return sweeper.Sweeper(dispatcher, owner="me", **kwargs)
    return build


@pytest.fixture
def run(rows, registry, sfn):
    """A 1.3 run of extract → load whose execution runs, as Start_Run wrote it: its run row due at once. With
    ``passed``, the pass its activation woke has run, and every run_task it started runs."""
    def start(execution: str = RUN, *, passed: bool = True) -> str:
        rows.start(CHAIN, execution=execution)
        registry.put_item(Item={"pipeline_name": "orders", "sfn_arn": MACHINE})
        sfn.executions[MACHINE.replace(":stateMachine:", ":execution:") + f":{execution}"] = "RUNNING"
        if passed:
            rows.dispatcher.full_pass(run_row.key(execution), source="stream", remaining_ms=lambda: 60_000)
            for started in sfn.started:
                sfn.executions[f"arn:run-task:{started['name']}"] = "RUNNING"
        return run_row.key(execution)
    return start


def _row(tokens, name: str) -> Dict[str, Any]:
    return tokens.get_item(Key={"execution_name": name}, ConsistentRead=True).get("Item")


def _key(task: str, execution: str = RUN) -> str:
    return task_row.key(task, DATE, execution)


def _set(tokens, name: str, **fields: Any) -> None:
    tokens.update_item(Key={"execution_name": name},
                       UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
                       ExpressionAttributeNames={f"#f{i}": k for i, k in enumerate(fields)},
                       ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())})


def _warnings(tokens, kind: str) -> List[Dict[str, Any]]:
    return [i for i in tokens.scan()["Items"] if i["execution_name"].startswith(f"_notify_warn_{kind}_")]


# ── the lease ───────────────────────────────────────────────────────────


def test_a_run_without_the_lease_does_nothing(make, run, tokens, clock):
    tokens.put_item(Item={"execution_name": sweep.LEASE_KEY, "owner": "other",
                          "expires_at": format_utc(clock.now + timedelta(minutes=2))})
    run()

    assert make().run() == {"skipped": "lease held"}
    assert _row(tokens, RUN_KEY)[att.DUE] == format_utc(NOW)


def test_an_expired_lease_is_taken_and_released_after_the_run(make, tokens, clock):
    tokens.put_item(Item={"execution_name": sweep.LEASE_KEY, "owner": "crashed",
                          "expires_at": format_utc(clock.now - timedelta(seconds=1))})

    result = make().run()

    assert result["runs_checked"] == 0
    assert "Item" not in tokens.get_item(Key={"execution_name": sweep.LEASE_KEY})


def test_a_lease_taken_over_meanwhile_is_left_to_its_new_owner(make, tokens, clock, mocker):
    sweeper_run = make()
    real = sweeper_run.due_runs

    def steal(**kwargs: Any) -> Dict[str, Any]:
        tokens.put_item(Item={"execution_name": sweep.LEASE_KEY, "owner": "next", "expires_at": "x"})
        return real(**kwargs)

    mocker.patch.object(sweeper_run, "due_runs", side_effect=steal)
    sweeper_run.run()
    assert _row(tokens, sweep.LEASE_KEY)["owner"] == "next"


def test_the_lease_holder_takes_its_lease_again(make, clock):
    """botocore resends a put whose answer was lost; the resend must not fail its own condition."""
    sweeper_run = make()
    assert sweeper_run._take_lease(clock.now) and sweeper_run._take_lease(clock.now)


def test_a_lease_that_cannot_be_written_fails_the_run(make, mocker):
    sweeper_run = make()
    mocker.patch.object(sweeper_run.tasks, "put_item", side_effect=client_error("AccessDeniedException", "PutItem"))
    with pytest.raises(Exception, match="AccessDeniedException"):
        sweeper_run.run()          # a failed scheduled run is visible in its logs and the health check


# ── the runs that are due (ADR #137 §7) ─────────────────────────────────


def test_a_due_run_gets_one_full_pass_and_its_due_time_moves_by_the_backoff(make, run, tokens, sfn, clock):
    run()

    result = make().run()

    assert (result["runs_checked"], result["passes_failed"], result["lost_hint_runs"]) == (1, 0, 0)
    assert [s["input"]["task_name"] for s in sfn.started] == ["extract"]
    checked = _row(tokens, RUN_KEY)
    assert (checked[att.DUE], checked[run_row.CHECKS]) == (format_utc(clock.now + timedelta(minutes=10)), 1)
    clock.now += timedelta(minutes=10)
    make().run()
    assert _row(tokens, RUN_KEY)[att.DUE] == format_utc(clock.now + timedelta(minutes=20))   # the backoff goes on


def test_the_entries_of_one_run_make_one_pass(make, run, tokens, clock, sfn):
    """The run row and a task row of its run fall into one check (§6): extract's liveness check is due too."""
    run()
    assert make().run()["runs_checked"] == 1
    assert _row(tokens, _key("extract"))["checks"] == 1


def test_the_check_clears_left_at_and_finds_no_lost_hint_under_it(make, run, tokens, sfn, clock):
    run()
    make().run()
    _set(tokens, _key("extract"), status="success", finished_at=format_utc(NOW))
    _set(tokens, RUN_KEY, left_at=format_utc(NOW), **{att.DUE: format_utc(clock.now)})

    result = make().run()

    assert result["lost_hint_runs"] == 0 and _warnings(tokens, "sweeper") == []
    assert _row(tokens, _key("load"))["status"] == "deps_ready"
    assert run_row.LEFT_AT not in _row(tokens, RUN_KEY)


def test_a_rows_wait_ended_on_a_lost_hint_writes_one_warning_for_its_run(make, run, tokens, clock, lambda_env):
    run()
    make().run()
    _set(tokens, _key("extract"), status="success", finished_at=format_utc(clock.now - timedelta(minutes=20)))
    _set(tokens, RUN_KEY, **{att.DUE: format_utc(clock.now)})

    result = make().run()

    assert result["lost_hint_runs"] == 1
    [warning] = _warnings(tokens, "sweeper")
    assert warning["pipeline_execution"] == RUN and warning["pipeline_name"] == "orders"
    assert warning["error"].startswith("The periodic check found 1 task of this run that could have been decided "
                                       "earlier: load.")
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health["lost_hint_runs"] == 1
    assert [f["title"] for f in sweep.engine_findings(health, clock.now)] == ["Runs missed their wake-ups"]


def test_a_finished_run_is_closed_and_the_move_does_not_make_it_due_again(make, run, tokens, sfn, clock):
    run()
    make().run()
    for task in ("extract", "load"):
        _set(tokens, _key(task), status="success", finished_at=format_utc(NOW))
    _set(tokens, RUN_KEY, **{att.DUE: format_utc(clock.now)})

    make().run()

    assert sfn.succeeded and att.DUE not in _row(tokens, RUN_KEY)


def test_a_move_after_a_write_that_raised_run_version_is_refused_and_the_run_stays_due(make, run, tokens, sfn, clock):
    """A pending request keeps the run row due after the answer; the answer's mark raised run_version (§3)."""
    run()
    make().run()
    for task in ("extract", "load"):
        _set(tokens, _key(task), status="success", finished_at=format_utc(NOW))
    due = format_utc(clock.now)
    _set(tokens, RUN_KEY, requests={run_row.RENEW_TTL: {"by": "ops", "at": due}}, **{att.DUE: due})

    make().run()

    assert sfn.succeeded and _row(tokens, RUN_KEY)[att.DUE] == due


def test_a_failed_pass_moves_its_run_on_and_its_warning_replaces_itself(make, run, tokens, specs, clock):
    run()
    specs.delete_item(Key={"pk": _row(tokens, RUN_KEY)[run_row.SPEC_ID], "sk": "extract"})   # deleted by hand

    result = make().run()

    assert (result["runs_checked"], result["passes_failed"]) == (1, 1)
    assert _row(tokens, RUN_KEY)[att.DUE] == format_utc(clock.now + timedelta(minutes=10))
    clock.now += timedelta(minutes=10)
    make().run()
    [warning] = _warnings(tokens, "coordination")
    assert warning["execution_name"].endswith(f"{RUN_KEY}-pass") and "pass over the run failed" in warning["error"]
    assert _row(tokens, sweep.HEALTH_KEY)["failed_steps"] == []          # one broken run alone is not the engine


def test_a_sweep_whose_passes_mostly_failed_reports_a_failed_step(make, run, tokens, specs, clock, lambda_env):
    for execution in (RUN, OTHER):
        run(execution)
    for item in specs.scan()["Items"]:
        if item["sk"] == "extract":
            specs.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})

    assert make().run()["healthy"] is False

    [failure] = _row(tokens, sweep.HEALTH_KEY)["failed_steps"]
    assert failure.startswith("due_runs: 2 of 2 passes over runs failed; the first: ")
    [finding] = sweep.engine_findings(_row(tokens, sweep.HEALTH_KEY), clock.now)
    assert finding["title"] == "The periodic re-check could not finish checking the runs that are due"


def test_a_pass_that_failed_transiently_counts_and_its_run_still_moves(make, run, tokens, clock, mocker):
    run()
    sweeper_run = make()
    mocker.patch.object(sweeper_run.dispatcher, "full_pass", side_effect=CoordinationTransient("throttled"))

    result = sweeper_run.run()

    assert result["passes_failed"] == 1 and _warnings(tokens, "coordination") == []
    assert _row(tokens, RUN_KEY)[att.DUE] == format_utc(clock.now + timedelta(minutes=10))


def test_a_run_row_that_cannot_be_read_counts_as_a_failed_pass(make, run, tokens, mocker):
    run()
    sweeper_run = make()
    mocker.patch.object(sweeper_run.tasks, "get_item", side_effect=client_error("InternalServerError", "GetItem"))
    assert sweeper_run.due_runs(deadline=1e9)["passes_failed"] == 1


def test_the_engine_stops_a_run_whose_execution_ended_before_its_answer(make, run, sfn, tokens):
    run()
    sfn.executions[MACHINE.replace(":stateMachine:", ":execution:") + f":{RUN}"] = "ABORTED"

    make().run()

    stopped = _row(tokens, RUN_KEY)
    assert stopped["stop"]["by"] == "the engine" and "ended ABORTED" in stopped["stop"]["reason"]
    assert {_row(tokens, _key(t))["status"] for t in ("extract", "load")} == {"stopped"}
    assert sfn.failed and att.DUE not in stopped


def test_the_engine_stops_a_run_still_creating_after_start_runs_retry_window(make, run, sfn, tokens, clock):
    run(passed=False)
    _set(tokens, RUN_KEY, status="creating")
    clock.now = NOW + start_run.retry_window()             # Start_Run wrote the run row at NOW

    make().run()

    assert _row(tokens, RUN_KEY)["stop"]["reason"].startswith("Start_Run did not make the run active")
    assert sfn.started == [] and sfn.failed


def test_rows_of_a_run_whose_run_row_is_missing_are_stopped_with_the_cause(make, tokens, clock):
    """1.2 rows left in flight: no run row, no readiness fields; each due one is stopped (invariant 11)."""
    for task in ("extract", "load"):
        name = f"{task}-{DATE}{RUN[-20:]}"
        tokens.put_item(Item={"execution_name": name, "task_name": task, "status": "waiting", "generation": 1,
                              "pipeline_execution": RUN, att.KEY: att.attention(att.WAITING, name),
                              att.DUE: format_utc(clock.now - timedelta(minutes=1))})

    result = make().run()

    assert (result["runs_checked"], result["orphans_stopped"]) == (1, 2)
    stopped = [i for i in tokens.scan()["Items"] if i.get("task_name") in ("extract", "load")]
    assert {(i["status"], i["error"]) for i in stopped} == {("stopped", "its run row is missing")}


def test_a_further_orphan_that_cannot_be_stopped_stays_due(make, tokens, clock, mocker):
    for task in ("extract", "load"):
        name = f"{task}-{DATE}{RUN[-20:]}"
        tokens.put_item(Item={"execution_name": name, "task_name": task, "status": "waiting", "generation": 1,
                              "pipeline_execution": RUN, att.KEY: att.attention(att.WAITING, name),
                              att.DUE: format_utc(clock.now - timedelta(minutes=1))})
    sweeper_run = make()
    real = sweeper_run.dispatcher.stop_orphans
    calls: List[int] = []

    def flaky(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls.append(1)
        if len(calls) > 1:
            raise CoordinationTransient("throttled")
        return real(*args, **kwargs)

    mocker.patch.object(sweeper_run.dispatcher, "stop_orphans", side_effect=flaky)
    assert sweeper_run.due_runs(deadline=1e9)["orphans_stopped"] == 1


def test_an_entry_that_names_no_run_is_skipped(make, tokens, clock):
    tokens.put_item(Item={"execution_name": "stray", "status": "waiting", att.KEY: att.attention(att.WAITING, "stray"),
                          att.DUE: format_utc(clock.now - timedelta(minutes=1))})
    assert make().run()["runs_checked"] == 0


def test_the_budget_stops_the_checks_and_leaves_the_rest_due(make, run, tokens, sfn):
    run(passed=False)
    ticks = iter([0.0, 10_000.0])

    result = make(monotonic=lambda: next(ticks, 10_000.0)).run()

    assert result["budget_reached"] and result["runs_checked"] == 0 and sfn.started == []
    assert _row(tokens, RUN_KEY)[att.DUE] == format_utc(NOW)


def test_every_page_of_due_entries_is_read(make, run, tokens, mocker):
    run()
    run(OTHER)
    sweeper_run = make()
    real = sweeper_run.tasks.query
    mocker.patch.object(sweeper_run.tasks, "query", side_effect=lambda **kw: real(**kw, Limit=1)
                        if "Limit" not in kw and "Select" not in kw else real(**kw))
    assert sweeper_run.due_runs(deadline=1e9)["runs_checked"] == 2


def test_a_move_that_fails_leaves_the_run_due_for_the_next_sweep(make, run, tokens, mocker):
    run()
    sweeper_run = make()
    real = sweeper_run.dispatcher.client.update_item

    def update(**kwargs: Any) -> Dict[str, Any]:
        if "#checks" in kwargs.get("ExpressionAttributeNames", {}):
            raise client_error("InternalServerError", "UpdateItem")
        return real(**kwargs)

    mocker.patch.object(sweeper_run.dispatcher.client, "update_item", side_effect=update)
    sweeper_run.due_runs(deadline=1e9)
    assert _row(tokens, RUN_KEY)[att.DUE] == format_utc(NOW)


def test_a_lost_hint_warning_that_cannot_be_written_is_logged(make, run, tokens, clock, mocker):
    run()
    make().run()
    _set(tokens, _key("extract"), status="success", finished_at=format_utc(clock.now - timedelta(minutes=20)))
    _set(tokens, RUN_KEY, **{att.DUE: format_utc(clock.now)})
    sweeper_run = make()
    real = sweeper_run.tasks.put_item
    mocker.patch.object(sweeper_run.tasks, "put_item", side_effect=lambda **kw: (
        (_ for _ in ()).throw(client_error("InternalServerError", "PutItem"))
        if kw["Item"]["execution_name"].startswith("_notify_warn_") else real(**kw)))
    assert sweeper_run.run()["lost_hint_runs"] == 1


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


@pytest.mark.parametrize("broken", [{"run_name": None}, {"first_failed_at": "yesterday"}, {"sfn_arn": None},
                                    {"attempts": "many"}])
def test_an_unreadable_retry_row_is_dropped_with_a_warning_and_the_rest_go_on(make, tokens, sfn, clock, broken):
    """Principle #51: it was read first on every run, and its KeyError stopped every retry behind it."""
    from polyris.coordination.records import push_retry_item

    bad = push_retry_item(run_name="pushed-2026-11-11-and-00000000", consumer="pushed", sfn_arn=PUSHED_ARN,
                          run_input="{}", error="ThrottlingException", now=clock.now - timedelta(hours=1))
    bad[att.DUE] = format_utc(clock.now - timedelta(minutes=5))          # due before the good one
    for field, value in broken.items():
        if value is None:
            del bad[field]
        else:
            bad[field] = value
    tokens.put_item(Item=bad)
    good = _retry_row(tokens, clock)

    result = make().run()

    assert (result["push_retried"], result["push_started"], result["push_given_up"]) == (2, 1, 1)
    assert [s["name"] for s in sfn.started] == [RUN_NAME]
    assert "Item" not in tokens.get_item(Key={"execution_name": bad["execution_name"]})
    assert "Item" not in tokens.get_item(Key={"execution_name": good})
    [warning] = _warnings(tokens, "push")
    assert "cannot be read" in warning["error"]


def test_a_run_started_meanwhile_counts_as_started(make, tokens, sfn, clock):
    row = _retry_row(tokens, clock)
    sfn.started.append({"arn": PUSHED_ARN, "name": RUN_NAME, "input": {}})

    assert make().run()["push_started"] == 1
    assert "Item" not in tokens.get_item(Key={"execution_name": row})


def test_a_transient_start_failure_backs_off(make, tokens, sfn, clock):
    row = _retry_row(tokens, clock, attempts=2)
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")

    make().run()

    retry = _row(tokens, row)
    assert (retry["attempts"], retry[att.DUE]) == (3, format_utc(clock.now + timedelta(minutes=40)))
    assert "ThrottlingException" in retry["error"]


def test_a_permanent_start_failure_becomes_a_warning(make, tokens, sfn, clock):
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
    row = _retry_row(tokens, clock)
    sweeper_run = make()
    real_update, real_delete = sweeper_run.tasks.update_item, sweeper_run.tasks.delete_item
    sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    mocker.patch.object(sweeper_run.tasks, "update_item", side_effect=lambda **kw: (
        real_update(**kw) if kw["Key"]["execution_name"] == "_engine_health"
        else (_ for _ in ()).throw(client_error("InternalServerError", "UpdateItem"))))
    sweeper_run.run()
    assert _row(tokens, row)["attempts"] == 0                     # unchanged, still due

    del sfn.errors["start_execution"]
    mocker.patch.object(sweeper_run.tasks, "delete_item", side_effect=lambda **kw: (
        real_delete(**kw) if kw["Key"]["execution_name"] == sweep.LEASE_KEY
        else (_ for _ in ()).throw(client_error("InternalServerError", "DeleteItem"))))
    sweeper_run.run()
    assert _row(tokens, row)["run_name"] == RUN_NAME               # started, but the row stays to be dropped later


def test_a_retry_row_dropped_meanwhile_is_skipped(make, tokens, sfn, clock, mocker):
    row = _retry_row(tokens, clock)
    sweeper_run = make()
    mocker.patch.object(sweeper_run.tasks, "get_item", return_value={})

    assert sweeper_run.push_retries(deadline=float("inf"))["push_retried"] == 0
    assert sfn.started == [] and _row(tokens, row)


def test_a_push_warning_that_cannot_be_written_still_drops_the_row(make, tokens, sfn, clock, mocker):
    row = _retry_row(tokens, clock, failed_hours_ago=24)
    sweeper_run = make()
    real = sweeper_run.tasks.put_item
    mocker.patch.object(sweeper_run.tasks, "put_item", side_effect=lambda **kw: (
        (_ for _ in ()).throw(client_error("AccessDeniedException", "PutItem"))
        if kw["Item"]["execution_name"].startswith("_notify_warn_") else real(**kw)))

    assert sweeper_run.run()["push_given_up"] == 1
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
    [started] = sfn.started
    assert started["name"].startswith("pushed-2026-11-12-and-")
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
    sweeper_run = make()
    mocker.patch.object(sweeper_run.tasks, "update_item", side_effect=client_error("InternalServerError", "UpdateItem"))

    assert sweeper_run.run()["catch_up"] == "done"
    assert _row(tokens, sweep.HEALTH_KEY) is None


def test_every_page_of_subscriptions_and_facts_is_read(make, consumers, asset_events, pushed, sfn, mocker):
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:00:00.000Z"))
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:30:00.000Z"))
    consumers.put_item(Item={"asset_name": "ns/z", "pipeline_name": "other", "sfn_arn": PUSHED_ARN})
    sweeper_run = make()
    scan, query = sweeper_run.consumers.scan, sweeper_run.facts.query
    mocker.patch.object(sweeper_run.consumers, "scan", side_effect=lambda **kw: scan(**kw, Limit=1))
    mocker.patch.object(sweeper_run.facts, "query", side_effect=lambda **kw: query(**kw, Limit=1))

    assert sweeper_run.run()["facts_replayed"] == 2


# ── the self-check ──────────────────────────────────────────────────────


@pytest.fixture
def lambda_env(monkeypatch):
    """What the Lambda runtime sets for the sweeper: its own name and its region."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "acme-dev-polyris-sweeper")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")


def test_the_self_check_reports_what_the_console_reads(make, tokens, clock, lambda_env):
    from polyris.coordination.records import push_retry_item

    tokens.put_item(Item=push_retry_item(run_name="r", consumer="c", sfn_arn="arn", run_input="{}",
                                         error="x", now=clock.now))

    assert make().run()["healthy"] is True

    health = _row(tokens, sweep.HEALTH_KEY)
    assert (health["checked_at"], health["fanout_mapping"], health["stream"]) == (format_utc(clock.now), "Enabled", "ENABLED")
    assert (health["dispatch_mapping"], health["dispatch_result"], health["tasks_stream"]) == ("Enabled", "OK", "ENABLED")
    assert (health["fanout_result"], health["failed_steps"], health["lost_hint_runs"]) == ("OK", [], 0)
    assert (health["oldest_overdue"], health["push_retry_backlog"]) == ("", 1)
    assert sweep.engine_findings(health, clock.now) == []
    # The names the console's findings put in their text and commands, and when AWS last changed each mapping:
    # a string, because DynamoDB rejects a datetime and with it the whole health row.
    assert (health["fanout_mapping_uuid"], health["fanout_mapping_changed_at"]) == (MAPPING_UUID, format_utc(MAPPING_CHANGED))
    assert (health["dispatch_mapping_uuid"], health["dispatch_mapping_changed_at"]) == (DISPATCH_UUID,
                                                                                         format_utc(MAPPING_CHANGED))
    assert (health["fanout_function"], health["events_table"]) == ("asset-fanout", TABLES["ASSET_EVENTS_TABLE"][1])
    assert (health["dispatcher_function"], health["tasks_table"]) == ("reconcile", TABLES["TOKENS_TABLE"][1])
    assert (health["sweeper_function"], health["region"]) == ("acme-dev-polyris-sweeper", "eu-west-1")


def test_an_unhealthy_engine_logs_the_titles_of_its_findings(make, tokens, clock, lambda_env, capsys):
    import json

    assert make(lambda_client=FakeLambda(result="PROBLEM: Function call failed")).run()["healthy"] is False

    [finding] = sweep.engine_findings(_row(tokens, sweep.HEALTH_KEY), clock.now)
    assert (finding["title"], finding["resource"]) == (
        "Asset event delivery is failing",
        "The last call of the stream mapping of asset-fanout failed: PROBLEM: Function call failed.")
    assert finding["command"] == "aws logs tail /aws/lambda/asset-fanout --region eu-west-1 --since 1h"
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    [logged] = [line for line in lines if line["msg"] == "the coordination engine is unhealthy"]
    assert (logged["level"], logged["findings"]) == ("ERROR", ["Asset event delivery is failing"])


def test_every_step_of_a_run_is_a_method():
    assert all(callable(getattr(sweeper.Sweeper, step, None)) for step in sweep.STEPS)


def test_a_failing_step_skips_neither_the_others_nor_the_self_check(make, tokens, clock, mocker):
    """Principle #38: the failure reaches the console's findings, and the next run tries the step again."""
    sweeper_run = make()
    mocker.patch.object(sweeper_run, "due_runs", side_effect=client_error("ThrottlingException", "Query"))
    catch_up = mocker.spy(sweeper_run, "catch_up")

    result = sweeper_run.run()

    assert catch_up.call_count == 1 and result["healthy"] is False
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health["checked_at"] == format_utc(clock.now)
    assert [s.split(": ")[0] for s in health["failed_steps"]] == ["due_runs"]
    [finding] = sweep.engine_findings(health, clock.now)
    assert finding["title"] == "The periodic re-check could not finish checking the runs that are due"
    assert finding["resource"].startswith("ClientError:") and "ThrottlingException" in finding["resource"]
    assert "Item" not in tokens.get_item(Key={"execution_name": sweep.LEASE_KEY})     # released


@pytest.mark.parametrize("kind", [att.WAITING, att.DECISION])
def test_an_overdue_entry_is_reported(make, tokens, clock, mocker, kind):
    sweeper_run = make()
    mocker.patch.object(sweeper_run, "due_runs", return_value={})            # a run that never got to it
    name = f"load-{DATE}{RUN[-20:]}"
    tokens.put_item(Item={"execution_name": name, att.KEY: att.attention(kind, name),
                          att.DUE: format_utc(clock.now - timedelta(minutes=50))})

    assert sweeper_run.run()["healthy"] is False
    assert _row(tokens, sweep.HEALTH_KEY)["oldest_overdue"] == format_utc(clock.now - timedelta(minutes=50))


@pytest.mark.parametrize("state,shown,title", [
    ("Disabled", "Disabled", "Asset event delivery is off"),
    ("missing", "missing", "Asset event delivery is not set up"),
    ("error", "unknown (AccessDeniedException)", "Asset event delivery could not be checked"),
    (None, "unknown (no State)", "Asset event delivery could not be checked"),     # a listing without a State
])
def test_a_mapping_that_does_not_deliver_facts_is_reported(make, tokens, clock, state, shown, title):
    """The state the sweeper writes is the one the findings tell apart."""
    assert make(lambda_client=FakeLambda(state)).run()["healthy"] is False
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health["fanout_mapping"] == shown
    assert [f["title"] for f in sweep.engine_findings(health, clock.now)] == [title]


@pytest.mark.parametrize("state,result,title", [
    ("Disabled", "OK", "Task delivery is off"),
    ("missing", "OK", "Task delivery is not set up"),
    ("error", "OK", "Task delivery could not be checked"),
    ("Enabled", "PROBLEM: Function call failed", "Task delivery is failing"),
])
def test_a_mapping_that_does_not_deliver_task_changes_is_reported(make, tokens, clock, state, result, title):
    """ADR #137 §6: the tasks table's stream mapping, as the asset-events one."""
    assert make(lambda_client=FakeLambda(tasks=state, tasks_result=result)).run()["healthy"] is False
    assert [f["title"] for f in sweep.engine_findings(_row(tokens, sweep.HEALTH_KEY), clock.now)] == [title]


@pytest.mark.parametrize("table,field,title", [
    ("ASSET_EVENTS_TABLE", "stream", "Asset event recording could not be checked"),
    ("TOKENS_TABLE", "tasks_stream", "Task changes could not be checked"),
])
def test_a_stream_that_cannot_be_read_is_reported(make, tokens, clock, mocker, table, field, title):
    sweeper_run = make()
    real = sweeper_run.dispatcher.client.describe_table
    mocker.patch.object(sweeper_run.dispatcher.client, "describe_table", side_effect=lambda **kw: (
        (_ for _ in ()).throw(client_error("AccessDeniedException", "DescribeTable"))
        if kw["TableName"] == TABLES[table][1] else real(**kw)))
    assert sweeper_run.run()["healthy"] is False
    health = _row(tokens, sweep.HEALTH_KEY)
    assert health[field] == "unknown (AccessDeniedException)"
    assert [f["title"] for f in sweep.engine_findings(health, clock.now)] == [title]


def test_a_disabled_stream_is_reported(make, tokens, clock, mocker):
    sweeper_run = make()
    mocker.patch.object(sweeper_run.dispatcher.client, "describe_table",
                        return_value={"Table": {"StreamSpecification": {"StreamEnabled": False}}})
    sweeper_run.run()
    health = _row(tokens, sweep.HEALTH_KEY)
    assert (health["stream"], health["tasks_stream"]) == ("DISABLED", "DISABLED")
    assert [f["title"] for f in sweep.engine_findings(health, clock.now)] == [
        "Asset events are not recorded for delivery", "Task changes are not recorded for delivery"]


def test_a_health_row_that_cannot_be_written_does_not_fail_the_run(make, tokens, mocker):
    sweeper_run = make()
    real = sweeper_run.tasks.update_item
    mocker.patch.object(sweeper_run.tasks, "update_item", side_effect=lambda **kw: (
        (_ for _ in ()).throw(client_error("InternalServerError", "UpdateItem"))
        if kw["Key"]["execution_name"] == sweep.HEALTH_KEY else real(**kw)))

    assert "healthy" in sweeper_run.run()


def test_a_self_check_reading_that_fails_is_reported_and_the_row_still_written(make, tokens, clock, mocker):
    sweeper_run = make()
    real = sweeper_run.tasks.query

    def query(**kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("Limit") == 1:                          # the oldest overdue row
            raise client_error("ProvisionedThroughputExceededException", "Query")
        return real(**kwargs)

    mocker.patch.object(sweeper_run.tasks, "query", side_effect=query)

    assert sweeper_run.run()["healthy"] is False
    [failure] = _row(tokens, sweep.HEALTH_KEY)["failed_steps"]
    assert failure.startswith("self_check: the oldest overdue task could not be read: ClientError")
    [finding] = sweep.engine_findings(_row(tokens, sweep.HEALTH_KEY), clock.now)
    assert finding["title"] == "The periodic re-check could not finish its self-check"
    assert finding["resource"].startswith("the oldest overdue task could not be read: ClientError")


def test_every_page_of_the_retry_backlog_is_counted(make, tokens, clock, mocker):
    from polyris.coordination.records import push_retry_item

    for i in range(3):
        item = push_retry_item(run_name=f"r{i}", consumer="c", sfn_arn="arn", run_input="{}", error="x", now=clock.now)
        item[att.KEY] = "retry#0"
        tokens.put_item(Item=item)
    sweeper_run = make()
    real = sweeper_run.tasks.query
    mocker.patch.object(sweeper_run.tasks, "query", side_effect=lambda **kw: real(**kw, Limit=1)
                        if kw.get("Select") == "COUNT" else real(**kw))
    assert sweeper_run._backlog() == 3


# ── the handler ─────────────────────────────────────────────────────────


def test_the_lambda_client_is_created_once(monkeypatch):
    import env

    monkeypatch.setattr(env, "_lambda", None)
    assert env.lambda_() is env.lambda_()


def test_the_handler_runs_a_sweeper_over_the_environments_dispatcher(make, mocker, monkeypatch, rows):
    import dispatcher as dispatching

    sweep_run = mocker.patch.object(sweeper.Sweeper, "run", return_value={"runs_checked": 0})
    monkeypatch.setattr(dispatching, "_DISPATCHER", rows.dispatcher)

    class Context:
        aws_request_id = "request-1"

    assert sweeper.handler({}, Context()) == {"runs_checked": 0}
    sweep_run.assert_called_once()
