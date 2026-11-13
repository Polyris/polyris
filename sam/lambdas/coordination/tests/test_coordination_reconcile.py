"""The reconciler against moto tables built from template.yaml (ADR #130 §3)."""

import json
import re

import pytest
from coordination_testkit import (
    DATE,
    NOW,
    REPO_ROOT,
    RUN,
    SHORT,
    client_error,
    execution_name,
    fact,
    legacy_fact,
    task_row,
)

import env
import reconcile
from coordination_core import attention as att
from coordination_core import decide as dc
from failures import CoordinationTransient

LOAD = execution_name("load")
ORDERS = [{"asset_name": "ns/orders", "freshness_hours": None}]


def row(tokens, name=LOAD):
    return tokens.get_item(Key={"execution_name": name}, ConsistentRead=True).get("Item")


def warning(tokens, kind, name=LOAD):
    return row(tokens, f"_notify_warn_{kind}_{name}")


# ── noop ────────────────────────────────────────────────────────────────


def test_a_missing_row_is_a_noop(reconciler, sfn):
    result = reconciler.reconcile(LOAD, source="registration")
    assert (result["verdict"], result["action"]) == (dc.NOOP, "none")
    assert not sfn.succeeded


def test_a_row_that_left_waiting_leaves_the_attention_index(reconciler, tokens):
    tokens.put_item(Item=task_row("load", status="running", attention="waiting#a", attention_at="2026-11-12T06:10:00.000Z"))
    result = reconciler.reconcile(LOAD, source="sweeper")
    assert (result["verdict"], result["action"]) == (dc.NOOP, "attention_cleared")
    stored = row(tokens)
    assert att.KEY not in stored and att.DUE not in stored and stored["status"] == "running"


