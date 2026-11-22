"""An operator's action on a task, built from the console's reads (ADR #137 §3; coordination.actions)."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from polyris.constants import TaskStatus
from polyris.coordination import actions, attention, dispatch, records, retention, run_name, run_row, task_row, task_spec
from polyris.coordination.times import format_utc

S = TaskStatus
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
TOKENS, FACTS = "tokens", "asset-events"
RUN = "orders-run-2026-11-21-abcd1234"
KEY = "load-2026-11-21--2026-11-21-abcd1234"
START = "2026-11-21T06:00:00.123Z"
OUTPUT = "output#orders#load#2026-11-21"
_ser, _de = TypeSerializer(), TypeDeserializer()


@pytest.fixture
def ddb():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName=TOKENS, KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
        client.create_table(TableName=FACTS, KeySchema=[{"AttributeName": "asset_name", "KeyType": "HASH"},
                                                        {"AttributeName": "event_time", "KeyType": "RANGE"}],
                            AttributeDefinitions=[{"AttributeName": "asset_name", "AttributeType": "S"},
                                                  {"AttributeName": "event_time", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
        yield client


def task(status: str, *, generation: int = 2, error: Optional[str] = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        task_row.TABLE_KEY: KEY, task_row.TASK_NAME: "load", task_row.STATUS: status,
        task_row.GENERATION: generation, "pipeline_name": "orders", task_row.BACKFILL_ID: "",
        task_row.READINESS: task_spec.canonical_json({"trigger_rule": "all_success", "dependencies": ["extract"]})}
    if error:
        item[task_row.ERROR] = error
    return item


def run(*, version: int = 5, due: Optional[str] = None) -> Dict[str, Any]:
    item = {run_row.TABLE_KEY: run_row.key(RUN), run_row.EXECUTION: RUN, run_row.STATUS: run_row.ACTIVE,
            run_row.RUN_VERSION: version, run_row.RUN_START: START, run_row.RUN_DATE: "2026-11-21",
            run_row.PIPELINE: "orders"}
    if due:
        item[attention.KEY], item[attention.DUE] = attention.attention(attention.WAITING, run_row.key(RUN)), due
    return item


def put(ddb, item: Dict[str, Any]) -> None:
    ddb.put_item(TableName=TOKENS, Item={k: _ser.serialize(v) for k, v in item.items()})


def get(ddb, key: str, table: str = TOKENS) -> Dict[str, Any]:
    item = ddb.get_item(TableName=table, Key={"execution_name": {"S": key}}, ConsistentRead=True).get("Item")
    return {k: _de.deserialize(v) for k, v in (item or {}).items()}


def build(action: str, row: Dict[str, Any], *, output: Optional[Dict[str, Any]] = None, outlets=(), reason="why",
          run_item: Optional[Dict[str, Any]] = None) -> actions.ActionWrite:
    return actions.task_action(action=action, task=row, run=run_item or run(), output=output, outlets=outlets,
                               cascade="auto", operator="ops@example.com", reason=reason, now=NOW,
                               tokens_table=TOKENS, asset_events_table=FACTS, fact_ttl_days=365)


def send(ddb, write: actions.ActionWrite) -> bool:
    try:
        ddb.transact_write_items(TransactItems=write.items)
        return True
    except ddb.exceptions.TransactionCanceledException:
        return False


def seed(ddb, row: Dict[str, Any], output: Optional[Dict[str, Any]] = None, run_item=None) -> None:
    put(ddb, row)
    put(ddb, run_item or run())
    if output is not None:
        put(ddb, output)


@pytest.mark.parametrize("action, status, skip_origin", [("skip", S.SKIPPED.value, "manual"),
                                                         ("mark_success", S.SUCCESS.value, None),
                                                         ("fail", S.FAILED.value, None),
                                                         ("stop", S.STOPPED.value, None)])
def test_an_action_writes_its_row_its_run_row_and_its_marker_in_one_transaction(ddb, action, status, skip_origin):
    row = task(S.WAITING_DECISION.value, error="the work failed")
    seed(ddb, row)
    assert send(ddb, build(action, row))
    written = get(ddb, KEY)
    assert (written["status"], written["generation"], written.get("skip_origin")) == (status, 3, skip_origin)
    assert written["finished_at"] == format_utc(NOW)
    assert ("attention" in written) is (status == S.FAILED.value)                  # the bell shows a failure
    assert written.get("error") == ("why" if action == "fail" else "the work failed")
    the_run = get(ddb, run_row.key(RUN))
    assert the_run[run_row.RUN_VERSION] == 6 and the_run[attention.DUE] == format_utc(NOW)   # due at once
    marker = json.loads(get(ddb, OUTPUT)["result"])
    assert marker == {"_manually_resolved": True, "_resolution": action, "_reason": "why",
                      "_operator": "ops@example.com", "_pipeline_execution": RUN}
    assert int(get(ddb, OUTPUT)["ttl"]) == retention.ttl(NOW, retention.OUTPUT)


def test_a_run_rows_due_time_and_a_raised_run_version_are_kept(ddb):
    """An action makes the run row due only when it has none, and raises run_version whatever it is (§3)."""
    row = task(S.FAILED.value)
    seed(ddb, row, run_item=run(version=9, due="2026-11-22T13:00:00.000Z"))
    assert send(ddb, build("skip", row, run_item=run(version=4)))        # read an older version: no matter
    the_run = get(ddb, run_row.key(RUN))
    assert the_run[run_row.RUN_VERSION] == 10 and the_run[attention.DUE] == "2026-11-22T13:00:00.000Z"


def test_an_action_on_a_row_that_moved_since_its_read_is_refused(ddb):
    """The row's write is conditional on the status and the generation read (invariant 5)."""
    seed(ddb, task(S.RUNNING.value, generation=3))
    assert not send(ddb, build("stop", task(S.RUNNING.value, generation=2)))
    assert get(ddb, KEY)["status"] == S.RUNNING.value and get(ddb, run_row.key(RUN))[run_row.RUN_VERSION] == 5


