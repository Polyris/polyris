"""Tests for routes.tasks.restart_task.

No test file existed for this function before. Regression tests here cover
a real, high-impact bug found in a code-review pass: restart_task's own
precondition check only accepted TASK_TERMINAL_STATUSES (success, failed,
skipped, aborted, upstream_failed, succeeded) — it did NOT accept 'stopped'.
But stop_task's own success message tells the user "Use Restart to resume",
and stop_task deliberately treats 'stopped' as non-terminal/resumable (not
in TASK_TERMINAL_STATUSES on purpose). So the advertised "stop, then
restart" workflow was completely non-functional: any user who stopped a
task and then tried to restart it, exactly as told, got a 409 saying their
task "is not in a restartable state" — contradicting the message they had
just followed.
"""
import json

import pytest

from routes.tasks import restart_task


def _item(status="failed", **overrides):
    base = {
        "execution_name": "extract-2024-01-15-abc12345",
        "task_name": "extract",
        "status": status,
        "date": "2024-01-15",
        "pipeline_execution": "run1",
    }
    base.update(overrides)
    return base


def _event(date="2024-01-15"):
    return {"body": json.dumps({"date": date})}


def _body(resp):
    return json.loads(resp["body"])


@pytest.fixture
def with_sfn_helper(monkeypatch):
    """The SAM template always sets RESTART_HELPER_ARN — that's the only real
    deploy shape. The previously-tested fallback path was dead code (couldn't
    do the two-level stop the helper does) and is now removed; restart_task
    fails fast with 500 when ARN is missing (see test_missing_helper_arn)."""
    monkeypatch.setenv("RESTART_HELPER_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:restart-helper")


@pytest.fixture
def missing_helper_arn(monkeypatch):
    monkeypatch.delenv("RESTART_HELPER_ARN", raising=False)


class TestRestartTaskAcceptsStoppedStatus:
    """The exact bug: 'stopped' must be restartable via the SFN-helper path."""

    def test_stopped_task_restartable_via_sfn_helper_path(self, mocker, with_sfn_helper):
        item = _item(status="stopped")
        mocker.patch("routes.tasks.resolve_task_item", return_value=(item, item["execution_name"]))
        mocker.patch("routes.tasks.record_manual_decision")
        mocker.patch("routes.tasks.sfn.start_execution",
                      return_value={"executionArn": "arn:aws:states:us-east-1:123456789012:execution:x:y"})

        resp = restart_task(item["execution_name"], _event())

        assert resp["statusCode"] == 200
        assert "Restart initiated" in _body(resp)["message"]


class TestRestartTaskStillAcceptsTerminalStatuses:
    """Control: every status that worked before must keep working."""

    @pytest.mark.parametrize("status", ["success", "failed", "skipped", "aborted", "upstream_failed"])
    def test_each_terminal_status_is_restartable(self, mocker, with_sfn_helper, status):
        item = _item(status=status)
        mocker.patch("routes.tasks.resolve_task_item", return_value=(item, item["execution_name"]))
        mocker.patch("routes.tasks.record_manual_decision")
        mocker.patch("routes.tasks.sfn.start_execution",
                      return_value={"executionArn": "arn:aws:states:us-east-1:123456789012:execution:x:y"})

        resp = restart_task(item["execution_name"], _event())

        assert resp["statusCode"] == 200


class TestRestartTaskRejectsNonRestartableStatuses:
    """Statuses that genuinely cannot be restarted must still 409."""

    @pytest.mark.parametrize("status", ["running", "waiting", "waiting_delay", "deps_ready"])
    def test_active_statuses_rejected(self, mocker, with_sfn_helper, status):
        item = _item(status=status)
        mocker.patch("routes.tasks.resolve_task_item", return_value=(item, item["execution_name"]))

        resp = restart_task(item["execution_name"], _event())

        assert resp["statusCode"] == 409
        assert status in _body(resp)["error"]

    def test_task_not_found_returns_404(self, mocker, with_sfn_helper):
        mocker.patch("routes.tasks.resolve_task_item", return_value=(None, None))

        resp = restart_task("nonexistent", _event())

        assert resp["statusCode"] == 404


class TestRestartTaskFailsFastOnMissingHelperArn:
    """RESTART_HELPER_ARN is set automatically by the SAM template in every real
    deploy. Its absence means a broken deploy — the old fallback path used to
    do a degraded restart but couldn't do the two-level stop that the helper
    does (would leave ghost wrappers alive on waiting_decision restarts).
    Fail fast with a clear error rather than attempting a degraded restart."""

    def test_missing_helper_arn_returns_500(self, mocker, missing_helper_arn):
        item = _item(status="failed")
        mocker.patch("routes.tasks.resolve_task_item", return_value=(item, item["execution_name"]))

        resp = restart_task(item["execution_name"], _event())

        assert resp["statusCode"] == 500
        assert "RESTART_HELPER_ARN" in _body(resp)["error"]