def test_a_row_held_by_pause_waiter_is_left_alone(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load", status="waiting_paused", pause_token="pause-1"))
    result = reconciler.reconcile(LOAD, source="resume")
    assert (result["verdict"], result["action"]) == (dc.NOOP, "none")
    assert not sfn.succeeded


def test_a_waiting_row_without_a_token_is_reported(reconciler, tokens, sfn):
    item = task_row("load")
    del item["wait_token"]
    tokens.put_item(Item=item)
    result = reconciler.reconcile(LOAD, source="sweeper")
    assert (result["verdict"], result["action"]) == (reconcile.ERROR, "warned")
    assert warning(tokens, "coordination")["error"].startswith("the waiting row has no wait_token")
    assert not sfn.succeeded


# ── dependencies ────────────────────────────────────────────────────────


def test_pending_dependencies_wait_without_reading_assets(reconciler, tokens, sfn, mocker):
    tokens.put_item(Item=task_row("load", deps=["extract"], wait_for=ORDERS))
    facts = mocker.patch("reconcile.FactStore")
    result = reconciler.reconcile(LOAD, source="registration")
    assert (result["verdict"], result["action"]) == (dc.WAIT, "none")
    assert result["deps"]["counts"]["pending"] == 1      # the dependency row does not exist yet
    facts.assert_not_called()
    assert not sfn.succeeded


def test_ready_signals_the_wrapper_and_deletes_only_this_runs_pointers(reconciler, tokens, subscriptions, sfn):
    tokens.put_item(Item=task_row("extract", status="success"))
    tokens.put_item(Item=task_row("load", deps=["extract"]))
    subscriptions.put_item(Item={"dependency_key": f"extract-{SHORT}", "subscriber": LOAD})
    subscriptions.put_item(Item={"dependency_key": f"extract-{SHORT}", "subscriber": execution_name("report")})

    result = reconciler.reconcile(LOAD, source="notify_dependents")

    assert (result["verdict"], result["action"]) == (dc.READY, "signalled")
    assert sfn.succeeded == [{"token": "token-load", "output": {"signal": "deps_ready", "reason": "all conditions met"}}]
    left = subscriptions.scan()["Items"]
    assert [i["subscriber"] for i in left] == [execution_name("report")]
    assert row(tokens)["status"] == "waiting"     # the wrapper writes the status, not the reconciler


@pytest.mark.parametrize("dep_status,skip_origin,signal", [
    ("failed", None, "deps_blocked"),
    ("skipped", "rule", "deps_skip"),
])
def test_unsatisfiable_rules_signal_the_outcome(reconciler, tokens, sfn, dep_status, skip_origin, signal):
    extra = {"skip_origin": skip_origin} if skip_origin else {}
    tokens.put_item(Item=task_row("extract", status=dep_status, **extra))
    tokens.put_item(Item=task_row("load", deps=["extract"]))
    reconciler.reconcile(LOAD, source="notify_dependents")
    assert sfn.succeeded[0]["output"]["signal"] == signal


def test_a_manual_skip_does_not_cascade(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("extract", status="skipped", skip_origin="manual"))
    tokens.put_item(Item=task_row("load", deps=["extract"]))
    reconciler.reconcile(LOAD, source="notify_dependents")
    assert sfn.succeeded[0]["output"]["signal"] == "deps_ready"


# ── assets ──────────────────────────────────────────────────────────────


def test_an_asset_fact_for_the_runs_date_makes_the_task_ready(reconciler, tokens, asset_events, sfn):
    asset_events.put_item(Item=fact("ns/orders", DATE))
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    result = reconciler.reconcile(LOAD, source="asset_fanout")
    assert result["verdict"] == dc.READY
    assert result["assets"] == [{"asset": "ns/orders", "ready": True, "reason": result["assets"][0]["reason"]}]


def test_a_fact_for_another_date_does_not(reconciler, tokens, asset_events, sfn):
    asset_events.put_item(Item=fact("ns/orders", "2026-11-11"))
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    result = reconciler.reconcile(LOAD, source="asset_fanout")
    assert result["verdict"] == dc.WAIT
    assert result["assets"][0]["ready"] is False
    assert not sfn.succeeded


def test_a_weekly_fact_covers_the_dates_of_its_week(reconciler, tokens, asset_events, sfn):
    asset_events.put_item(Item=fact("ns/weekly", "2026-W46"))       # Monday 2026-11-09
    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/weekly", "freshness_hours": None}]))
    assert reconciler.reconcile(LOAD, source="asset_fanout")["verdict"] == dc.READY


def test_known_events_close_the_index_lag(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    result = reconciler.reconcile(LOAD, source="asset_fanout", known_events=[fact("ns/orders", DATE)])
    assert result["verdict"] == dc.READY


def test_a_recent_legacy_fact_is_read_from_the_base_table(reconciler, tokens, asset_events, sfn):
    # No partition_date, so the partition index cannot see it; the consistent read of recent writes does.
    asset_events.put_item(Item=legacy_fact("ns/orders", DATE, event_time="2026-11-12T05:50:00.123456+00:00"))
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    assert reconciler.reconcile(LOAD, source="asset_fanout")["verdict"] == dc.READY


def test_an_old_legacy_fact_waits_for_adoption(reconciler, tokens, asset_events, sfn):
    asset_events.put_item(Item=legacy_fact("ns/orders", DATE, event_time="2026-11-12T05:00:00+00:00"))
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    assert reconciler.reconcile(LOAD, source="sweeper")["verdict"] == dc.WAIT


def test_a_legacy_fact_takes_the_granularity_its_producer_declares(reconciler, tokens, asset_events, registry, sfn):
    dag = {"nodes": [{"id": "build", "outlets": [{"name": "ns/weekly", "granularity": "weekly"}]}]}
    registry.put_item(Item={"pipeline_name": "producer", "dag": json.dumps(dag)})
    # The old producer stored its run date; as a weekly fact it covers the whole week.
    asset_events.put_item(Item=legacy_fact("ns/weekly", "2026-11-10", event_time="2026-11-12T05:55:00.000Z"))
    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/weekly", "freshness_hours": None}]))
    assert reconciler.reconcile(LOAD, source="asset_fanout")["verdict"] == dc.READY


# ── pause ───────────────────────────────────────────────────────────────


def test_a_ready_task_of_a_paused_run_is_parked(reconciler, tokens, sfn):
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": True})
    tokens.put_item(Item=task_row("load"))
    result = reconciler.reconcile(LOAD, source="registration")
    assert (result["verdict"], result["action"]) == (dc.PAUSED, "parked")
    assert row(tokens)["status"] == "waiting_paused"
    assert not sfn.succeeded
    again = reconciler.reconcile(LOAD, source="sweeper")
    assert (again["verdict"], again["action"]) == (dc.PAUSED, "none")


def test_resume_wakes_a_parked_task(reconciler, tokens, sfn):
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": False})
    tokens.put_item(Item=task_row("load", status="waiting_paused"))
    assert reconciler.reconcile(LOAD, source="resume")["action"] == "signalled"


def test_a_parked_task_could_run_from_the_resume_not_from_its_facts(reconciler, tokens, sfn):
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": False,
                          "resumed_at": "2026-11-12T05:50:00.000Z"})
    tokens.put_item(Item=task_row("extract", status="success", finished_at="2026-11-12T05:10:00.000Z"))
    tokens.put_item(Item=task_row("load", deps=["extract"], status="waiting_paused"))
    assert reconciler.reconcile(LOAD, source="resume")["enabled_at"] == "2026-11-12T05:50:00.000Z"