def test_real_output_keeps_the_marker_out_and_says_so(ddb):
    """Principle #31: an earlier run's output of the date stays; the console warns instead of writing over it."""
    row, output = task(S.FAILED.value), {"execution_name": OUTPUT, "result": json.dumps({"rows": 3})}
    seed(ddb, row, output)
    write = build("mark_success", row, output=output)
    assert write.marker_blocked and send(ddb, write)
    assert json.loads(get(ddb, OUTPUT)["result"]) == {"rows": 3}


@pytest.mark.parametrize("result", ["ok", 7])
def test_a_1_2_string_result_or_any_other_shape_is_real_output(result):
    assert build("skip", task(S.FAILED.value), output={"execution_name": OUTPUT, "result": result}).marker_blocked


def test_an_earlier_marker_is_replaced_and_a_push_since_the_read_cancels_the_action(ddb):
    row = task(S.FAILED.value)
    earlier = {"execution_name": OUTPUT, "result": json.dumps({"_manually_resolved": True, "_resolution": "fail"})}
    seed(ddb, row, earlier)
    put(ddb, {"execution_name": OUTPUT, "result": json.dumps({"pushed": 1})})        # xcom.push after the read
    assert not send(ddb, build("skip", row, output=earlier))
    assert get(ddb, KEY)["status"] == S.FAILED.value
    put(ddb, earlier)
    assert send(ddb, build("skip", row, output=earlier))
    assert json.loads(get(ddb, OUTPUT)["result"])["_resolution"] == "skip"


