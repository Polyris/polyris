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
from polyris.coordination import run_name, run_row, spec_store, task_row, task_spec

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
                            run_task_arn=RUN_TASK, clock=clock.now, sleep=lambda _: None)
    return SimpleNamespace(client=client, ddb=ddb, sfn=sfn, dispatcher=dispatcher, clock=clock)


def start(world, specs: List[Dict[str, Any]], **inputs: Any) -> None:
    version = task_spec.build_version("orders", specs)
    spec_store.write(world.client, table("SPECS_TABLE"), version, sleep=lambda _: None)
    start_run_lambda.handler({"format_version": task_spec.FORMAT_VERSION, "spec_id": version.spec_id,
                              "task_count": version.task_count, "pipeline_name": "orders", "execution_name": EXECUTION,
                              "execution_start": START, "token": "run-token", "retry_count": 0, "redrive_count": 0,
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


def test_a_start_made_already_counts_as_done(world):
    start(world, CHAIN)
    full(world)
    update(world, key("extract"), start_failures=1, start_failed_first="2026-11-12T05:59:00.000Z",
           start_failed_last="2026-11-12T05:59:00.000Z")                 # its answer was lost
    full(world)
    assert len(world.sfn.started) == 1 and item(world, key("extract"))["start_failures"] == 1


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


def test_a_decision_refused_by_a_change_meanwhile_is_decided_again_from_new_reads(world):
    start(world, CHAIN)
    real = world.client.transact_write_items
    calls: List[int] = []

    def racing(**kwargs: Any) -> Any:
        if not calls:
            calls.append(1)
            update(world, RUN_KEY, run_version=item(world, RUN_KEY)["run_version"] + 1)   # an action lands
        return real(**kwargs)

    world.client.transact_write_items = racing
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