def test_one_success_could_run_from_its_earliest_success(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("a", status="success", finished_at="2026-11-12T05:20:00.000Z"))
    tokens.put_item(Item=task_row("b", status="success", finished_at="2026-11-12T05:40:00.000Z"))
    tokens.put_item(Item=task_row("load", deps=["a", "b"], trigger_rule="one_success"))
    assert reconciler.reconcile(LOAD, source="notify_dependents")["enabled_at"] == "2026-11-12T05:20:00.000Z"


def test_pause_does_not_hold_back_a_skip(reconciler, tokens, sfn):
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": True})
    tokens.put_item(Item=task_row("extract", status="failed"))
    tokens.put_item(Item=task_row("load", deps=["extract"]))
    reconciler.reconcile(LOAD, source="notify_dependents")
    assert sfn.succeeded[0]["output"]["signal"] == "deps_blocked"


# ── dead tokens ─────────────────────────────────────────────────────────


def _dead(tokens, sfn, wrapper_status, **row_extra):
    item = task_row("load", attention="waiting#a", attention_at="2026-11-12T06:10:00.000Z", **row_extra)
    tokens.put_item(Item=item)
    sfn.dead_tokens.add(item["wait_token"])
    if wrapper_status:
        sfn.executions[item["wrapper_execution_arn"]] = wrapper_status
    return item


def test_a_token_consumed_by_a_running_wrapper_is_left_to_the_wrapper(reconciler, tokens, sfn):
    _dead(tokens, sfn, "RUNNING")
    result = reconciler.reconcile(LOAD, source="sweeper")
    assert result["action"] == "consumed"
    assert row(tokens)["status"] == "waiting"


def test_a_skip_consumed_by_a_finished_pre_upgrade_wrapper_is_written(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("extract", status="skipped", skip_origin="rule"))
    _dead(tokens, sfn, "SUCCEEDED", dependencies=json.dumps(["extract"]))
    result = reconciler.reconcile(LOAD, source="sweeper")
    assert (result["verdict"], result["action"]) == (dc.SKIP, "skipped")
    stored = row(tokens)
    assert (stored["status"], stored["skip_origin"]) == ("skipped", "rule")
    assert stored["finished_at"] == "2026-11-12T06:00:00.000Z" and att.KEY not in stored


@pytest.mark.parametrize("wrapper_status", ["FAILED", "ABORTED", "SUCCEEDED", None])
def test_a_zombie_is_aborted_loudly(reconciler, tokens, sfn, wrapper_status):
    _dead(tokens, sfn, wrapper_status)
    result = reconciler.reconcile(LOAD, source="sweeper")
    assert result["action"] == "aborted"
    stored = row(tokens)
    assert stored["status"] == "aborted" and att.KEY not in stored
    assert "can no longer be woken" in stored["error"]
    assert warning(tokens, "zombie")[att.KEY].startswith("warn#")


def test_a_wrapper_that_is_not_visible_yet_is_not_taken_for_gone(reconciler, tokens, sfn):
    """DescribeExecution is eventually consistent: a row that registered moments ago keeps its task."""
    from datetime import timedelta

    from coordination_core.times import format_utc
    from coordination_testkit import NOW

    _dead(tokens, sfn, None, started_at=format_utc(NOW - timedelta(seconds=30)))
    assert reconciler.reconcile(LOAD, source="sweeper")["action"] == "unconfirmed"
    assert row(tokens)["status"] == "waiting" and warning(tokens, "zombie") is None


def test_a_dead_token_without_a_wrapper_arn_is_reported(reconciler, tokens, sfn):
    _dead(tokens, sfn, None, wrapper_execution_arn="")
    assert reconciler.reconcile(LOAD, source="sweeper")["action"] == "warned"
    assert row(tokens)["status"] == "waiting"
    assert warning(tokens, "zombie") is not None


