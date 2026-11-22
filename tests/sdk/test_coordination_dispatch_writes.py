"""The writes of a dispatcher's pass, and their conditions as DynamoDB evaluates them (moto; ADR #137 §1, §3, §4)."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest
from boto3.dynamodb.types import TypeDeserializer

from polyris.constants import TaskStatus
from polyris.coordination import attention, dispatch, dispatch_writes, retention, run_name, run_row, start_run
from polyris.coordination import task_row, task_spec
from polyris.coordination.deadlines import RowTimes
from polyris.coordination.pauses import Interval
from polyris.coordination.times import format_utc

S = TaskStatus
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
TABLE = "tokens"
RUN_START = "2026-11-19T06:00:00.123Z"
KEY = "load-2026-11-19-run-2026-11-19-abcd1234"
_deserializer = TypeDeserializer()


def run(**changes: Any) -> dispatch.RunView:
    base = dict(key="_run_orders-run-2026-11-19-abcd1234", execution="orders-run-2026-11-19-abcd1234",
                status=run_row.ACTIVE, version=3, run_start=RUN_START, token="token-2", token_order=2, task_count=1)
    return dispatch.RunView(**{**base, **changes})


def row(status: str = S.WAITING.value, *, generation: int = 1, due=None, reasons=()) -> dispatch.RowView:
    return dispatch.RowView(task_id="load", key=KEY, status=status, generation=generation, trigger_rule="all_success",
                            dependencies=(), times=RowTimes(status=status, waiting_since=NOW - timedelta(hours=1)),
                            due=due, reasons=tuple(reasons))


@pytest.fixture
def ddb():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName=TABLE, KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
        yield client


def put(ddb, item: Dict[str, Any]) -> None:
    from boto3.dynamodb.types import TypeSerializer

    ddb.put_item(TableName=TABLE, Item={k: TypeSerializer().serialize(v) for k, v in item.items()})


def get(ddb, key: str) -> Dict[str, Any]:
    item = ddb.get_item(TableName=TABLE, Key={"execution_name": {"S": key}}, ConsistentRead=True).get("Item")
    return {k: _deserializer.deserialize(v) for k, v in (item or {}).items()}


def seed(ddb, *, status: str = S.WAITING.value, generation: int = 1, version: int = 3) -> None:
    put(ddb, {"execution_name": KEY, "status": status, "generation": generation, "reasons": ["waiting"]})
    put(ddb, {"execution_name": run().key, "status": "active", "run_version": version, "token_order": 2})


def send(ddb, items) -> bool:
    try:
        if len(items) == 1:
            ddb.update_item(**items[0]["Update"])
        else:
            ddb.transact_write_items(TransactItems=items)
        return True
    except (ddb.exceptions.ConditionalCheckFailedException, ddb.exceptions.TransactionCanceledException):
        return False


# ── task rows ───────────────────────────────────────────────────────────────

def ready() -> dispatch.Judgment:
    return dispatch.Judgment("load", "ready", {task_row.STATUS_SINCE: format_utc(NOW)}, (task_row.REASONS,),
                             due=NOW + attention.FIRST_DUE, start=True)


def test_a_decision_writes_the_row_and_records_its_run_task_name(ddb):
    seed(ddb)
    items = dispatch_writes.task_write(row(reasons=["waiting"]), ready(), run(), table=TABLE, now=NOW)
    assert [next(iter(i)) for i in items] == ["Update", "ConditionCheck"]
    assert send(ddb, items)
    written = get(ddb, KEY)
    assert (written["status"], written["generation"]) == ("deps_ready", 1) and "reasons" not in written
    assert written["task_run_id"] == run_name.run_task_name(KEY, RUN_START, 1)
    assert (written["attention"], written["attention_at"]) == (attention.attention(attention.WAITING, KEY),
                                                               format_utc(NOW + attention.FIRST_DUE))
    assert written["ttl"] == retention.ttl(NOW, retention.HISTORY)


@pytest.mark.parametrize("stored", [{"version": 4}, {"status": S.SKIPPED.value}, {"generation": 2}])
def test_a_decision_on_a_read_that_is_no_longer_true_writes_nothing(ddb, stored):
    """A Pause, a Stop or an action raised run_version, or another writer moved the row (§1, Rule 47)."""
    seed(ddb, **stored)
    assert not send(ddb, dispatch_writes.task_write(row(), ready(), run(), table=TABLE, now=NOW))
    assert get(ddb, KEY)["status"] == stored.get("status", "waiting")


def test_a_stop_raises_the_generation_to_fence_a_running_run_task(ddb):
    seed(ddb, status=S.RUNNING.value)
    judgment = dispatch.stop(row(S.RUNNING.value), now=NOW, cause="stopped by ops")
    assert send(ddb, dispatch_writes.task_write(row(S.RUNNING.value), judgment, run(), table=TABLE, now=NOW))
    written = get(ddb, KEY)
    assert (written["status"], written["generation"], written["error"]) == ("stopped", 2, "stopped by ops")
    assert "attention" not in written


def test_a_row_whose_run_row_is_missing_is_stopped_without_a_version_to_check(ddb):
    seed(ddb, status=S.RUNNING.value)
    judgment = dispatch.stop(row(S.RUNNING.value), now=NOW, cause="its run row is missing")
    items = dispatch_writes.task_write(row(S.RUNNING.value), judgment, None, table=TABLE, now=NOW)
    assert len(items) == 1 and send(ddb, items) and get(ddb, KEY)["status"] == "stopped"
    with pytest.raises(ValueError, match="only within its run"):
        dispatch_writes.task_write(row(), ready(), None, table=TABLE, now=NOW)


def test_a_failed_row_enters_the_bell_when_it_failed(ddb):
    seed(ddb)
    judgment = dispatch.Judgment("load", "fail_wait", {task_row.ERROR: "the row of its upstream x is missing"},
                                 cause="the row of its upstream x is missing")
    assert send(ddb, dispatch_writes.task_write(row(), judgment, run(), table=TABLE, now=NOW))
    written = get(ddb, KEY)
    assert (written["attention"], written["attention_at"]) == (attention.attention(attention.FAILED, KEY),
                                                               format_utc(NOW))


def test_a_wait_writes_alone_without_the_run_rows_check(ddb):
    seed(ddb, version=9)                                     # an action came meanwhile: a wait does not care
    judgment = dispatch.Judgment("load", fields={task_row.REASONS: ["lake/prices: no fact"]},
                                 due=NOW + timedelta(days=7))
    items = dispatch_writes.task_write(row(), judgment, run(), table=TABLE, now=NOW)
    assert len(items) == 1 and send(ddb, items)
    assert get(ddb, KEY)["reasons"] == ["lake/prices: no fact"]


@pytest.mark.parametrize("due,checked", [(None, True), (NOW + timedelta(days=9), True), (NOW + timedelta(days=1), False)])
def test_a_pause_that_removes_or_postpones_a_due_time_checks_the_run_version(due, checked):
    paused = run(pauses=(Interval(NOW - timedelta(minutes=5)),))
    stored = row(due=NOW + timedelta(days=2))
    items = dispatch_writes.task_write(stored, dispatch.Judgment("load", due=due), paused, table=TABLE, now=NOW)
    assert (len(items) == 2) is checked


def test_nothing_changed_writes_nothing():
    stored = row(due=NOW + timedelta(days=2))
    assert dispatch_writes.task_write(stored, dispatch.Judgment("load", due=NOW + timedelta(days=2)), run(),
                                      table=TABLE, now=NOW) == []
    with pytest.raises(ValueError, match="cannot write"):
        dispatch_writes.task_write(stored, dispatch.Judgment("other"), run(), table=TABLE, now=NOW)


def test_a_decision_deadline_is_a_decision_entry(ddb):
    seed(ddb, status=S.WAITING_DECISION.value)
    judgment = dispatch.Judgment("load", due=NOW + timedelta(hours=5))
    assert send(ddb, dispatch_writes.task_write(row(S.WAITING_DECISION.value), judgment, run(), table=TABLE, now=NOW))
    assert get(ddb, KEY)["attention"] == attention.attention(attention.DECISION, KEY)


def test_run_task_starts_under_the_name_its_row_records():
    request = dispatch_writes.start_request(row(S.DEPS_READY.value), run(), state_machine="arn:run-task", input_text="{}")
    assert request == {"stateMachineArn": "arn:run-task", "name": run_name.run_task_name(KEY, RUN_START, 1),
                       "input": "{}"}


def _spec(**fields: Any) -> task_spec.TaskSpec:
    text = task_spec.canonical_json({"task_id": "load", "task_type": "sfn", "dependencies": [],
                                     "trigger_rule": "all_success", **fields})
    return task_spec.parse(text, expected_hash=task_spec.text_hash(text))


def test_a_rows_pointers_are_those_start_run_wrote():
    spec = _spec(wait_for=[{"asset_name": "lake/prices"}])
    call = start_run.call_from({"format_version": task_spec.FORMAT_VERSION, "spec_id": "orders#" + "f" * 64,
                                "task_count": 1, "pipeline_name": "orders", "execution_name": "orders-run-1",
                                "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:orders:orders-run-1", "execution_start": RUN_START, "token": "t", "retry_count": 0, "date": "2026-11-19"})
    [item] = start_run.task_items(call, {"load": spec}, now=NOW)
    assert dispatch_writes.pointers(item) == [{k: p[k] for k in (start_run.POINTER_KEY, start_run.POINTER_SUBSCRIBER)}
                                              for p in start_run.pointer_items(call, {"load": spec}, now=NOW)]
    assert dispatch_writes.pointers({**item, task_row.READINESS: task_spec.readiness_text(_spec())}) == []


# ── the run row ─────────────────────────────────────────────────────────────

def test_a_successful_run_is_answered_with_its_outcome_and_a_failed_one_with_the_cause():
    end = dispatch.run_outcome(run(), {"load": S.SUCCESS.value})
    call, arguments = dispatch_writes.answer(run(), end)
    assert call == "send_task_success" and json.loads(arguments["output"])["outcome"] == "success"
    failed = dispatch.run_outcome(run(), {"load": S.FAILED.value})
    assert dispatch_writes.answer(run(), failed) == ("send_task_failure", {
        "taskToken": "token-2", "error": run_row.FAILED_RUN_ERROR, "cause": "1 tasks failed: load"})
    with pytest.raises(ValueError, match="finished run"):
        dispatch_writes.answer(run(), dispatch.run_outcome(run(), {"load": S.RUNNING.value}))
    with pytest.raises(ValueError, match="finished run"):
        dispatch_writes.answer(run(token=None), end)


def test_the_answers_mark_holds_only_for_the_token_still_current(ddb):
    seed(ddb)
    assert send(ddb, [{"Update": dispatch_writes.answered(run(), table=TABLE, now=NOW)}])
    marked = get(ddb, run().key)
    assert (marked["answered_order"], marked["run_version"], marked["finished_at"]) == (2, 4, format_utc(NOW))
    assert not send(ddb, [{"Update": dispatch_writes.answered(run(), table=TABLE, now=NOW)}])   # marked once
    put(ddb, {"execution_name": run().key, "status": "active", "run_version": 4, "token_order": 3})   # a newer token
    assert not send(ddb, [{"Update": dispatch_writes.answered(run(version=4), table=TABLE, now=NOW)}])
    with pytest.raises(ValueError, match="no answer to mark"):
        dispatch_writes.answered(run(token_order=None), table=TABLE, now=NOW)


def test_the_answers_mark_holds_after_an_action_raised_run_version(ddb):
    """D120: a Restart between the pass's read and its answer raised run_version; the token was answered all the
    same, and the mark records it, raising run_version on from where the action left it."""
    seed(ddb)
    put(ddb, {**get(ddb, run().key), "run_version": 7})                      # the action, after the read of 3
    assert send(ddb, [{"Update": dispatch_writes.answered(run(), table=TABLE, now=NOW)}])
    assert (get(ddb, run().key)["answered_order"], get(ddb, run().key)["run_version"]) == (2, 8)


def test_a_summary_never_replaces_one_from_a_later_read(ddb):
    seed(ddb)
    end = dispatch.run_outcome(run(), {"load": S.SUCCESS.value})
    assert send(ddb, [{"Update": dispatch_writes.summary(run(), end, table=TABLE, read_at=NOW)}])
    earlier = dispatch.run_outcome(run(), {"load": S.RUNNING.value})
    assert not send(ddb, [{"Update": dispatch_writes.summary(run(), earlier, table=TABLE,
                                                                 read_at=NOW - timedelta(seconds=1))}])
    written = get(ddb, run().key)
    assert written["summary"] == {"success": 1} and written["run_version"] == 3


def test_a_finished_runs_summary_replaces_one_a_clock_ahead_wrote_unless_the_run_changed(ddb):
    seed(ddb)
    unfinished = dispatch.run_outcome(run(), {"load": S.RUNNING.value})
    ahead = NOW + timedelta(seconds=2)                    # another Lambda's clock runs ahead
    assert send(ddb, [{"Update": dispatch_writes.summary(run(), unfinished, table=TABLE, read_at=ahead)}])
    finished = dispatch.run_outcome(run(), {"load": S.SUCCESS.value})
    assert send(ddb, [{"Update": dispatch_writes.summary(run(), finished, table=TABLE, read_at=NOW)}])
    written = get(ddb, run().key)
    assert written["summary"] == {"success": 1} and written["run_version"] == 3    # the last count stays
    restarted = dispatch.run_outcome(run(version=2), {"load": S.FAILED.value})       # read before a restart
    assert not send(ddb, [{"Update": dispatch_writes.summary(run(version=2), restarted, table=TABLE,
                                                                 read_at=ahead + timedelta(seconds=1))}])


def test_a_pass_that_left_rows_makes_its_run_row_due_at_once(ddb):
    seed(ddb)
    assert send(ddb, [{"Update": dispatch_writes.due_now(run(), table=TABLE, now=NOW, left=True)}])
    written = get(ddb, run().key)
    assert (written["attention_at"], written["left_at"], written["run_version"]) == (format_utc(NOW), format_utc(NOW), 3)
    assert send(ddb, [{"Update": dispatch_writes.due_now(run(), table=TABLE, now=NOW, left=False)}])
    ddb.delete_item(TableName=TABLE, Key={"execution_name": {"S": run().key}})
    assert not send(ddb, [{"Update": dispatch_writes.due_now(run(), table=TABLE, now=NOW, left=False)}])
    assert get(ddb, run().key) == {}                           # never a row of the key alone


def test_the_run_rows_due_time_goes_only_on_the_version_the_pass_read(ddb):
    seed(ddb)
    send(ddb, [{"Update": dispatch_writes.due_now(run(), table=TABLE, now=NOW, left=True)}])
    assert not send(ddb, [{"Update": dispatch_writes.close(run(version=2), table=TABLE, now=NOW)}])
    assert send(ddb, [{"Update": dispatch_writes.close(run(), table=TABLE, now=NOW)}])
    assert not {"attention", "attention_at", "left_at"} & set(get(ddb, run().key))


def test_the_stop_done_raises_the_run_version_on_the_one_read(ddb):
    seed(ddb)
    assert not send(ddb, [{"Update": dispatch_writes.stop_done(run(version=2), table=TABLE, now=NOW)}])
    assert send(ddb, [{"Update": dispatch_writes.stop_done(run(), table=TABLE, now=NOW)}])
    written = get(ddb, run().key)
    assert (written["stop_done"], written["run_version"]) == (format_utc(NOW), 4)


# ── ADR #137 §4, §5: the engine's stop, the sweeper's move, the history TTL ──

def test_the_engines_stop_mark_names_the_engine_and_raises_the_version_only_without_a_mark(ddb):
    seed(ddb)
    cause = "its execution ended ABORTED before the run's answer"
    assert not send(ddb, [{"Update": dispatch_writes.engine_stop(run(version=2), table=TABLE, cause=cause, now=NOW)}])
    assert send(ddb, [{"Update": dispatch_writes.engine_stop(run(), table=TABLE, cause=cause, now=NOW)}])
    written = get(ddb, run().key)
    assert written["stop"] == {"by": run_row.ENGINE, "reason": cause, "at": format_utc(NOW)}
    assert written["run_version"] == 4
    # A Stop of the run that came first keeps who asked and why.
    put(ddb, {**written, "stop": {"by": "ops", "reason": "wrong date", "at": format_utc(NOW)}})
    assert not send(ddb, [{"Update": dispatch_writes.engine_stop(run(version=4), table=TABLE, cause=cause, now=NOW)}])
    assert get(ddb, run().key)["stop"]["by"] == "ops"


def test_the_sweepers_move_backs_off_clears_left_at_and_keeps_the_version(ddb):
    seed(ddb)
    due = format_utc(NOW - timedelta(minutes=1))
    put(ddb, {**get(ddb, run().key), "attention_at": due, "left_at": due, "checks": 1})
    assert send(ddb, [{"Update": dispatch_writes.checked(run().key, table=TABLE, due_read=due, version_read=3,
                                                         checks=1, now=NOW)}])
    written = get(ddb, run().key)
    assert (written["attention_at"], written["checks"], written["run_version"]) == (
        format_utc(NOW + timedelta(minutes=20)), 2, 3)
    assert "left_at" not in written and written["ttl"] == retention.ttl(NOW, retention.HISTORY)


@pytest.mark.parametrize("due_read,version_read", [("2026-11-19T11:00:00.000Z", 3), (None, 2)])
def test_the_sweepers_move_never_postpones_a_check_set_meanwhile_or_after_a_raise(ddb, due_read, version_read):
    """A request, a pass or an action that set the due time, or a write that raised run_version, keeps it (§3)."""
    seed(ddb)
    due = format_utc(NOW - timedelta(minutes=1))
    put(ddb, {**get(ddb, run().key), "attention_at": due})
    assert not send(ddb, [{"Update": dispatch_writes.checked(run().key, table=TABLE, due_read=due_read or due,
                                                             version_read=version_read, checks=0, now=NOW)}])
    assert get(ddb, run().key)["attention_at"] == due


def test_every_write_of_the_run_row_renews_its_history_ttl_and_never_makes_a_row_of_the_key_alone(ddb):
    seed(ddb)
    later = NOW + timedelta(days=30)
    end = dispatch.RunEnd({"success": 1}, None)
    writes = [dispatch_writes.summary(run(), end, table=TABLE, read_at=later),
              dispatch_writes.due_now(run(), table=TABLE, now=later, left=False)]
    for update in writes:
        assert send(ddb, [{"Update": update}])
        assert get(ddb, run().key)["ttl"] == retention.ttl(later, retention.HISTORY)
    ddb.delete_item(TableName=TABLE, Key={"execution_name": {"S": run().key}})
    for update in writes:
        assert not send(ddb, [{"Update": update}])
    assert get(ddb, run().key) == {}


def test_a_row_a_12_run_left_without_a_generation_is_stopped_on_its_absence(ddb):
    """Its stop is conditional on the status read and on no generation, and gives it the first (*Migration*)."""
    put(ddb, {"execution_name": KEY, "status": "waiting"})
    item = {"execution_name": KEY, "task_name": "load", "status": "waiting"}
    legacy = dispatch.stop_view(item)
    judgment = dispatch.stop(legacy, now=NOW, cause="its run row is missing")
    assert send(ddb, dispatch_writes.task_write(legacy, judgment, None, table=TABLE, now=NOW))
    stopped = get(ddb, KEY)
    assert (stopped["status"], stopped["generation"]) == ("stopped", 1)
    assert not send(ddb, dispatch_writes.task_write(legacy, judgment, None, table=TABLE, now=NOW))   # raised now


# ── the row as a write leaves it (dispatch_writes.applied) ──────────────────

def _cases():
    due = NOW + timedelta(minutes=30)
    start_failed = dispatch.start_failed(row(S.DEPS_READY.value), run(), now=NOW, error="ThrottlingException: slow",
                                         kind=dispatch.TRANSIENT)
    return {
        "ready": (S.WAITING.value, None, ready()),
        "stop": (S.RUNNING.value, None, dispatch.stop(row(S.RUNNING.value), now=NOW, cause="stopped by ops: wrong")),
        "a start that failed": (S.DEPS_READY.value, None, start_failed),
        "a skip by its rule": (S.WAITING.value, None, dispatch.Judgment("load", "skip_by_rule", {
            task_row.STATUS_SINCE: format_utc(NOW), task_row.FINISHED_AT: format_utc(NOW)})),
        "a wait that loses its due time": (S.WAITING.value, due, dispatch.Judgment("load", fields={
            task_row.REASONS: ["waiting for its upstreams"]}, due=None)),
    }


@pytest.mark.parametrize("case", sorted(_cases()))
def test_a_pass_reads_a_row_after_its_write_as_dynamodb_holds_it(ddb, case):
    """The rest of a pass reads the row from ``applied``, not from DynamoDB: they must agree (the review of TR-4)."""
    status, due, judgment = _cases()[case]
    before: Dict[str, Any] = {"execution_name": KEY, "status": status, "generation": 1, "reasons": ["waiting"]}
    if due is not None:
        before.update({attention.KEY: attention.attention(attention.WAITING, KEY), attention.DUE: format_utc(due)})
    put(ddb, before)
    put(ddb, {"execution_name": run().key, "status": "active", "run_version": 3, "token_order": 2})
    view = dispatch.RowView(**{**row(status).__dict__, "due": due})
    assert send(ddb, dispatch_writes.task_write(view, judgment, run(), table=TABLE, now=NOW))
    assert get(ddb, KEY) == dispatch_writes.applied(before, view, judgment, run(), now=NOW)


def test_a_row_is_made_ready_only_within_its_run_in_what_a_pass_reads_too():
    with pytest.raises(ValueError, match="made ready only within its run"):
        dispatch_writes.applied({"execution_name": KEY}, row(), ready(), None, now=NOW)
