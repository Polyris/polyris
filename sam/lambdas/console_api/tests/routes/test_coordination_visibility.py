"""The bell reads coordination warnings by write time; /api/health reports the engine (ADR #130 §9).

DynamoDB is moto, with the tokens table built from sam/template.yaml, so the
attention index is the deployed one. Warning rows are written by the same
`warning_item` the coordination code uses.
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from polyris.coordination import sweep
from polyris.coordination.records import warning_item
from polyris.coordination.times import format_utc

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def tokens(mocker):
    import config
    from dal import executions_repo

    logical, name = kit.TABLES["TOKENS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(config, "_dynamodb", resource)
        mocker.patch.object(executions_repo, "_table_name", name)
        yield resource.Table(name)


def _warning(tokens, *, kind="coordination", task="load", task_date="2026-10-13", minutes_ago=10):
    tokens.put_item(Item=warning_item(
        kind=kind, execution_name=f"{task}-{task_date}-run", task_name=task, pipeline_name="orders",
        pipeline_execution="orders-run", task_date=task_date, error=f"{kind} happened",
        now=NOW - timedelta(minutes=minutes_ago)))


def _bell(mocker, hours="24", limit="10"):
    from routes import notifications

    mocker.patch.object(notifications, "_backfill_terminal_notifications", return_value=[])
    resp = notifications.get_notifications({"queryStringParameters": {"hours": hours, "limit": limit}})
    assert resp["statusCode"] == 200
    return json.loads(resp["body"])["notifications"]


def test_a_warning_about_an_old_partition_reaches_the_bell(tokens, mocker):
    _warning(tokens)                                    # a task of 2026-10-13, warned about now

    [shown] = _bell(mocker)

    assert (shown["type"], shown["kind"], shown["date"]) == ("warning", "coordination", "2026-10-13")
    assert shown["task_name"] == "load" and shown["error"] == "coordination happened"
    assert shown["id"] == "_notify_warn_coordination_load-2026-10-13-run"


def test_two_kinds_of_warning_about_one_task_are_both_shown(tokens, mocker):
    _warning(tokens, kind="push")
    _warning(tokens, kind="sweeper", minutes_ago=5)

    assert sorted(n["kind"] for n in _bell(mocker)) == ["push", "sweeper"]


def test_warnings_outside_the_window_and_over_the_limit_are_left_out(tokens, mocker):
    _warning(tokens, task="old", minutes_ago=60 * 30)
    for i in range(3):
        _warning(tokens, task=f"t{i}", kind="push", minutes_ago=i + 1)

    shown = _bell(mocker, limit="2")

    assert [n["task_name"] for n in shown] == ["t0", "t1"]


def test_a_warning_read_that_fails_leaves_the_rest_of_the_bell(tokens, mocker):
    from botocore.exceptions import ClientError
    from routes import notifications

    mocker.patch.object(notifications.executions_repo, "recent_warnings",
                        side_effect=ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Query"))
    assert _bell(mocker) == []


class _Now(datetime):
    """The route's clock, held at NOW: a report's age reads in whole minutes, so a slow suite made 30 minutes 31."""

    @classmethod
    def now(cls, tz=None):
        return NOW


def _health(mocker, tokens):
    from routes import health

    mocker.patch.object(health, "datetime", _Now)
    for check in ("_check_dynamodb", "_check_stepfunctions", "_check_recent_failures", "_check_circuit_breakers"):
        mocker.patch.object(health, check, return_value={"status": health.HEALTHY})
    resp = health.health_check({})
    return resp["statusCode"], json.loads(resp["body"])


def test_a_fresh_engine_report_is_healthy(tokens, mocker):
    tokens.put_item(Item={"execution_name": sweep.HEALTH_KEY, "checked_at": format_utc(NOW - timedelta(minutes=2)),
                          "fanout_mapping": "Enabled", "dispatch_mapping": "Enabled", "tasks_stream": "ENABLED", "stream": "ENABLED", "oldest_overdue": "",
                          "push_retry_backlog": 3})

    status, body = _health(mocker, tokens)

    assert (status, body["status"], body["detail"], body["findings"]) == (200, "healthy", "full", [])
    coordination = body["checks"]["coordination"]
    assert (coordination["status"], set(coordination)) == ("healthy", {"status", "report"})
    assert coordination["report"]["push_retry_backlog"] == 3


def test_a_stale_report_degrades_the_api_and_names_the_finding(tokens, mocker):
    tokens.put_item(Item={"execution_name": sweep.HEALTH_KEY, "checked_at": format_utc(NOW - timedelta(minutes=30)),
                          "fanout_mapping": "Enabled", "dispatch_mapping": "Enabled", "tasks_stream": "ENABLED", "stream": "ENABLED", "oldest_overdue": ""})

    status, body = _health(mocker, tokens)

    assert (status, body["status"]) == (200, "degraded")             # the API serves; the console shows the finding
    assert body["checks"]["coordination"]["status"] == "unhealthy"
    [finding] = body["findings"]
    assert finding["title"] == "The periodic re-check is not running"
    # A report written before the names were added: the name reads in words, and no command is given.
    assert finding["resource"] == "The last run of the sweeper function was 30 min ago. It runs every 5 minutes."
    assert "command" not in finding


def test_a_failed_sweeper_step_degrades_the_api_and_is_shown(tokens, mocker):
    tokens.put_item(Item={"execution_name": sweep.HEALTH_KEY, "checked_at": format_utc(NOW - timedelta(minutes=2)),
                          "fanout_mapping": "Enabled", "dispatch_mapping": "Enabled", "tasks_stream": "ENABLED", "fanout_result": "OK", "stream": "ENABLED",
                          "oldest_overdue": "", "failed_steps": ["catch_up: ClientError: throttled"]})

    _, body = _health(mocker, tokens)

    assert [(f["title"], f["resource"]) for f in body["findings"]] == [
        ("The periodic re-check could not finish the hourly push catch-up", "ClientError: throttled")]
    coordination = body["checks"]["coordination"]
    assert (coordination["report"]["fanout_result"], coordination["report"]["failed_steps"]) == (
        "OK", ["catch_up: ClientError: throttled"])


def test_no_report_yet_is_named_too(tokens, mocker):
    _, body = _health(mocker, tokens)
    assert [f["title"] for f in body["findings"]] == ["No check has run yet"]
    assert body["checks"]["coordination"]["status"] == "unhealthy"


def test_a_report_that_cannot_be_read_is_unhealthy(tokens, mocker):
    from botocore.exceptions import ClientError
    from routes import health

    mocker.patch.object(health.executions_repo, "get",
                        side_effect=ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetItem"))
    _, body = _health(mocker, tokens)
    assert body["checks"]["coordination"] == {"status": "unhealthy", "report": {}}
    [finding] = body["findings"]
    assert finding == {
        "title": "The coordination status cannot be read",
        "resource": "ClientError: An error occurred (AccessDeniedException) when calling the GetItem operation: no",
        "impact": "The console cannot tell whether waiting tasks are re-checked.",
        "fix": "Check the console API's access to the pipeline-tokens table, then read the console API's logs.",
    }


@pytest.mark.parametrize("field,value,error", [
    ("checked_at", "yesterday", "ValueError"),
    ("checked_at", "2026-11-12T05:58:00", "ValueError"),          # no timezone
    ("oldest_overdue", "soon", "ValueError"),
    ("push_retry_backlog", "many", "ValueError"),
    ("failed_steps", 5, "TypeError"),
])
def test_a_report_that_cannot_be_judged_is_unhealthy_and_the_api_still_answers(tokens, mocker, field, value, error):
    """Each of these failed all of /api/health with a 500, so the console could not show what is wrong
    (external review, round 2)."""
    report = {"execution_name": sweep.HEALTH_KEY, "checked_at": format_utc(NOW - timedelta(minutes=2)),
              "fanout_mapping": "Enabled", "dispatch_mapping": "Enabled", "tasks_stream": "ENABLED", "stream": "ENABLED", "oldest_overdue": ""}
    tokens.put_item(Item={**report, field: value})

    status, body = _health(mocker, tokens)

    assert (status, body["status"]) == (200, "degraded")
    assert body["checks"]["coordination"] == {"status": "unhealthy", "report": {}}
    [finding] = body["findings"]
    assert finding["title"] == "The coordination status cannot be read"
    assert finding["resource"].startswith(f"{error}: ")