def test_a_re_registered_row_is_not_closed(reconciler, tokens, sfn, mocker):
    item = _dead(tokens, sfn, "FAILED")
    original = reconciler.signals.execution_status

    def re_register(arn):
        tokens.put_item(Item={**item, "wait_token": "token-new", "started_at": "2026-11-12T06:30:00.000Z"})
        return original(arn)

    mocker.patch.object(reconciler.signals, "execution_status", side_effect=re_register)
    assert reconciler.reconcile(LOAD, source="sweeper")["action"] == "superseded"
    assert row(tokens)["status"] == "waiting"
    assert warning(tokens, "zombie") is None


# ── failures (ADR #130 §9) ──────────────────────────────────────────────


@pytest.mark.parametrize("code", ["ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError"])
def test_transient_failures_raise_and_write_nothing(reconciler, tokens, sfn, mocker, code):
    tokens.put_item(Item=task_row("load", deps=["extract"]))
    mocker.patch.object(reconciler.resource, "batch_get_item", side_effect=client_error(code))
    with pytest.raises(CoordinationTransient):
        reconciler.reconcile(LOAD, source="notify_dependents")
    assert not sfn.succeeded and not sfn.failed
    assert row(tokens) == task_row("load", deps=["extract"])


def test_a_transient_failure_reading_the_row_raises(reconciler, mocker):
    mocker.patch.object(reconciler.tasks.table, "get_item", side_effect=client_error("ThrottlingException"))
    with pytest.raises(CoordinationTransient):
        reconciler.reconcile(LOAD, source="registration")


