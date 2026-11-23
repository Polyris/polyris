"""The dispatcher's passes (ADR #137 §1, §4, §7), over runs Start_Run wrote, against DynamoDB (moto) as the template
declares it and Step Functions faked at the boto3 boundary."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

import env
import start_run as start_run_lambda
from coordination_testkit import TABLES, FakeSfn, client_error, fact
from dispatcher import RESERVE_MS, Dispatcher
from polyris.coordination import attention, run_name, run_row, spec_store, start_run, sweep, task_row, task_spec
from polyris.coordination.records import DECISION_EVENT_ORDER, DEPS_BLOCKED, DEPS_READY
from polyris.coordination.times import format_utc

NOW = datetime(2026, 11, 12, 6, 0, 5, tzinfo=timezone.utc)
EXECUTION = "orders-run-2026-11-12-abcd1234"
START = "2026-11-12T06:00:00.123Z"
DATE = "2026-11-12"
RUN_KEY = run_row.key(EXECUTION)
RUN_TASK = "arn:aws:states:us-east-1:123456789012:stateMachine:run-task"
_ser, _de = TypeSerializer(), TypeDeserializer()


def table(var: str) -> str:
    return TABLES[var][1]


def spec(task_id: str, deps=(), rule: str = "all_success", **extra: Any) -> Dict[str, Any]:
    return {"task_id": task_id, "task_type": "sfn", "task_arn": f"arn:aws:states:us-east-1:1:stateMachine:{task_id}",
            "dependencies": list(deps), "trigger_rule": rule, **extra}


CHAIN = [spec("extract"), spec("load", ["extract"])]


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


@pytest.fixture
def world(ddb, monkeypatch):
    import boto3

    client = boto3.client("dynamodb", region_name="us-east-1")
    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_dynamodb_client", client)
    clock = Clock()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:          # type: ignore[override]
            return clock.at

    monkeypatch.setattr(start_run_lambda, "datetime", Frozen)
    sfn = FakeSfn()
    dispatcher = Dispatcher(client, sfn, resource=ddb, tokens_table=table("TOKENS_TABLE"),
                            subscriptions_table=table("SUBSCRIPTIONS_TABLE"), specs_table=table("SPECS_TABLE"),
                            asset_events_table=table("ASSET_EVENTS_TABLE"), pipelines_table=table("PIPELINES_TABLE"),
                            task_events_table=table("TASK_EVENTS_TABLE"), run_task_arn=RUN_TASK, clock=clock.now,
                            sleep=lambda _: None)
    return SimpleNamespace(client=client, ddb=ddb, sfn=sfn, dispatcher=dispatcher, clock=clock)


def start(world, specs: List[Dict[str, Any]], **inputs: Any) -> None:
    version = task_spec.build_version("orders", specs)
    spec_store.write(world.client, table("SPECS_TABLE"), version, sleep=lambda _: None)
    start_run_lambda.handler({"format_version": task_spec.FORMAT_VERSION, "spec_id": version.spec_id,
                              "task_count": version.task_count, "pipeline_name": "orders", "execution_name": EXECUTION,
                              "execution_arn": PIPELINE_EXECUTION, "execution_start": START, "token": "run-token", "retry_count": 0, "redrive_count": 0,
                              "date": DATE, "input": {"triggered_by": "schedule", **inputs}}, None)


def full(world, **kwargs: Any) -> Dict[str, Any]:
    return world.dispatcher.full_pass(RUN_KEY, source="test", remaining_ms=kwargs.get("remaining", lambda: 60_000))


def key(task: str) -> str:
    return task_row.key(task, DATE, EXECUTION)


def item(world, item_key: str) -> Dict[str, Any]:
    raw = world.client.get_item(TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": item_key}},
                                ConsistentRead=True).get("Item")
    return {k: _de.deserialize(v) for k, v in (raw or {}).items()}


def update(world, item_key: str, **fields: Any) -> None:
    names = {f"#a{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": _ser.serialize(v) for i, v in enumerate(fields.values())}
    world.client.update_item(TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": item_key}},
                             UpdateExpression="SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(fields))),
                             ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def finish(world, task: str, status: str = "success") -> None:
    """What run_task writes at the end of a task's work."""
    update(world, key(task), status=status, finished_at=world.clock.at.isoformat().replace("+00:00", "Z"))


# ── the passes ──────────────────────────────────────────────────────────────

def test_a_pass_starts_the_root_tasks_under_their_derived_names_and_the_rest_wait(world):
    start(world, CHAIN)
    result = full(world)
    assert (result["started"], result["changed"], result["left"]) == (1, 2, False)
    extract, load = item(world, key("extract")), item(world, key("load"))
    assert extract["status"] == "deps_ready"
    assert extract["task_run_id"] == run_name.run_task_name(key("extract"), START, 1)
    [started] = world.sfn.started
    assert (started["arn"], started["name"]) == (RUN_TASK, extract["task_run_id"])
    assert (started["input"]["task_name"], started["input"]["upstreams"]) == ("extract", {})
    assert (load["status"], load["reasons"]) == ("waiting", ["waiting for its upstreams"])


def test_an_upstreams_success_starts_its_dependant_with_the_upstreams_outcome(world):
    start(world, CHAIN)
    full(world)
    finish(world, "extract")
    full(world)
    assert item(world, key("load"))["status"] == "deps_ready"
    assert world.sfn.started[-1]["input"]["upstreams"] == {"extract": {"status": "success", "skip_origin": None}}


def test_a_failure_cascades_within_one_pass_and_the_run_fails_with_the_cause(world):
    start(world, [spec("extract"), spec("load", ["extract"]), spec("report", ["load"])])
    full(world)
    finish(world, "extract", "failed")
    full(world)
    assert item(world, key("load"))["status"] == "upstream_failed"
    assert item(world, key("report"))["status"] == "upstream_failed"     # decided in the same pass
    [answer] = world.sfn.failed
    assert (answer["token"], answer["error"]) == ("run-token", run_row.FAILED_RUN_ERROR)
    assert answer["cause"] == "3 tasks failed: extract, load, report"


def test_a_run_whose_rows_are_final_is_answered_marked_summed_and_closed(world):
    start(world, CHAIN)
    full(world)
    finish(world, "extract")
    full(world)
    finish(world, "load")
    full(world)
    [answer] = world.sfn.succeeded
    assert answer["token"] == "run-token" and answer["output"]["outcome"] == "success"
    run = item(world, RUN_KEY)
    assert (run["answered_order"], run["summary"]) == (run["token_order"], {"success": 2})
    assert "attention" not in run and "attention_at" not in run
    full(world)
    assert len(world.sfn.succeeded) == 1                             # answered once


def test_a_paused_run_holds_its_ready_rows_and_starts_none(world):
    start(world, CHAIN)
    update(world, RUN_KEY, pauses=[[int((NOW - timedelta(minutes=1)).timestamp())]])
    full(world)
    assert item(world, key("extract"))["status"] == "waiting_paused" and world.sfn.started == []