def test_mark_successful_records_a_fact_per_outlet_in_its_transaction(ddb):
    row = task(S.FAILED.value)
    seed(ddb, row)
    outlets = [{"name": "ns/orders", "uri": "s3://lake/orders/"}, {"name": "ns/weekly", "granularity": "weekly"}]
    assert send(ddb, build("mark_success", row, outlets=outlets))
    facts = {i["asset_name"]["S"]: {k: _de.deserialize(v) for k, v in i.items()}
             for i in ddb.scan(TableName=FACTS)["Items"]}
    expected = records.asset_fact_item(asset_name="ns/orders", partition_key="2026-11-21", granularity="daily",
                                       cascade="auto", source_dag="orders", source_task="load",
                                       uri="s3://lake/orders/", now=NOW, ttl_days=365)
    assert facts["ns/orders"] == expected and facts["ns/weekly"]["granularity"] == "weekly"
    assert not build("skip", task(S.FAILED.value), outlets=outlets).items[3:]          # only Mark Successful
    assert len(build("mark_success", task(S.FAILED.value), outlets=[{"uri": "s3://x/"}]).items) == 3   # no name


def test_stop_names_the_run_task_of_the_generation_it_read_and_the_events_follow(ddb):
    write = build("stop", task(S.RUNNING.value, generation=2, error="earlier"))
    assert write.run_task_to_stop == run_name.run_task_name(KEY, START, 2)
    assert build("stop", task(S.WAITING.value)).run_task_to_stop is None             # nothing runs yet
    finished, decision = write.events
    assert (finished["event_type"], finished["status"]) == ("TASK_FINISHED", "failed")
    assert (decision["event_type"], decision["decision"], decision["operator"]) == ("MANUAL_DECISION", "stop",
                                                                                     "ops@example.com")
    assert finished["task_run_id"] == decision["task_run_id"] == run_name.run_task_name(KEY, START, 2)
    assert [e["event_type"] for e in build("fail", task(S.RUNNING.value, error="x")).events] == ["MANUAL_DECISION"]


@pytest.mark.parametrize("action, status", [("skip", S.SUCCESS.value), ("mark_success", S.SKIPPED.value),
                                            ("fail", S.FAILED.value), ("stop", S.STOPPED.value)])
def test_an_action_the_table_does_not_allow_from_the_status_is_refused(action, status):
    with pytest.raises(actions.ActionRefused, match=f"takes no {action}"):
        build(action, task(status))


def test_a_decision_without_a_reason_says_how_it_was_taken():
    [decision] = build("skip", task(S.FAILED.value), reason="").events
    assert decision["reason"] == "Manual skip via UI"


def test_only_the_light_actions_are_built_here():
    with pytest.raises(ValueError, match="no such action"):
        build("restart", task(S.FAILED.value))


def test_a_task_of_a_run_from_before_1_3_takes_no_action():
    legacy = {k: v for k, v in task(S.FAILED.value).items() if k != task_row.READINESS}
    with pytest.raises(actions.ActionRefused, match="before 1.3"):
        build("skip", legacy)


def test_the_marker_fields_are_those_xcom_reads():
    """Principle #28: xcom.py, copied alone into a Lambda's package, spells the field out."""
    from polyris import xcom
    assert actions.MARKER_RESOLVED == xcom._MANUAL_RESOLVED_FIELD


def test_a_mark_successful_transaction_fits_dynamodb_with_the_most_outlets():
    """The row, the run row, the marker and a fact per outlet: at most 100 items (§8)."""
    outlets = [{"name": f"ns/a{i}"} for i in range(task_spec.MAX_OUTLETS)]
    assert len(build("mark_success", task(S.FAILED.value), outlets=outlets).items) <= 100


# ── Restart, Restart with dependants, and the requests a pass carries out (§3, §5) ─────────────────────────────

STARTED = "2026-11-21T06:00:00.123Z"


def active_run(*, version: int = 5, **fields: Any) -> Dict[str, Any]:
    return {**run(version=version), run_row.STARTED_AT: STARTED, **fields}