def test_a_decision_that_cannot_be_made_fails_the_task(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/orders", "partition": "daily"}]))
    result = reconciler.reconcile(LOAD, source="registration")
    assert (result["verdict"], result["action"]) == (reconcile.ERROR, "failed_task")
    assert sfn.failed[0]["error"] == "CoordinationError"
    assert sfn.failed[0]["cause"].startswith("ValueError")


def test_a_transient_failure_while_failing_the_task_raises(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/orders", "partition": "daily"}]))
    sfn.errors["send_task_failure"] = client_error("ThrottlingException")
    with pytest.raises(CoordinationTransient):
        reconciler.reconcile(LOAD, source="registration")


def test_a_row_without_list_attributes_has_no_conditions(reconciler, tokens, sfn):
    item = task_row("load")
    del item["dependencies"], item["wait_for"]
    tokens.put_item(Item=item)
    assert reconciler.reconcile(LOAD, source="sweeper")["verdict"] == dc.READY


def test_a_list_attribute_of_another_shape_fails_the_task(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load", dependencies=json.dumps({"extract": True})))
    assert reconciler.reconcile(LOAD, source="registration")["action"] == "failed_task"
    assert "must be a JSON list" in sfn.failed[0]["cause"]


def test_a_non_transient_aws_error_fails_the_task_with_its_cause(reconciler, tokens, sfn, mocker):
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    mocker.patch.object(reconciler.resource.meta.client, "query", side_effect=client_error("AccessDeniedException", "Query"))
    reconciler.reconcile(LOAD, source="registration")
    assert sfn.failed[0]["cause"].startswith("ClientError: An error occurred (AccessDeniedException)")


def test_a_denied_signal_fails_the_task(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load"))
    sfn.errors["send_task_success"] = client_error("AccessDeniedException")
    assert reconciler.reconcile(LOAD, source="registration")["action"] == "failed_task"


def test_when_failing_the_task_also_fails_a_warning_remains(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load"))
    sfn.errors["send_task_success"] = client_error("AccessDeniedException")
    sfn.errors["send_task_failure"] = client_error("AccessDeniedException")
    assert reconciler.reconcile(LOAD, source="registration")["action"] == "warned"
    assert "Failing the task also failed" in warning(tokens, "coordination")["error"]


def test_failing_a_task_whose_token_is_dead_and_names_no_wrapper_leaves_a_warning(reconciler, tokens, sfn):
    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/orders", "partition": "daily"}],
                                  wrapper_execution_arn=""))
    sfn.dead_tokens.add("token-load")
    assert reconciler.reconcile(LOAD, source="sweeper")["action"] == "warned"
    assert warning(tokens, "coordination")["error"].endswith("The task token is already dead.")


@pytest.mark.parametrize("wrapper,action,status", [
    ("RUNNING", "consumed", "waiting"),     # a concurrent wake consumed the token: the wrapper owns the status
    ("ABORTED", "aborted", "aborted"),      # no wrapper is left: a zombie, closed as one
    (None, "aborted", "aborted"),
])
def test_failing_a_task_whose_token_is_dead_checks_its_wrapper(reconciler, tokens, sfn, wrapper, action, status):
    item = task_row("load", wait_for=[{"asset_name": "ns/orders", "partition": "daily"}])
    tokens.put_item(Item=item)
    sfn.dead_tokens.add("token-load")
    if wrapper:
        sfn.executions[item["wrapper_execution_arn"]] = wrapper

    result = reconciler.reconcile(LOAD, source="sweeper")

    assert (result["verdict"], result["action"], row(tokens)["status"]) == ("error", action, status)
    if action == "aborted":
        assert "Failing the task found its token dead" in warning(tokens, "zombie")["error"]


def test_unexpected_failures_never_escape(reconciler, tokens, mocker):
    mocker.patch.object(reconciler.tasks.table, "get_item", side_effect=client_error("AccessDeniedException"))
    result = reconciler.reconcile(LOAD, source="asset_fanout")
    assert (result["verdict"], result["action"]) == (reconcile.ERROR, "warned")
    assert "AccessDeniedException" in warning(tokens, "coordination")["error"]


def test_a_warning_that_cannot_be_written_is_logged(reconciler, tokens, mocker, capsys):
    mocker.patch.object(reconciler.tasks.table, "get_item", side_effect=client_error("AccessDeniedException"))
    mocker.patch.object(reconciler.tasks.table, "put_item", side_effect=client_error("AccessDeniedException"))
    assert reconciler.reconcile(LOAD, source="asset_fanout")["action"] == "warned"
    assert "could not write the warning row" in capsys.readouterr().out


def test_a_failed_pointer_delete_is_harmless(reconciler, tokens, sfn, mocker):
    tokens.put_item(Item=task_row("load", wait_for=ORDERS))
    mocker.patch.object(reconciler.subscriptions.table, "delete_item", side_effect=client_error("AccessDeniedException"))
    result = reconciler.reconcile(LOAD, source="asset_fanout", known_events=[fact("ns/orders", DATE)])
    assert result["action"] == "signalled"


# ── resume: reconcile_run ───────────────────────────────────────────────


def _run_rows(tokens):
    due = {"attention": "waiting#a", "attention_at": "2026-11-12T07:00:00.000Z"}
    tokens.put_item(Item=task_row("load", **due))
    tokens.put_item(Item=task_row("report", deps=["load"], **due))
    tokens.put_item(Item=task_row("extract", status="running"))
    tokens.put_item(Item={"execution_name": f"_notify_warn_x_{RUN}", "pipeline_execution": RUN, "status": "waiting"})


def test_resume_reconciles_every_waiting_row_of_the_run(reconciler, tokens, sfn):
    _run_rows(tokens)
    result = reconciler.reconcile_run(RUN, source="resume")
    assert (result["handled"], result["deferred"]) == (2, 0)
    assert [s["token"] for s in sfn.succeeded] == ["token-load"]


def test_rows_over_the_budget_are_left_due_for_the_sweeper(ddb, sfn, tokens):
    from reconcile import Reconciler

    _run_rows(tokens)
    result = Reconciler(ddb, sfn, clock=lambda: NOW, budget_seconds=0).reconcile_run(RUN, source="resume")
    assert (result["handled"], result["deferred"]) == (0, 2)
    assert row(tokens)[att.DUE] == "2026-11-12T06:00:00.000Z"          # never resumed: now


def test_deferred_rows_are_due_from_the_resume_so_they_are_no_miss(ddb, sfn, tokens):
    """Their enabling time is no earlier than the resume, so a sweeper that wakes them
    later does not count a primary-path miss (ADR #130 §6.5, §9)."""
    from reconcile import Reconciler

    _run_rows(tokens)
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": False,
                          "resumed_at": "2026-11-12T05:59:30.250000+00:00"})        # as the console writes it
    Reconciler(ddb, sfn, clock=lambda: NOW, budget_seconds=0).reconcile_run(RUN, source="resume")
    assert row(tokens)[att.DUE] == "2026-11-12T05:59:30.250Z"


def test_a_transient_failure_defers_only_that_row(reconciler, tokens, sfn):
    _run_rows(tokens)
    sfn.errors["send_task_success"] = client_error("ThrottlingException")
    result = reconciler.reconcile_run(RUN, source="resume")
    assert (result["handled"], result["deferred"]) == (1, 1)


def test_a_failed_deferral_is_logged(reconciler, tokens, sfn, mocker, capsys):
    _run_rows(tokens)
    sfn.errors["send_task_success"] = client_error("ThrottlingException")
    mocker.patch.object(reconciler.tasks.table, "update_item", side_effect=client_error("AccessDeniedException"))
    reconciler.reconcile_run(RUN, source="resume")
    assert "could not make the row due now" in capsys.readouterr().out


# ── handler ─────────────────────────────────────────────────────────────


@pytest.fixture
def lambda_env(ddb, sfn, monkeypatch):
    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_stepfunctions", sfn)
    monkeypatch.setattr(reconcile, "_RECONCILER", None)


def test_handler_routes_both_payloads(lambda_env, tokens, sfn):
    tokens.put_item(Item=task_row("load"))
    assert reconcile.handler({"execution_name": LOAD, "source": "registration"}, None)["action"] == "signalled"
    assert reconcile.handler({"pipeline_execution": RUN}, None)["source"] == "resume"


def test_handler_rejects_other_payloads(lambda_env):
    with pytest.raises(ValueError, match="execution_name"):
        reconcile.handler({"task": "load"}, None)


def test_the_result_names_its_latency_and_reasons(reconciler, tokens):
    tokens.put_item(Item=task_row("load"))
    result = reconciler.reconcile(LOAD, source="registration")
    assert isinstance(result["latency_ms"], int) and result["reasons"] == ["all conditions met"]


# ── contracts with the state machines (Principle #28) ───────────────────


def _sfn(path):
    text = (REPO_ROOT / "sam" / "sfn_templates" / path).read_text()
    text = re.sub(r'"\$\{[^}]+\}"', '"X"', text)
    return json.loads(re.sub(r"\$\{[^}]+\}", "0", text))


def _state(states, name):
    for key, state in states.items():
        if key == name:
            return state
        for branch in state.get("Branches", []):
            found = _state(branch["States"], name)
            if found:
                return found
    return None


def test_the_reconciler_reads_only_fields_registration_writes():
    written = set(_sfn("helpers/registration/sfn.tpl.json")["States"]["Save_Task_Record"]["Arguments"]["Item"])
    read = {"status", "dependencies", "wait_for", "trigger_rule", "date", "started_at", "wait_token",
            "pipeline_execution", "pipeline_execution_short", "wrapper_execution_arn", "task_name", "pipeline_name"}
    assert read <= written, sorted(read - written)


def test_the_wrapper_routes_every_signal_the_reconciler_sends():
    states = _sfn("dependency_wrapper/sfn.tpl.json")["States"]
    choices = json.dumps(_state(states, "Check_Deps_Signal")["Choices"])
    for verdict in (dc.SKIP, dc.UPSTREAM_FAILED):
        assert f"'{reconcile.SIGNALS[verdict]}'" in choices
    wait = _state(states, "Wait_For_Dependencies")
    assert "$res.signal" in json.dumps(wait["Assign"]) and "$res.reason" in json.dumps(wait["Assign"])


def _fields(update_expression):
    """{"SET": {...}, "REMOVE": {...}} attribute names of an UpdateExpression (#names resolved by the caller)."""
    parts = re.split(r"\b(SET|REMOVE)\b", update_expression)
    found = {}
    for keyword, body in zip(parts[1::2], parts[2::2]):
        found[keyword] = {clause.split("=")[0].strip() for clause in body.split(",") if clause.strip()}
    return found


def test_a_skip_closed_by_the_reconciler_has_the_shape_the_wrapper_writes(reconciler, tokens, sfn):
    claim = _state(_sfn("dependency_wrapper/sfn.tpl.json")["States"], "Claim_Skipped")["Arguments"]
    names = claim["ExpressionAttributeNames"]
    fields = {k: {names.get(f, f) for f in v} for k, v in _fields(claim["UpdateExpression"]).items()}
    tokens.put_item(Item=task_row("extract", status="skipped", skip_origin="rule"))
    _dead(tokens, sfn, "SUCCEEDED", dependencies=json.dumps(["extract"]))
    reconciler.reconcile(LOAD, source="sweeper")
    stored = row(tokens)
    outcome = fields["SET"] - {"claimed_by"}                 # the wrapper's own retry marker (B-W1), not the outcome
    assert outcome <= set(stored), sorted(outcome - set(stored))
    assert not fields["REMOVE"] & set(stored), sorted(fields["REMOVE"] & set(stored))


def test_coordination_error_fails_the_task_instead_of_re_registering():
    wait = _state(_sfn("dependency_wrapper/sfn.tpl.json")["States"], "Wait_For_Dependencies")
    retried = {e for r in wait["Retry"] for e in r["ErrorEquals"]}
    assert not retried & {"CoordinationError", "States.ALL", "States.TaskFailed"}
    assert any("States.ALL" in c["ErrorEquals"] and c["Next"] == "Handle_Failure" for c in wait["Catch"])


def test_an_unreadable_fact_counts_for_no_task_and_is_reported(reconciler, tokens, asset_events, sfn):
    """B-F2: one malformed fact must not fail every task waiting on its asset."""
    from coordination_testkit import fact

    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/orders", "freshness_hours": None}]))
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:00:00.000Z",
                                    partition_date="2026-11-1"))            # not a date: unreadable
    result = reconciler.reconcile(LOAD, source="sweeper")

    assert (result["verdict"], result["action"]) == ("wait", "none")
    assert sfn.failed == []
    reported = warning(tokens, "coordination", "fact-ns/orders-2026-11-12T05:00:00.000Z")
    assert "cannot be read, so it counts for no task" in reported["error"]

    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:30:00.000Z"))
    assert reconciler.reconcile(LOAD, source="sweeper")["action"] == "signalled"   # a good fact still counts