def test_a_stop_stops_every_unfinished_row_answers_the_run_and_records_the_stop_done(world):
    start(world, CHAIN)
    full(world)
    update(world, RUN_KEY, stop={"by": "ops", "reason": "wrong date", "at": "2026-11-12T06:01:00.000Z"})
    full(world)
    extract, load = item(world, key("extract")), item(world, key("load"))
    assert (extract["status"], extract["generation"], load["status"]) == ("stopped", 2, "stopped")
    assert extract["error"] == "the run was stopped by ops: wrong date"
    assert world.sfn.failed and "stop_done" in item(world, RUN_KEY)
    # Cleanup (§4): the started row's run_task, then the run's own execution, before the answer.
    assert [s["arn"] for s in world.sfn.stopped] == [run_task_execution("extract"), PIPELINE_EXECUTION]
    assert {s["error"] for s in world.sfn.stopped} == {"Polyris.Stopped"}


def test_a_run_task_that_cannot_be_stopped_warns_and_its_row_stays_stopped(world):
    from coordination_testkit import client_error
    start(world, CHAIN)
    full(world)
    world.sfn.errors["stop_execution"] = client_error("AccessDeniedException", "StopExecution")
    update(world, RUN_KEY, stop={"by": "ops", "reason": "wrong date", "at": "2026-11-12T06:01:00.000Z"})
    full(world)
    assert item(world, key("extract"))["status"] == "stopped"
    assert any("could not be stopped" in w["error"] for w in warnings(world))
    assert "stop_done" in item(world, RUN_KEY)                       # the answer ended the execution instead


def test_a_run_not_yet_active_starts_nothing(world):
    start(world, CHAIN)
    update(world, RUN_KEY, status="creating")
    assert full(world)["action"] == "creating" and world.sfn.started == []