def finished(status: str = S.FAILED.value, *, readiness: Optional[Dict[str, Any]] = None, **fields: Any):
    item = task(status, error="the work failed")
    if readiness is not None:
        item[task_row.READINESS] = task_spec.canonical_json(readiness)
    item.update({task_row.FINISHED_AT: "2026-11-21T07:00:00.000Z", task_row.TASK_RUN_ID: "old-run-task",
                 task_row.RUNNING_AT: "2026-11-21T06:30:00.000Z", task_row.ATTEMPT: 2,
                 task_row.RUN_TASK_ARN: "arn:old", task_row.STATUS_SINCE: "2026-11-21T07:00:00.000Z",
                 attention.KEY: attention.attention(attention.FAILED, KEY), attention.DUE: "2026-11-21T07:00:00.000Z",
                 **fields})
    return item


def restart_of(row: Dict[str, Any], *, run_item: Optional[Dict[str, Any]] = None, with_dependants: bool = False,
               now: datetime = NOW) -> actions.RestartWrite:
    return actions.restart(task=row, run=run_item or active_run(), with_dependants=with_dependants,
                           operator="ops@example.com", reason="rerun", now=now, tokens_table=TOKENS)


def test_a_restart_puts_the_row_back_to_waiting_and_requests_the_renewal(ddb):
    row = finished()
    seed(ddb, row, run_item=active_run())
    write = restart_of(row)
    assert send(ddb, write)
    written = get(ddb, KEY)
    assert (written["status"], written["generation"], written["attempt"]) == ("waiting", 3, 2)
    assert not set(task_row.RUN_FIELDS) & set(written), set(task_row.RUN_FIELDS) & set(written)
    assert written[task_row.WAITING_SINCE] == written[task_row.STATUS_SINCE] == format_utc(NOW)
    assert attention.KEY not in written                       # it waits for extract: no limit of its own
    the_run = get(ddb, run_row.key(RUN))
    assert the_run[run_row.RUN_VERSION] == 6 and the_run[attention.DUE] == format_utc(NOW)
    assert int(the_run[run_row.TTL]) == retention.ttl(NOW, retention.HISTORY)
    assert the_run[run_row.REQUESTS] == {run_row.RENEW_TTL: {"by": "ops@example.com", "at": format_utc(NOW)}}
    [event] = write.events
    assert (event["decision"], event["task_run_id"]) == ("restart", run_name.run_task_name(KEY, START, 2))


def test_a_restart_counts_its_limits_anew_and_its_pointers_are_as_start_run_writes_them():
    readiness = {"trigger_rule": "all_success", "dependencies": [], "orchestration_timeout": 3600,
                 "wait_for": [{"asset_name": "ns/orders"}]}
    row = finished(readiness=readiness)
    update = restart_of(row).items[0]["Update"]
    assert _de.deserialize(update["ExpressionAttributeValues"][":due"]) == format_utc(NOW + timedelta(hours=1))
    assert actions.pointer_items(row, run_key=run_row.key(RUN), now=NOW) == [
        {"dependency_key": "asset:ns/orders", "subscriber": KEY, "run": run_row.key(RUN),
         "ttl": retention.ttl(NOW, retention.HISTORY)}]


def test_a_restart_with_dependants_requests_their_reset_and_a_second_waits_for_it(ddb):
    row = finished()
    seed(ddb, row, run_item=active_run())
    assert send(ddb, restart_of(row, with_dependants=True))
    requests = get(ddb, run_row.key(RUN))[run_row.REQUESTS]
    assert requests[run_row.RESET_DEPENDANTS] == {"by": "ops@example.com", "at": format_utc(NOW), "task": "load"}
    again = get(ddb, KEY)
    again[task_row.STATUS] = S.FAILED.value                    # as if it failed again
    with pytest.raises(actions.ActionRefused, match="with dependants is in progress"):
        restart_of(again, run_item=get(ddb, run_row.key(RUN)), with_dependants=True)
    put(ddb, {**get(ddb, KEY), task_row.STATUS: S.FAILED.value})
    # Built from a read before the first request: its condition refuses it.
    assert not send(ddb, restart_of({**again, task_row.GENERATION: 3}, with_dependants=True))
    # A plain Restart beside the reset renews the request of the renewal by kind.
    assert send(ddb, restart_of({**again, task_row.GENERATION: 3}, run_item=get(ddb, run_row.key(RUN))))
    assert set(get(ddb, run_row.key(RUN))[run_row.REQUESTS]) == {run_row.RENEW_TTL, run_row.RESET_DEPENDANTS}