@pytest.mark.parametrize("claim", ["Claim_Deps_Ready", "Claim_Skipped", "Claim_Upstream_Failed"])
def test_a_claim_retried_after_its_write_applied_passes_and_no_other_owner_does(tokens, claim):
    """B-W1: a claim whose write applied but whose answer was lost is retried, and the retry passes its own
    condition. A wrapper of another generation, or an operator who decided the task since, fails it (ADR #130 §2.1)."""
    from decimal import Decimal

    from boto3.dynamodb.types import TypeDeserializer
    from botocore.exceptions import ClientError

    from coordination_core import ownership

    args = _state(_sfn("dependency_wrapper/sfn.tpl.json")["States"], claim)["Arguments"]

    def values(generation):
        out = {}
        for k, v in args["ExpressionAttributeValues"].items():
            if str(v.get("S", "")).startswith("{%"):
                out[k] = "x"
            elif str(v.get("N", "")).startswith("{%"):
                out[k] = Decimal(generation)
            else:
                out[k] = TypeDeserializer().deserialize(v)
        return out

    def claim_as(generation):
        tokens.update_item(
            Key={"execution_name": LOAD},
            UpdateExpression=args["UpdateExpression"], ConditionExpression=args["ConditionExpression"],
            ExpressionAttributeNames=args["ExpressionAttributeNames"], ExpressionAttributeValues=values(generation))

    tokens.put_item(Item=task_row("load", generation=2))
    claim_as(2)
    claim_as(2)                                      # the retry
    with pytest.raises(ClientError, match="ConditionalCheckFailed"):
        claim_as(1)                                  # a wrapper a restart replaced
    tokens.update_item(Key={"execution_name": LOAD}, UpdateExpression=f"SET #s = :s, {ownership.RAISE}",
                       ExpressionAttributeNames={"#s": "status"},
                       ExpressionAttributeValues={":s": "success", **ownership.RAISE_VALUES})
    with pytest.raises(ClientError, match="ConditionalCheckFailed"):
        claim_as(2)                                  # an operator marked it successful meanwhile


def test_a_run_date_that_is_no_partition_key_fails_the_waiting_task_with_the_cause(reconciler, tokens, sfn):
    """`wait_for` needs the run's date on the date line: a date such as 20261113 fails the task loudly."""
    tokens.put_item(Item=task_row("load", wait_for=ORDERS, date="20261113"))
    result = reconciler.reconcile(LOAD, source="registration")
    assert result["action"] == "failed_task"
    assert [f["error"] for f in sfn.failed] == ["CoordinationError"]
    assert "'20261113' is not a partition key" in sfn.failed[0]["cause"]