def test_a_row_of_a_run_whose_run_row_is_missing_is_stopped_when_a_change_wakes_it(world):
    start(world, CHAIN)
    world.client.delete_item(TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": RUN_KEY}})
    restarted = item(world, key("load"))
    record = {"eventName": "MODIFY", "dynamodb": {
        "SequenceNumber": "100", "Keys": {"execution_name": {"S": key("load")}},
        "OldImage": {k: _ser.serialize(v) for k, v in {**restarted, "status": "failed"}.items()},
        "NewImage": {k: _ser.serialize(v) for k, v in restarted.items()}}}
    assert world.dispatcher.handle([record], lambda: 60_000) == {"batchItemFailures": []}
    assert item(world, key("load"))["error"] == "its run row is missing"


# ── starts ──────────────────────────────────────────────────────────────────

def test_a_throttled_start_is_counted_and_tried_again_at_its_next_try(world):
    start(world, CHAIN)
    world.sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    full(world)
    extract = item(world, key("extract"))
    assert (extract["status"], extract["start_failures"]) == ("deps_ready", 1) and "Throttling" in extract["error"]
    del world.sfn.errors["start_execution"]
    full(world)
    assert world.sfn.started == []                                    # before its next try
    world.clock.at = NOW + timedelta(seconds=31)
    full(world)
    assert [s["name"] for s in world.sfn.started] == [extract["task_run_id"]]


def test_an_access_denied_start_warns_and_counts_toward_no_deadline(world):
    start(world, CHAIN)
    world.sfn.errors["start_execution"] = client_error("AccessDeniedException", "StartExecution")
    full(world)
    assert "start_failures" not in item(world, key("extract"))
    warning = item(world, f"_notify_warn_coordination_{key('extract')}")
    assert "access denied" in warning["error"]


def test_a_start_made_already_counts_as_done_and_its_liveness_counts_from_then(world):
    """Its failed attempts go, so its run_task is judged at its liveness check, not retried until 6 hours."""
    start(world, CHAIN)
    full(world)
    update(world, key("extract"), start_failures=1, start_failed_first="2026-11-12T05:59:00.000Z",
           start_failed_last="2026-11-12T05:59:00.000Z", error="ThrottlingException: lost answer")
    full(world)
    extract = item(world, key("extract"))
    assert len(world.sfn.started) == 1
    assert not {"start_failures", "start_failed_first", "start_failed_last", "error"} & set(extract)
    assert extract["status_since"] == format_utc(NOW)
    assert extract["attention_at"] == format_utc(NOW + attention.FIRST_DUE)


def test_a_start_refused_for_good_fails_the_row(world):
    start(world, CHAIN)
    world.sfn.errors["start_execution"] = client_error("StateMachineDoesNotExist", "StartExecution")
    full(world)
    extract = item(world, key("extract"))
    assert extract["status"] == "failed" and "StateMachineDoesNotExist" in extract["error"]


# ── budgets, conflicts, failures ────────────────────────────────────────────

def test_a_pass_out_of_budget_leaves_its_run_row_due_at_once_with_left_at(world):
    start(world, CHAIN)
    result = full(world, remaining=lambda: RESERVE_MS)
    assert result["left"] and result["changed"] == 0
    run = item(world, RUN_KEY)
    assert run["left_at"] == run["attention_at"]


def test_an_action_between_a_passs_read_and_its_write_ends_the_pass_and_the_next_decides_from_new_reads(world):
    """What the pass decided from its read no longer holds once the run moved (§1, §3): it leaves the rest
    through left_at instead of deciding the other rows from that read against the new run_version."""
    start(world, CHAIN)
    real = world.client.transact_write_items
    calls: List[int] = []

    def racing(**kwargs: Any) -> Any:
        if not calls:
            calls.append(1)
            update(world, RUN_KEY, run_version=item(world, RUN_KEY)["run_version"] + 1)   # an action lands
        return real(**kwargs)

    world.client.transact_write_items = racing
    first = full(world)
    assert first["left"] is True and item(world, key("extract"))["status"] == "waiting" and world.sfn.started == []
    assert "left_at" in item(world, RUN_KEY)                                   # its record wakes the next pass
    full(world)
    assert item(world, key("extract"))["status"] == "deps_ready" and len(world.sfn.started) == 1


def test_a_missing_spec_version_fails_the_pass_with_a_warning(world):
    start(world, CHAIN)
    for raw in world.client.scan(TableName=table("SPECS_TABLE"))["Items"]:
        world.client.delete_item(TableName=table("SPECS_TABLE"), Key={"pk": raw["pk"], "sk": raw["sk"]})
    assert full(world)["action"] == "failed"
    assert "a pass over the run failed" in item(world, f"_notify_warn_coordination_{RUN_KEY}-pass")["error"]


def test_a_missing_row_fails_its_dependants_and_counts_as_failed_for_the_run(world):
    start(world, CHAIN)
    world.client.delete_item(TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": key("extract")}})
    full(world)
    assert item(world, key("load"))["error"] == "the row of its upstream extract is missing"
    assert world.sfn.failed and "extract" in world.sfn.failed[0]["cause"]


# ── facts ───────────────────────────────────────────────────────────────────

def test_a_row_whose_facts_arrived_starts_and_its_pointers_go(world):
    start(world, [spec("load", wait_for=[{"asset_name": "lake/prices", "freshness_hours": None}])])
    pointers = world.ddb.Table(table("SUBSCRIPTIONS_TABLE"))
    assert pointers.scan()["Items"]
    full(world)
    assert item(world, key("load"))["status"] == "waiting"                # no fact yet
    world.ddb.Table(table("ASSET_EVENTS_TABLE")).put_item(Item=fact("lake/prices", DATE,
                                                                    event_time="2026-11-12T05:00:00.000Z"))
    full(world)
    assert item(world, key("load"))["status"] == "deps_ready" and pointers.scan()["Items"] == []


# ── the stream ──────────────────────────────────────────────────────────────

def _record(sequence: int, row_key: str, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    return {"eventName": "MODIFY", "dynamodb": {
        "SequenceNumber": str(sequence), "Keys": {"execution_name": {"S": row_key}},
        "OldImage": {k: _ser.serialize(v) for k, v in old.items()},
        "NewImage": {k: _ser.serialize(v) for k, v in new.items()}}}


def test_the_stream_makes_one_pass_per_run_and_drops_what_the_table_does_not_mark(world, monkeypatch):
    start(world, CHAIN)
    row = item(world, key("extract"))
    passes: List[str] = []
    monkeypatch.setattr(world.dispatcher, "_pass", lambda run_key, targets, **_: passes.append(run_key) or {})
    records = [_record(1, key("extract"), {**row, "status": "running"}, {**row, "status": "success"}),
               _record(2, key("load"), {**row, "status": "running"}, {**row, "status": "failed"}),
               _record(3, key("extract"), row, {**row, "ttl": 1})]                  # a TTL write wakes nothing
    assert world.dispatcher.handle(records, lambda: 60_000) == {"batchItemFailures": []}
    assert passes == [RUN_KEY]


def test_a_transient_failure_redelivers_the_batch_from_that_runs_first_record(world, monkeypatch):
    start(world, CHAIN)
    row = item(world, key("extract"))
    other = {**row, "pipeline_execution": "orders-run-2026-11-12-ffff0000"}

    def failing(run_key: str, targets: Any, **_: Any) -> Dict[str, Any]:
        if run_key != RUN_KEY:
            raise start_run_lambda.failures.CoordinationTransient("throttled")
        return {}

    monkeypatch.setattr(world.dispatcher, "_pass", failing)
    records = [_record(5, key("extract"), {**row, "status": "running"}, {**row, "status": "success"}),
               _record(7, "other-key", {**other, "status": "running"}, {**other, "status": "success"}),
               _record(9, key("load"), {**row, "status": "running"}, {**row, "status": "success"})]
    assert world.dispatcher.handle(records, lambda: 60_000) == {"batchItemFailures": [{"itemIdentifier": "7"}]}


# ── more paths ──────────────────────────────────────────────────────────────

def test_a_narrow_pass_decides_the_rows_it_names_and_leaves_the_runs_end(world):
    start(world, CHAIN)
    full(world)
    finish(world, "extract")
    result = world.dispatcher.narrow_pass(RUN_KEY, [key("load")], source="asset", remaining_ms=lambda: 60_000)
    assert (result["rows"], result["started"]) == (2, 1)                  # load, and extract as its upstream
    finish(world, "load")
    world.dispatcher.narrow_pass(RUN_KEY, [key("load")], source="asset", remaining_ms=lambda: 60_000)
    assert world.sfn.succeeded == []                                      # the run's end waits for a full pass


def test_a_run_row_that_became_active_wakes_a_pass(world, monkeypatch):
    start(world, CHAIN)
    run = item(world, RUN_KEY)
    passes: List[str] = []
    monkeypatch.setattr(world.dispatcher, "_pass", lambda run_key, targets, **_: passes.append(run_key) or {})
    record = _record(3, RUN_KEY, {**run, "status": "creating"}, run)
    assert world.dispatcher.handle([record], lambda: 60_000) == {"batchItemFailures": []} and passes == [RUN_KEY]


def test_a_batch_whose_budget_ran_out_is_redelivered(world):
    start(world, CHAIN)
    row = item(world, key("extract"))
    record = _record(4, key("extract"), {**row, "status": "running"}, {**row, "status": "success"})
    assert world.dispatcher.handle([record], lambda: RESERVE_MS - 1) == {"batchItemFailures": [{"itemIdentifier": "4"}]}


def test_a_transient_read_raises_so_the_caller_tries_again(world, monkeypatch):
    start(world, CHAIN)
    monkeypatch.setattr(world.client, "get_item",
                        lambda **_: (_ for _ in ()).throw(client_error("ProvisionedThroughputExceededException")))
    with pytest.raises(start_run_lambda.failures.CoordinationTransient):
        full(world)


def test_a_decision_refused_twice_is_left_to_the_next_pass(world):
    start(world, CHAIN)
    real = world.client.transact_write_items

    def always_racing(**kwargs: Any) -> Any:
        update(world, RUN_KEY, run_version=item(world, RUN_KEY)["run_version"] + 1)
        return real(**kwargs)

    world.client.transact_write_items = always_racing
    assert full(world)["left"] and item(world, RUN_KEY)["left_at"]
    assert item(world, key("extract"))["status"] == "waiting" and world.sfn.started == []


def test_a_conflicting_transaction_is_sent_again(world):
    start(world, CHAIN)
    real = world.client.transact_write_items
    conflicts: List[int] = []

    def conflicting(**kwargs: Any) -> Any:
        if not conflicts:
            conflicts.append(1)
            error = client_error("TransactionCanceledException", "TransactWriteItems")
            error.response["CancellationReasons"] = [{"Code": "TransactionConflict"}, {"Code": "None"}]
            raise error
        return real(**kwargs)

    world.client.transact_write_items = conflicting
    full(world)
    assert item(world, key("extract"))["status"] == "deps_ready" and conflicts == [1]


def test_an_action_between_a_passs_read_and_its_answer_leaves_the_answer_marked(world):
    """D120 (the review of TR-4, break 5): a Restart raises run_version while the pass answers the token it read
    as final. The token was answered, so the mark records it, and the engine never stops the run for an answer
    it did not see marked; the restarted row goes on as after the run's end."""
    start(world, CHAIN)
    full(world)
    finish(world, "extract")
    full(world)
    finish(world, "load")
    real = world.sfn.send_task_success

    def answered_while_an_action_lands(**kwargs: Any) -> Any:
        update(world, RUN_KEY, run_version=int(item(world, RUN_KEY)["run_version"]) + 1)    # the action's raise
        return real(**kwargs)

    world.sfn.send_task_success = answered_while_an_action_lands
    full(world)
    run = item(world, RUN_KEY)
    assert run["answered_order"] == run["token_order"] and len(world.sfn.succeeded) == 1


def test_a_closed_token_counts_as_answered(world):
    start(world, CHAIN)
    for task in ("extract", "load"):
        update(world, key(task), status="success", finished_at="2026-11-12T06:00:04.000Z")
    world.sfn.dead_tokens.add("run-token")                         # the execution ended already
    full(world)
    assert item(world, RUN_KEY)["answered_order"] == item(world, RUN_KEY)["token_order"]


def test_an_answer_refused_for_good_warns_and_leaves_the_run_due(world):
    start(world, CHAIN)
    for task in ("extract", "load"):
        update(world, key(task), status="success", finished_at="2026-11-12T06:00:04.000Z")
    world.sfn.errors["send_task_success"] = client_error("InvalidOutput", "SendTaskSuccess")
    full(world)
    assert "answered_order" not in item(world, RUN_KEY) and item(world, RUN_KEY)["attention_at"]
    assert "could not be answered" in item(world, f"_notify_warn_coordination_{RUN_KEY}-answer")["error"]


def test_a_run_task_input_over_256_kb_fails_the_row(world):
    start(world, CHAIN, variables={"blob": "x" * 270_000})
    full(world)
    extract = item(world, key("extract"))
    assert extract["status"] == "failed" and "over Step Functions' 262144" in extract["error"]


def test_the_stream_handler_runs_the_dispatcher_of_the_environment(world, monkeypatch):
    import dispatcher as module

    monkeypatch.setattr(module, "_DISPATCHER", world.dispatcher)
    assert module.handler({"Records": []}, None) == {"batchItemFailures": []}
    monkeypatch.setattr(module, "_DISPATCHER", None)
    monkeypatch.setattr(env, "_stepfunctions", world.sfn)
    assert isinstance(module._dispatcher(), Dispatcher)


def test_a_passs_budget_goes_on_work_so_each_pass_moves_past_the_rows_that_need_nothing(world):
    """Deciding a row is pure: a pass spends its budget on reads of facts, writes and starts (Rule 52)."""
    import itertools

    start(world, [spec("a"), spec("b"), spec("c")])
    ticks = itertools.count()
    world.dispatcher.monotonic = lambda: float(next(ticks))       # one second per reading of the clock
    one_write = lambda: RESERVE_MS + 1000                          # noqa: E731 — room for one piece of work
    results = [world.dispatcher.full_pass(RUN_KEY, source="test", remaining_ms=one_write) for _ in range(3)]
    assert [r["started"] for r in results] == [1, 1, 1] and [r["left"] for r in results] == [True, True, False]
    assert {item(world, key(t))["status"] for t in "abc"} == {"deps_ready"}


# ── the sweeper's check of a run (ADR #137 §1, §4, §7) ─────────────────────

PIPELINE_EXECUTION = f"arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-orders:{EXECUTION}"


def execution_is(world, status: Any = "RUNNING") -> None:
    """The run's execution as DescribeExecution shows it, under the ARN Start_Run recorded on the run row."""
    world.sfn.executions[PIPELINE_EXECUTION] = status


def check(world, **kwargs: Any) -> Dict[str, Any]:
    return world.dispatcher.full_pass(RUN_KEY, source="sweeper", remaining_ms=lambda: 60_000, check=True, **kwargs)


def run_task_execution(task: str, generation: int = 1) -> str:
    return f"arn:aws:states:us-east-1:123456789012:execution:run-task:{run_name.run_task_name(key(task), START, generation)}"


def warnings(world) -> List[Dict[str, Any]]:
    items = world.client.scan(TableName=table("TOKENS_TABLE"))["Items"]
    return [w for w in ({k: _de.deserialize(v) for k, v in i.items()} for i in items)
            if w["execution_name"].startswith("_notify_warn_")]


def test_only_the_sweepers_pass_judges_a_run_task_and_each_check_backs_off(world):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    world.sfn.executions[run_task_execution("extract")] = "RUNNING"
    world.clock.at = NOW + timedelta(minutes=11)
    full(world)
    assert world.sfn.described == []                                   # a stream pass calls no DescribeExecution
    check(world)
    assert world.sfn.described == [run_task_execution("extract"), PIPELINE_EXECUTION]
    extract = item(world, key("extract"))
    assert (extract["status"], extract["checks"], extract["last_check"]) == ("deps_ready", 1, format_utc(world.clock.at))
    assert extract["attention_at"] == format_utc(world.clock.at + timedelta(minutes=20))
    check(world)
    assert world.sfn.described.count(run_task_execution("extract")) == 1     # not before its next check


def test_a_run_task_that_ended_without_an_outcome_fails_its_row_with_how_it_ended(world):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    update(world, key("extract"), status="running", status_since=format_utc(NOW))
    world.sfn.executions[run_task_execution("extract")] = {"status": "FAILED", "error": "States.Runtime",
                                                           "cause": "the Lambda was deleted"}
    world.clock.at = NOW + timedelta(minutes=11)
    check(world)
    extract = item(world, key("extract"))
    assert extract["status"] == "failed"
    assert extract["error"] == ("its run_task execution ended FAILED (States.Runtime: the Lambda was deleted) "
                                "without recording an outcome")
    assert extract["attention"].startswith("failed#")
    assert item(world, key("load"))["status"] == "upstream_failed"    # decided in the same pass (ADR #138)


def test_a_ready_row_whose_run_task_does_not_exist_is_started_again_under_its_name(world):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    world.sfn.started.clear()                                          # the start never reached Step Functions
    world.clock.at = NOW + timedelta(minutes=11)
    check(world)
    [again] = world.sfn.started
    assert again["name"] == run_name.run_task_name(key("extract"), START, 1)
    assert item(world, key("extract"))["checks"] == 1


def test_a_running_row_whose_run_task_does_not_exist_fails(world):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    update(world, key("extract"), status="running", status_since=format_utc(NOW))
    world.clock.at = NOW + timedelta(minutes=11)
    check(world)
    assert item(world, key("extract"))["error"] == "its run_task execution does not exist"


@pytest.mark.parametrize("code,warned", [("ThrottlingException", False), ("AccessDeniedException", True)])
def test_an_execution_that_cannot_be_described_judges_nothing(world, code, warned):
    """No read failure leads to a start, a close or a run's answer (invariant 10)."""
    start(world, CHAIN)
    full(world)
    execution_is(world, "ABORTED")
    world.sfn.errors["describe_execution"] = client_error(code, "DescribeExecution")
    world.clock.at = NOW + timedelta(minutes=11)
    check(world)
    extract, run = item(world, key("extract")), item(world, RUN_KEY)
    assert extract["status"] == "deps_ready" and "last_check" not in extract and "stop" not in run
    assert bool(warnings(world)) is warned
    world.sfn.errors["describe_execution"] = client_error("ThrottlingException", "DescribeExecution")


def test_a_run_whose_execution_ended_before_its_answer_is_stopped_with_the_cause(world):
    start(world, CHAIN)
    full(world)
    execution_is(world, {"status": "ABORTED"})
    world.sfn.executions[run_task_execution("extract")] = "RUNNING"
    world.clock.at = NOW + timedelta(minutes=3)
    check(world)
    run = item(world, RUN_KEY)
    assert run["stop"]["by"] == "the engine" and run["stop"]["reason"] == "its execution ended ABORTED before the run's answer"
    assert {item(world, key(t))["status"] for t in ("extract", "load")} == {"stopped"}
    [answer] = world.sfn.failed
    assert answer["token"] == "run-token" and "stop_done" in run


def test_a_run_whose_execution_runs_goes_on(world):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    world.clock.at = NOW + timedelta(minutes=3)
    check(world)
    assert "stop" not in item(world, RUN_KEY) and world.sfn.failed == []


def test_a_run_whose_execution_does_not_exist_is_stopped_with_no_registry_read(world):
    """The ARN comes from the run row (Start_Run's $states.context.Execution.Id): a pipeline whose registration
    failed is checked all the same (the review of TR-4)."""
    start(world, CHAIN)
    full(world)
    world.clock.at = NOW + timedelta(minutes=3)
    check(world)
    assert item(world, RUN_KEY)["stop"]["reason"] == "its execution does not exist"
    assert world.sfn.described[-1] == PIPELINE_EXECUTION


def test_a_run_row_without_its_execution_arn_is_not_checked(world):
    start(world, CHAIN)
    full(world)
    world.client.update_item(TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": RUN_KEY}},
                             UpdateExpression="REMOVE run_execution_arn")
    world.clock.at = NOW + timedelta(minutes=3)
    check(world)
    assert "stop" not in item(world, RUN_KEY) and PIPELINE_EXECUTION not in world.sfn.described


def test_a_run_still_creating_after_start_runs_retry_window_is_stopped_with_its_executions_error(world):
    start(world, CHAIN)
    update(world, RUN_KEY, status="creating")
    execution_is(world, {"status": "FAILED", "error": "States.TaskFailed", "cause": "Start_Run timed out"})
    assert check(world)["action"] == "creating" and world.sfn.described == []      # within its window
    world.clock.at = NOW + start_run.retry_window()
    check(world)
    run = item(world, RUN_KEY)
    assert run["stop"]["reason"] == (f"Start_Run did not make the run active within its retry window of "
                                     f"{start_run.retry_window()}; its execution FAILED (States.TaskFailed: "
                                     "Start_Run timed out)")
    assert {item(world, key(t))["status"] for t in ("extract", "load")} == {"stopped"}
    assert world.sfn.failed and "attention" not in item(world, RUN_KEY) and warnings(world) == []


def test_a_creating_run_whose_execution_runs_past_the_window_is_stopped_too(world):
    start(world, CHAIN)
    update(world, RUN_KEY, status="creating")
    execution_is(world)
    world.clock.at = NOW + start_run.retry_window()
    check(world)
    assert item(world, RUN_KEY)["stop"]["reason"] == (
        f"Start_Run did not make the run active within its retry window of {start_run.retry_window()}")


def test_a_check_reports_the_rows_whose_wait_ended_on_a_lost_hint(world):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    finish(world, "extract")                                           # its stream record never came
    world.clock.at = NOW + sweep.LOST_HINT_GRACE + timedelta(seconds=1)
    assert check(world)["lost"] == ["load"]
    assert item(world, key("load"))["status"] == "deps_ready"


@pytest.mark.parametrize("left_at", [False, True])
def test_a_row_within_the_grace_or_left_by_a_pass_is_no_lost_hint(world, left_at):
    start(world, CHAIN)
    full(world)
    execution_is(world)
    finish(world, "extract")
    if left_at:
        update(world, RUN_KEY, left_at=format_utc(NOW))
        world.clock.at = NOW + timedelta(minutes=30)
    else:
        world.clock.at = NOW + sweep.LOST_HINT_GRACE
    assert check(world)["lost"] == []
    assert item(world, key("load"))["status"] == "deps_ready"


def test_a_countdown_that_ended_at_the_check_is_no_lost_hint(world):
    start(world, [spec("extract"), spec("load", ["extract"], wait_before=600)])
    full(world)
    execution_is(world)
    finish(world, "extract")
    full(world)                                                        # load counts down
    world.clock.at = NOW + timedelta(minutes=11)
    assert check(world)["lost"] == []
    assert item(world, key("load"))["status"] == "deps_ready"


@pytest.mark.parametrize("generation,raised", [(None, 1), (0, 1), (3, 4)])
def test_a_row_a_12_run_left_in_flight_is_stopped_without_readiness_fields(world, generation, raised):
    """1.2 rows name no spec version; their stop needs only the status and the generation (*Migration*)."""
    old = "load-2026-11-12-run-2026-11-12-0ld0ld01"
    row = {"execution_name": old, "task_name": "load", "status": "waiting", "pipeline_execution": "old-run",
           "attention": attention.attention(attention.WAITING, old), "attention_at": "2026-11-12T05:00:00.000Z",
           **({"generation": generation} if generation is not None else {})}
    world.client.put_item(TableName=table("TOKENS_TABLE"), Item={k: _ser.serialize(v) for k, v in row.items()})
    result = world.dispatcher.full_pass(run_row.key("old-run"), source="sweeper", remaining_ms=lambda: 60_000,
                                        check=True, orphan_keys=[old, "gone-2026-11-12-x"])
    assert (result["action"], result["stopped"]) == ("orphans", 1)
    stopped = item(world, old)
    assert (stopped["status"], stopped["generation"], stopped["error"]) == ("stopped", raised, "its run row is missing")
    assert "attention" not in stopped


# ── one row never holds up its run; a moved run ends the pass (from the review of TR-4) ──────────────

def _finish_clean(world, task: str, status: str = "success") -> None:
    """What run_task writes at a task's end: the status, and no `waiting` entry left (ADR #137 §3, Attention)."""
    world.client.update_item(
        TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": key(task)}},
        UpdateExpression="SET #s = :s, finished_at = :f REMOVE attention, attention_at",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": status}, ":f": {"S": "2026-11-12T06:00:04.000Z"}})


def _racing(world, action) -> None:
    """`action` lands between the pass's read and its first transaction."""
    real = world.client.transact_write_items
    calls: List[int] = []

    def racing(**kwargs: Any) -> Any:
        if not calls:
            calls.append(1)
            action()
        return real(**kwargs)

    world.client.transact_write_items = racing


@pytest.mark.parametrize("code,fails", [("ValidationException", True), ("AccessDeniedException", False)])
def test_a_row_whose_decision_raises_fails_with_the_cause_and_the_other_rows_go_on(world, code, fails):
    """ADR #137 §3: fail_wait on a CoordinationError; Rule 51: one row never holds up its run. An access denied
    fails no row: the row waits for the permission (§7). Both warn."""
    wait_for = [{"asset_name": "lake/prices", "freshness_hours": None}]
    start(world, [spec("a_waits", wait_for=wait_for), spec("b_root"), spec("c_after_b", ["b_root"])])

    def refuse(params: Dict[str, Any], **_: Any) -> None:
        if params.get("TableName") == table("ASSET_EVENTS_TABLE"):
            raise client_error(code, "Query")

    world.ddb.meta.client.meta.events.register("before-parameter-build.dynamodb.Query", refuse)
    result = full(world)
    a = item(world, key("a_waits"))
    assert result.get("action") != "failed" and item(world, key("b_root"))["status"] == "deps_ready"
    assert (a["status"] == "failed") is fails
    if fails:
        assert "could not be decided" in a["error"] and code in a["error"]
    assert code in item(world, f"_notify_warn_coordination_{key('a_waits')}")["error"]


def test_a_row_that_cannot_be_read_counts_as_missing_and_says_so(world):
    start(world, [spec("a_bad"), spec("b_good"), spec("c_after_a", ["a_bad"])])
    update(world, key("a_bad"), waiting_since="yesterday")                      # a time no reader can parse
    result = full(world)
    assert result.get("action") != "failed" and item(world, key("b_good"))["status"] == "deps_ready"
    assert item(world, key("c_after_a"))["status"] == "failed"               # its upstream's row is missing
    assert "cannot be read" in item(world, f"_notify_warn_coordination_{key('a_bad')}")["error"]


def test_a_start_whose_upstream_row_went_missing_fails_the_row_with_the_cause(world):
    start(world, CHAIN)
    full(world)
    _finish_clean(world, "extract")
    world.sfn.errors["start_execution"] = client_error("ThrottlingException", "StartExecution")
    full(world)
    assert item(world, key("load"))["start_failures"] == 1
    del world.sfn.errors["start_execution"]
    world.client.delete_item(TableName=table("TOKENS_TABLE"), Key={"execution_name": {"S": key("extract")}})
    world.clock.at = NOW + timedelta(minutes=1)
    assert full(world).get("action") != "failed"
    load = item(world, key("load"))
    assert load["status"] == "failed" and "the row of its upstream extract is missing" in load["error"]


@pytest.mark.parametrize("names", ["sink_first", "root_first"])
def test_a_long_chain_starts_its_root_and_its_stop_stops_every_row(world, names):
    """A pass orders its rows without recursion (dispatch.upstreams_first, stop_order; their gate tests run a
    chain of 2,000 under a recursion limit of 100). Here a chain whose sink sorts first, and one whose root does,
    goes through a whole pass and a whole stop. moto copies every table, its stream records included, for each
    transaction, so the chain here is 60 long."""
    n = 60
    name = (lambda i: f"t{n - i:05d}") if names == "sink_first" else (lambda i: f"t{i:05d}")
    start(world, [spec(name(i), [name(i - 1)] if i else []) for i in range(n)])
    assert full(world)["started"] == 1
    update(world, RUN_KEY, stop={"by": "ops", "reason": "wrong date", "at": "2026-11-12T06:01:00.000Z"})
    assert full(world).get("action") != "failed"
    assert {item(world, key(name(i)))["status"] for i in (0, n // 2, n - 1)} == {"stopped"}


@pytest.mark.parametrize("reason,left", [("TransactionConflict", True), ("ThrottlingError", None)])
def test_a_transaction_that_keeps_conflicting_leaves_the_row_and_a_throttled_one_is_transient(world, reason, left):
    from failures import CoordinationTransient

    start(world, CHAIN)

    def cancelled(**kwargs: Any) -> Any:
        error = client_error("TransactionCanceledException", "TransactWriteItems")
        error.response["CancellationReasons"] = [{"Code": reason}, {"Code": "None"}]
        raise error

    world.client.transact_write_items = cancelled
    if left is None:
        with pytest.raises(CoordinationTransient):
            full(world)
        return
    result = full(world)
    assert result.get("action") != "failed" and result["left"] is True and "left_at" in item(world, RUN_KEY)
    assert not item(world, f"_notify_warn_coordination_{RUN_KEY}-pass")         # no false warning


@pytest.mark.parametrize("conflicts, sent", [(1, True), (99, False)])
def test_a_single_write_that_meets_a_transaction_backs_off_and_then_is_decided_again(world, conflicts, sent):
    """A write with no transition is one UpdateItem (a due time moved by a check, say), and DynamoDB refuses it
    while run_task's success transaction holds the row (TransactionConflictException). It is sent again after a
    short backoff, and one that keeps meeting it is decided again from new reads (False), instead of ending the
    whole pass as transient (the review of TR-5)."""
    start(world, [spec("extract")])
    real, left = world.client.update_item, [conflicts]

    def conflicting(**kwargs: Any) -> Any:
        if left[0] > 0:
            left[0] -= 1
            raise client_error("TransactionConflictException", "UpdateItem")
        return real(**kwargs)

    world.client.update_item = conflicting
    update = {"TableName": table("TOKENS_TABLE"), "Key": {"execution_name": {"S": key("extract")}},
              "UpdateExpression": "SET #n = :v", "ExpressionAttributeNames": {"#n": "note"},
              "ExpressionAttributeValues": {":v": {"S": "checked"}}}
    assert world.dispatcher._send_items([{"Update": update}]) is sent      # never CoordinationTransient
    assert (item(world, key("extract")).get("note") == "checked") is sent


def test_a_stop_mark_that_lands_mid_pass_starts_no_row_and_the_next_pass_stops_them(world):
    """ADR §4: no row becomes deps_ready while a stop is under way; the mark is the engine's, as a concurrent
    sweeper pass writes it."""
    from polyris.coordination import dispatch, dispatch_writes

    start(world, [spec(f"t{i}") for i in range(5)])

    def engine_stops_the_run() -> None:
        run = dispatch.run_view(item(world, RUN_KEY))
        world.client.update_item(**dispatch_writes.engine_stop(
            run, table=table("TOKENS_TABLE"), cause="its execution ended ABORTED before the run's answer", now=NOW))

    _racing(world, engine_stops_the_run)
    full(world)
    assert world.sfn.started == []
    full(world)
    assert {item(world, key(f"t{i}"))["status"] for i in range(5)} == {"stopped"} and world.sfn.started == []


def test_a_describe_of_a_run_task_waits_for_the_passs_budget(world):
    n = 60
    start(world, [spec(f"t{i:03d}") for i in range(n)])
    full(world)
    for i in range(n):
        update(world, key(f"t{i:03d}"), status="running")
    world.clock.at = NOW + timedelta(minutes=11)                              # every row's liveness check has come
    ticks = {"t": 0.0}
    world.dispatcher.monotonic = lambda: ticks["t"]

    def throttled(**kwargs: Any) -> Any:
        ticks["t"] += 1.5                                                     # botocore's retries of a throttled call
        world.sfn.described.append(kwargs["executionArn"])
        raise client_error("ThrottlingException", "DescribeExecution")

    world.sfn.describe_execution = throttled
    result = world.dispatcher.full_pass(RUN_KEY, source="sweeper", remaining_ms=lambda: RESERVE_MS + 10_000,
                                        check=True)
    rows = [d for d in world.sfn.described if ":execution:run-task:" in d]
    assert result["left"] is True and len(rows) < n and ticks["t"] <= 10 + 1.5


def test_a_denied_start_right_after_ready_keeps_the_rows_due_time(world):
    start(world, [spec("load", wait_for=[{"asset_name": "lake/prices", "freshness_hours": None}])])
    full(world)
    world.ddb.Table(table("ASSET_EVENTS_TABLE")).put_item(Item=fact("lake/prices", DATE,
                                                                    event_time="2026-11-12T05:00:00.000Z"))
    world.sfn.errors["start_execution"] = client_error("AccessDeniedException", "StartExecution")
    full(world)
    after = item(world, key("load"))
    assert after["status"] == "deps_ready" and "attention_at" in after         # its check comes back to it


def test_the_rows_that_wait_for_the_same_facts_read_them_once_per_pass(world):
    n = 50
    wait_for = [{"asset_name": "lake/prices", "freshness_hours": None}]
    start(world, [spec(f"t{i:03d}", wait_for=wait_for) for i in range(n)])
    queries: List[str] = []
    world.ddb.meta.client.meta.events.register("before-call.dynamodb.Query", lambda **_: queries.append("q"))
    full(world)
    assert 0 < len(queries) <= 2                                              # the index and the recent writes


def test_a_row_whose_fact_is_there_is_reached_behind_rows_that_still_wait(world):
    late = [{"asset_name": "lake/late", "freshness_hours": None}]
    there = [{"asset_name": "lake/there", "freshness_hours": None}]
    start(world, [spec(f"a{i:03d}", wait_for=late) for i in range(30)] + [spec("z_ready", wait_for=there)])
    world.ddb.Table(table("ASSET_EVENTS_TABLE")).put_item(Item=fact("lake/there", DATE,
                                                                    event_time="2026-11-12T05:00:00.000Z"))
    ticks = iter(range(10 ** 9))
    world.dispatcher.monotonic = lambda: float(next(ticks))     # a second per reading: 10 reads or writes a pass
    passes: List[Dict[str, Any]] = []
    while not passes or passes[-1]["left"]:
        passes.append(world.dispatcher.full_pass(RUN_KEY, source="test", remaining_ms=lambda: RESERVE_MS + 10_000))
        assert len(passes) <= 5, passes
    # Each pass spends its budget on work: the waiting rows' first writes, and one read of the facts they share.
    # So every pass moves on, and the row whose fact is there is reached, instead of never (the review of TR-4).
    assert all(p["changed"] > 0 for p in passes) and item(world, key("z_ready"))["status"] == "deps_ready"


# ── what a pass reads of the specs (D117) ───────────────────────────────────

def _spec_calls(world) -> List[str]:
    calls: List[str] = []

    def record(params: Dict[str, Any], model: Any, **_: Any) -> None:
        tables = {params.get("TableName")} | set((params.get("RequestItems") or {}).keys())
        if table("SPECS_TABLE") in tables:
            calls.append(model.name)

    world.client.meta.events.register("provide-client-params.dynamodb.*", record)
    return calls


def test_a_pass_keeps_the_task_names_and_a_start_reads_its_spec_text_by_the_rows_hash(world):
    """D117: no pass loads a whole spec version; the names are read once per version, a text once per start."""
    start(world, CHAIN)
    calls = _spec_calls(world)
    full(world)                                                   # extract starts
    finish(world, "extract")
    full(world)                                                   # load starts
    assert calls == ["Query", "GetItem", "GetItem"] and len(world.sfn.started) == 2


def test_the_spec_texts_a_container_keeps_are_bounded_by_bytes(world, monkeypatch):
    import dispatcher

    monkeypatch.setattr(dispatcher, "TEXTS_CACHE_BYTES", 1)        # no two texts fit: the older one goes
    start(world, [spec("a"), spec("b")])
    calls = _spec_calls(world)
    full(world)
    assert calls.count("GetItem") == 2 and len(world.dispatcher._texts) == 1


# ── the timeline (ADR #137 §1) ──────────────────────────────────────────────

def timeline(world, task: str) -> List[Dict[str, Any]]:
    response = world.client.query(TableName=table("TASK_EVENTS_TABLE"), IndexName="execution-name-index",
                                  KeyConditionExpression="execution_name = :e",
                                  ExpressionAttributeValues={":e": {"S": key(task)}})
    return sorted(({k: _de.deserialize(v) for k, v in i.items()} for i in response["Items"]),
                  key=lambda e: e["event_time"])


def test_a_ready_row_writes_deps_ready_under_its_run_tasks_name(world):
    start(world, CHAIN)
    full(world)
    [event] = timeline(world, "extract")
    assert event["event_type"] == DEPS_READY and event["event_time"].endswith(DECISION_EVENT_ORDER)
    assert event["task_run_id"] == item(world, key("extract"))["task_run_id"]      # one timeline per generation
    assert (event["parent_execution_id"], event["partition_key"]) == (EXECUTION, DATE)
    assert timeline(world, "load") == []                                        # a wait writes no event


def test_a_blocked_row_writes_deps_blocked_with_the_reason(world):
    start(world, CHAIN)
    full(world)
    finish(world, "extract", status="failed")
    full(world)
    [event] = timeline(world, "load")
    assert event["event_type"] == DEPS_BLOCKED
    assert [event["reason"]] == item(world, key("load"))["reasons"]                # the reason the row records


def test_a_start_refused_for_good_ends_the_timeline_with_the_failure(world):
    """A failure run_task never recorded ends the timeline as run_task's own does (records.FAILURE_EVENTS)."""
    start(world, CHAIN)
    world.sfn.errors["start_execution"] = client_error("StateMachineDoesNotExist", "StartExecution")
    full(world)
    finished = [e for e in timeline(world, "extract") if e["event_type"] == "TASK_FINISHED"]
    [event] = finished
    assert event["status"] == "failed" and "StateMachineDoesNotExist" in event["error_summary"]
    assert event["task_run_id"] == item(world, key("extract")).get("task_run_id", event["task_run_id"])


def test_a_timeline_that_cannot_be_written_holds_up_no_decision(world):
    """Best effort, as 1.2's wrapper wrote it: the row holds the state (ADR #137 §1)."""
    start(world, CHAIN)

    def refuse(params: Dict[str, Any], **_: Any) -> None:
        if params.get("TableName") == table("TASK_EVENTS_TABLE"):
            raise client_error("AccessDeniedException", "PutItem")

    world.client.meta.events.register("before-parameter-build.dynamodb.PutItem", refuse)
    assert full(world)["started"] == 1
    assert item(world, key("extract"))["status"] == "deps_ready" and timeline(world, "extract") == []


# ── requests: a Restart's pointers and the reset of its dependants (ADR #137 §3) ──────────────────────────────

def restart(world, task: str, *, with_dependants: bool = False) -> None:
    """The console's Restart, through the builder it sends (coordination.actions)."""
    from polyris.coordination import actions
    write = actions.restart(task=item(world, key(task)), run=item(world, RUN_KEY), with_dependants=with_dependants,
                            operator="ops", reason="rerun", now=world.clock.at, tokens_table=table("TOKENS_TABLE"))
    world.client.transact_write_items(TransactItems=write.items)


def test_a_restart_with_dependants_reruns_what_the_failure_decided_and_the_request_clears(world):
    start(world, [spec("extract"), spec("load", ["extract"]), spec("report", ["load"]), spec("side")])
    full(world)
    finish(world, "side")
    finish(world, "extract", "failed")
    full(world)
    assert item(world, key("report"))["status"] == "upstream_failed" and len(world.sfn.failed) == 1
    world.clock.at += timedelta(minutes=5)
    restart(world, "extract", with_dependants=True)
    full(world)
    assert {t: item(world, key(t))["status"] for t in ("extract", "load", "report", "side")} == {
        "extract": "deps_ready", "load": "waiting", "report": "waiting", "side": "success"}
    assert item(world, key("load"))["generation"] == item(world, key("report"))["generation"] == 2
    assert run_row.RESET_DEPENDANTS in item(world, RUN_KEY)[run_row.REQUESTS]          # a pass that reset rows
    full(world)
    assert item(world, RUN_KEY)[run_row.REQUESTS] == {}                              # the next resets nothing
    for task in ("extract", "load", "report"):
        finish(world, task)
        full(world)
    assert len(world.sfn.failed) == 1 and not world.sfn.succeeded     # its token was answered once, at the failure
    run = item(world, RUN_KEY)
    assert run["summary"] == {"success": 4} and "attention" not in run


def test_a_restart_writes_the_pointers_of_the_rows_it_put_back_to_wait_and_renews_nothing(world):
    """D126: no TTL is renewed; the pass writes the pointers of the rows that wait since the request alone, the one
    a row lost when it left its wait (Rule 40), and not those of a row that waits since Start_Run."""
    wait_for = [{"asset_name": "ns/orders"}]
    start(world, [spec("extract", wait_for=wait_for), spec("load", ["extract"]),
                  spec("audit", wait_for=[{"asset_name": "ns/audit"}])])
    full(world)
    finish(world, "extract", "failed")
    full(world)
    world.client.delete_item(TableName=table("SUBSCRIPTIONS_TABLE"),      # extract left its wait (Rule 40)
                             Key={"dependency_key": {"S": "asset:ns/orders"}, "subscriber": {"S": key("extract")}})
    world.clock.at += timedelta(days=2)              # 1.2's renewal would have rewritten every TTL by now
    load_ttl = item(world, key("load"))["ttl"]
    puts = []
    real = world.dispatcher.client.put_item

    def put_item(**kwargs):
        if kwargs["TableName"] == table("SUBSCRIPTIONS_TABLE"):
            puts.append(kwargs["Item"]["subscriber"]["S"])
        return real(**kwargs)

    world.dispatcher.client.put_item = put_item
    restart(world, "extract")
    full(world)
    assert puts == [key("extract")]                  # audit waits since Start_Run: its pointer is there already
    assert item(world, key("load"))["ttl"] == load_ttl                             # nothing renewed
    assert item(world, RUN_KEY)[run_row.REQUESTS] == {}


def test_a_second_restart_before_a_pass_keeps_the_first_requests_time(world):
    """The pointers request keeps its first time (if_not_exists), so both restarted rows get their pointers."""
    start(world, [spec("a", wait_for=[{"asset_name": "ns/a"}]), spec("b", wait_for=[{"asset_name": "ns/b"}])])
    full(world)
    finish(world, "a", "failed")
    finish(world, "b", "failed")
    full(world)
    restart(world, "a")
    first = item(world, RUN_KEY)[run_row.REQUESTS][run_row.POINTERS]["at"]
    world.clock.at += timedelta(minutes=3)
    restart(world, "b")
    assert item(world, RUN_KEY)[run_row.REQUESTS][run_row.POINTERS]["at"] == first
    full(world)
    subscribers = sorted(p["subscriber"]["S"] for p in world.client.scan(TableName=table("SUBSCRIPTIONS_TABLE"))["Items"])
    assert subscribers == sorted([key("a"), key("b")])


def test_a_narrow_pass_over_a_run_with_a_pending_reset_decides_nothing_and_leaves_it_to_the_full_pass(world):
    """Review of TR-6, B-4: a fact's narrow pass would decide a dependant from an upstream the reset has not reached."""
    start(world, [spec("extract"), spec("load", ["extract"])])
    full(world)
    finish(world, "extract", "failed")
    full(world)
    restart(world, "extract", with_dependants=True)
    result = world.dispatcher.narrow_pass(RUN_KEY, [key("load")], source="test", remaining_ms=lambda: 60_000)
    assert result["left"] and result["changed"] == 0 and item(world, key("load"))["status"] == "upstream_failed"
    assert run_row.LEFT_AT in item(world, RUN_KEY)
    full(world)
    assert item(world, key("load"))["status"] == "waiting"                    # the full pass reset it


def test_a_pointer_the_role_may_not_write_warns(world, monkeypatch):
    from coordination_testkit import client_error
    start(world, [spec("extract", wait_for=[{"asset_name": "ns/orders"}])])
    full(world)
    finish(world, "extract", "failed")
    full(world)
    real = world.dispatcher.client.put_item

    def put_item(**kwargs):
        if kwargs["TableName"] == table("SUBSCRIPTIONS_TABLE"):
            raise client_error("AccessDeniedException", "PutItem")
        return real(**kwargs)

    monkeypatch.setattr(world.dispatcher.client, "put_item", put_item)
    restart(world, "extract")
    full(world)
    assert any("asset pointers cannot be written" in w["error"] for w in warnings(world))


def test_a_stop_of_the_run_cancels_a_pending_reset(world):
    """ADR #137 §3: the engine's own stop too, which the console's write did not reach (review P10)."""
    start(world, [spec("extract"), spec("load", ["extract"])])
    full(world)
    finish(world, "extract", "failed")
    full(world)
    restart(world, "extract", with_dependants=True)
    update(world, RUN_KEY, stop={"by": "the engine", "reason": "its execution ended", "at": "2026-11-12T06:02:00.000Z"})
    full(world)
    assert run_row.RESET_DEPENDANTS not in item(world, RUN_KEY)[run_row.REQUESTS]
    assert item(world, key("load"))["status"] == "upstream_failed"          # not reset under the stop


def test_a_request_that_cannot_be_carried_out_warns_and_the_pass_decides(world):
    """Rule 51: a request with no time, as only a hand could leave it, holds up no decision (review P11)."""
    start(world, [spec("extract")])
    update(world, RUN_KEY, requests={run_row.POINTERS: {"by": "ops"}})
    full(world)
    assert item(world, key("extract"))["status"] == "deps_ready"
    assert any("request of the run could not be carried out" in w["error"] for w in warnings(world))


def test_a_narrow_pass_drops_the_pointers_of_a_row_an_action_finished(world):
    """Rule 40: an operator's action takes the row out of its wait with no decision of a pass (review P4)."""
    start(world, [spec("extract", wait_for=[{"asset_name": "ns/orders"}])])
    pointer = {"dependency_key": {"S": "asset:ns/orders"}, "subscriber": {"S": key("extract")}}
    assert world.client.get_item(TableName=table("SUBSCRIPTIONS_TABLE"), Key=pointer).get("Item")
    finish(world, "extract", "skipped")                                       # as the console's Skip leaves it
    world.dispatcher.narrow_pass(RUN_KEY, [key("extract")], source="asset", remaining_ms=lambda: 60_000)
    assert not world.client.get_item(TableName=table("SUBSCRIPTIONS_TABLE"), Key=pointer).get("Item")


def test_a_stop_after_a_restart_writes_no_pointer_and_clears_the_request(world):
    """Review of TR-6, B-2: a pass that stops the run writes no pointer it would delete at once."""
    start(world, [spec(f"t{i:02d}", wait_for=[{"asset_name": f"ns/a{i}"}]) for i in range(6)])
    full(world)
    finish(world, "t00", "failed")
    full(world)
    restart(world, "t00")
    update(world, RUN_KEY, stop={"by": "ops", "reason": "enough", "at": "2026-11-12T06:01:00.000Z"})
    puts = []
    real = world.dispatcher.client.put_item

    def put_item(**kwargs):
        if kwargs["TableName"] == table("SUBSCRIPTIONS_TABLE"):
            puts.append(kwargs)
        return real(**kwargs)

    world.dispatcher.client.put_item = put_item
    full(world)
    assert {item(world, key(f"t{i:02d}"))["status"] for i in range(6)} == {"stopped"}
    assert puts == [] and run_row.POINTERS not in item(world, RUN_KEY)[run_row.REQUESTS]