@pytest.mark.parametrize("fields, refused", [
    ({run_row.STOP: {"by": "ops"}}, "a Stop of its run is in progress"),
    ({run_row.STOP: {"by": "ops"}, run_row.STOP_DONE: "2026-11-22T11:00:00.000Z"}, None),
    ({run_row.STATUS: run_row.CREATING}, "not active"),
])
def test_a_restart_waits_for_a_stop_of_its_run_to_be_done(fields, refused):
    if refused is None:
        assert restart_of(finished(), run_item=active_run(**fields)).items
    else:
        with pytest.raises(actions.ActionRefused, match=refused):
            restart_of(finished(), run_item=active_run(**fields))


def test_a_restart_is_refused_a_year_less_a_day_after_its_run_started():
    started = datetime(2026, 11, 21, 6, 0, 0, 123000, tzinfo=timezone.utc)
    assert restart_of(finished(), now=started + timedelta(days=363, hours=23)).items
    with pytest.raises(actions.ActionRefused, match="more than a year ago"):
        restart_of(finished(), now=started + timedelta(days=364, minutes=1))


def test_a_restart_takes_only_a_final_or_undecided_row():
    assert restart_of(finished(S.WAITING_DECISION.value)).items
    with pytest.raises(actions.ActionRefused, match="takes no restart"):
        restart_of(finished(S.RUNNING.value))


def _view(name: str, status: str, *, dependencies=(), origin: Optional[str] = None,
          since: str = "2026-11-22T11:00:00.000Z"):
    readiness = {"trigger_rule": "all_success", "dependencies": list(dependencies)}
    item = {task_row.TABLE_KEY: f"{name}-key", task_row.TASK_NAME: name, task_row.STATUS: status,
            task_row.GENERATION: 2, task_row.READINESS: task_spec.canonical_json(readiness),
            task_row.STATUS_SINCE: since}
    if origin:
        item[task_row.SKIP_ORIGIN] = origin
    return dispatch.row_view(item)


def test_the_reset_set_is_what_a_failure_decided_downstream_before_the_request_upstreams_first():
    late = format_utc(NOW + timedelta(seconds=1))
    rows = {
        "extract": _view("extract", S.WAITING.value),
        "load": _view("load", S.UPSTREAM_FAILED.value, dependencies=["extract"]),
        "report": _view("report", S.UPSTREAM_FAILED.value, dependencies=["load"]),
        "audit": _view("audit", S.SKIPPED.value, dependencies=["extract"], origin="rule"),
        "manual": _view("manual", S.SKIPPED.value, dependencies=["extract"], origin="manual"),
        "late": _view("late", S.UPSTREAM_FAILED.value, dependencies=["extract"], since=late),
        "stranger": _view("stranger", S.UPSTREAM_FAILED.value, dependencies=["other"]),
        "other": _view("other", S.FAILED.value),
    }
    request = run_row.request(by="ops", at=format_utc(NOW), task="extract")
    assert actions.reset_set(rows, request) == ["audit", "load", "report"]


def test_a_reset_raises_run_version_from_its_read_so_a_second_from_the_same_read_loses(ddb):
    row = task(S.UPSTREAM_FAILED.value)
    seed(ddb, row, run_item=active_run())
    view = dispatch.run_view(active_run())
    assert send(ddb, actions.ActionWrite(actions.reset(row, dispatch.row_view(row), view, now=NOW, table=TOKENS)))
    assert (get(ddb, KEY)["status"], get(ddb, KEY)["generation"]) == ("waiting", 3)
    assert get(ddb, KEY) == actions.reset_applied(row, dispatch.row_view(row), view, now=NOW)   # what the pass reads
    assert get(ddb, run_row.key(RUN))[run_row.RUN_VERSION] == 6
    put(ddb, row)                                          # the row as read, the run row moved on
    assert not send(ddb, actions.ActionWrite(actions.reset(row, dispatch.row_view(row), view, now=NOW,
                                                               table=TOKENS)))


def test_a_cleared_request_goes_by_kind_on_the_version_read(ddb):
    requests = {run_row.RENEW_TTL: {"by": "a", "at": "t"}, run_row.RESET_DEPENDANTS: {"by": "a", "at": "t"}}
    put(ddb, active_run(**{run_row.REQUESTS: requests}))
    view = dispatch.run_view(active_run())
    ddb.update_item(**actions.clear_request(view, run_row.RENEW_TTL, table=TOKENS, now=NOW))
    the_run = get(ddb, run_row.key(RUN))
    assert set(the_run[run_row.REQUESTS]) == {run_row.RESET_DEPENDANTS} and the_run[run_row.RUN_VERSION] == 6
    with pytest.raises(ddb.exceptions.ConditionalCheckFailedException):
        ddb.update_item(**actions.clear_request(view, run_row.RESET_DEPENDANTS, table=TOKENS, now=NOW))


def test_the_renewal_writes_what_lives_less_than_the_request_and_nothing_ttl_deleted(ddb):
    at = NOW - timedelta(hours=1)
    old, renewed = retention.ttl(at - timedelta(days=200), retention.HISTORY), retention.ttl(NOW, retention.HISTORY)
    rows = {"load": {**task(S.FAILED.value), task_row.TTL: old},
            "extract": {**task(S.SUCCESS.value), task_row.TABLE_KEY: "extract-key", task_row.TTL: renewed}}
    for item in rows.values():
        put(ddb, item)
    put(ddb, {"execution_name": run_row.input_key(RUN), "ttl": old})
    put(ddb, {"execution_name": "output#orders#extract#2026-11-21", "result": "{}", "ttl": old})
    view = dispatch.run_view(active_run(**{run_row.PIPELINE: "orders"}))
    writes = actions.renewals(view, rows, request=run_row.request(by="ops", at=format_utc(at)), now=NOW, table=TOKENS)
    assert [w["Key"]["execution_name"]["S"] for w in writes] == [
        KEY, run_row.input_key(RUN), "output#orders#extract#2026-11-21", OUTPUT]   # extract's row is renewed
    for write in writes:
        try:
            ddb.update_item(**write)
        except ddb.exceptions.ConditionalCheckFailedException:
            pass                                           # gone, or renewed already: nothing to do
    assert int(get(ddb, KEY)["ttl"]) == renewed and int(get(ddb, run_row.input_key(RUN))["ttl"]) == renewed
    assert int(get(ddb, "output#orders#extract#2026-11-21")["ttl"]) == retention.ttl(NOW, retention.OUTPUT)
    assert get(ddb, OUTPUT) == {}                          # load wrote none: no item of the key alone


def test_a_restart_clears_every_field_run_task_writes_on_the_row_but_the_attempt():
    """Principle #28: run_task's template writes its fields by name; a new one is cleared by a Restart or kept
    here by decision."""
    import re
    from pathlib import Path
    template = (Path(__file__).resolve().parents[2] / "sam/sfn_templates/helpers/run_task/sfn.tpl.json").read_text()
    written = set()
    for expression in re.findall(r"SET ([^\"']+)", template):
        if "task_name = :tn" in expression:
            continue                                       # output# and input#, not the row
        sets = re.sub(r"\([^)]*\)", "", expression.split(" REMOVE ")[0])     # if_not_exists(a, b) has a comma
        written |= {part.split("=")[0].strip() for part in sets.split(",")}
    kept = {"#s", "#ttl", "#att", "#due", "status_since", task_row.ATTEMPT}
    named = {"#e": task_row.ERROR}
    assert {named.get(name, name) for name in written} - kept <= set(task_row.RUN_FIELDS)


def test_a_reset_row_with_a_limit_is_due_at_it_as_the_pass_reads(ddb):
    row = task(S.UPSTREAM_FAILED.value)
    row[task_row.READINESS] = task_spec.canonical_json({"trigger_rule": "all_success", "dependencies": ["extract"],
                                                        "orchestration_timeout": 600})
    seed(ddb, row, run_item=active_run())
    view = dispatch.run_view(active_run())
    assert send(ddb, actions.ActionWrite(actions.reset(row, dispatch.row_view(row), view, now=NOW, table=TOKENS)))
    assert get(ddb, KEY) == actions.reset_applied(row, dispatch.row_view(row), view, now=NOW)
    assert get(ddb, KEY)[attention.DUE] == format_utc(NOW + timedelta(minutes=10))


# ── Stop of a run, Pause and Resume (§3, §4, §5) ───────────────────────────


def test_a_stop_of_a_run_marks_it_raises_it_makes_it_due_and_cancels_a_pending_reset(ddb):
    reset = {run_row.RESET_DEPENDANTS: {"by": "a", "at": "t", "task": "load"}, run_row.RENEW_TTL: {"by": "a", "at": "t"}}
    put(ddb, active_run(**{run_row.REQUESTS: reset}))
    ddb.update_item(**actions.stop_run(get(ddb, run_row.key(RUN)), by="ops", reason="wrong data", now=NOW,
                                       table=TOKENS))
    the_run = get(ddb, run_row.key(RUN))
    assert the_run[run_row.STOP] == {"by": "ops", "reason": "wrong data", "at": format_utc(NOW)}
    assert the_run[run_row.RUN_VERSION] == 6 and the_run[attention.DUE] == format_utc(NOW)
    assert set(the_run[run_row.REQUESTS]) == {run_row.RENEW_TTL}             # the renewal goes on
    assert actions.stop_run(the_run, by="ops", reason="again", now=NOW, table=TOKENS) is None


def test_a_stop_built_before_a_reset_was_requested_is_refused_so_it_cancels_it_when_built_again(ddb):
    read = active_run()
    put(ddb, active_run(**{run_row.REQUESTS: {run_row.RESET_DEPENDANTS: {"by": "a", "at": "t", "task": "x"}}}))
    with pytest.raises(ddb.exceptions.ConditionalCheckFailedException):
        ddb.update_item(**actions.stop_run(read, by="ops", reason="", now=NOW, table=TOKENS))
    with pytest.raises(actions.ActionRefused, match="not active"):
        actions.stop_run(active_run(**{run_row.STATUS: run_row.CREATING}), by="ops", reason="", now=NOW, table=TOKENS)


def test_a_pause_opens_an_interval_and_a_resume_closes_it_each_on_the_intervals_read(ddb):
    put(ddb, active_run())
    ddb.update_item(**actions.pause_run(get(ddb, run_row.key(RUN)), now=NOW, table=TOKENS))
    paused = get(ddb, run_row.key(RUN))
    assert dispatch.run_view(paused).paused and paused[run_row.RUN_VERSION] == 6
    assert actions.pause_run(paused, now=NOW, table=TOKENS) is None              # paused already
    later = NOW + timedelta(hours=1)
    ddb.update_item(**actions.resume_run(paused, now=later, table=TOKENS))
    resumed = get(ddb, run_row.key(RUN))
    assert not dispatch.run_view(resumed).paused and resumed[run_row.RUN_VERSION] == 7
    assert actions.resume_run(resumed, now=later, table=TOKENS) is None          # not paused
    with pytest.raises(ddb.exceptions.ConditionalCheckFailedException):         # built from the paused read
        ddb.update_item(**actions.pause_run({**paused, run_row.PAUSES: []}, now=later, table=TOKENS))


def test_a_stopped_run_takes_no_pause():
    with pytest.raises(actions.ActionRefused, match="takes no Pause"):
        actions.pause_run(active_run(**{run_row.STOP: {"by": "ops"}}), now=NOW, table=TOKENS)
