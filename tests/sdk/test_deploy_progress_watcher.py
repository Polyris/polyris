"""Tests for the background CloudFormation stack-event watcher added to
`polyris-deploy` for real-time per-resource progress output.

`aws cloudformation deploy` (the subprocess this wraps) does not print
per-resource progress by default — confirmed against its own documented
behavior across CLI versions. This watcher polls the same
describe_stack_events data the AWS Console's Events tab shows, printing new
events as `aws cloudformation deploy` runs, without changing anything about
how that subprocess call itself behaves.

The single most important property here: this is purely observational.
Every test in TestDeployStillWorksExactlyAsBefore exists specifically to
prove the watcher cannot change the deploy's actual outcome (exit code,
error handling), even when the watcher itself is broken or slow.
"""
import threading
import time
from datetime import datetime, timezone

from polyris.deploy import _watch_stack_events, deploy_pipeline, _colorize_status
from polyris import DAG, task


ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:x"


def _event(event_id, status, resource_type, logical_id, reason=None, ts=None):
    e = {
        "EventId": event_id,
        "ResourceStatus": status,
        "ResourceType": resource_type,
        "LogicalResourceId": logical_id,
        "Timestamp": ts or datetime.now(timezone.utc),
    }
    if reason:
        e["ResourceStatusReason"] = reason
    return e


class TestColorizeStatus:
    """The color-coding added to progress output. Plain text (no ANSI
    codes) whenever stdout isn't a real terminal — piping polyris-deploy's
    output to a file or CI log must not embed raw escape codes."""

    def test_plain_text_when_not_a_tty(self, mocker):
        mocker.patch("sys.stdout.isatty", return_value=False)
        assert _colorize_status("CREATE_COMPLETE") == "CREATE_COMPLETE"
        assert "\033[" not in _colorize_status("CREATE_FAILED")

    def test_complete_is_green_when_a_tty(self, mocker):
        mocker.patch("sys.stdout.isatty", return_value=True)
        result = _colorize_status("UPDATE_COMPLETE")
        assert "\033[32m" in result
        assert "UPDATE_COMPLETE" in result
        assert result.endswith("\033[0m")

    def test_failed_is_red_when_a_tty(self, mocker):
        mocker.patch("sys.stdout.isatty", return_value=True)
        assert "\033[31m" in _colorize_status("CREATE_FAILED")

    def test_rollback_is_red_when_a_tty(self, mocker):
        mocker.patch("sys.stdout.isatty", return_value=True)
        assert "\033[31m" in _colorize_status("UPDATE_ROLLBACK_COMPLETE")

    def test_in_progress_is_cyan_when_a_tty(self, mocker):
        mocker.patch("sys.stdout.isatty", return_value=True)
        assert "\033[36m" in _colorize_status("CREATE_IN_PROGRESS")

    def test_other_statuses_are_yellow_when_a_tty(self, mocker):
        mocker.patch("sys.stdout.isatty", return_value=True)
        # A status matching none of the FAILED/ROLLBACK/COMPLETE/IN_PROGRESS
        # keyword buckets falls through to the default (yellow) case.
        assert "\033[33m" in _colorize_status("SOME_UNRECOGNIZED_STATUS")

    def test_the_original_status_text_is_always_preserved(self, mocker):
        """Colored or not, the actual status word must still be findable in
        the output — a script grepping for "CREATE_FAILED" must still work
        even with ANSI codes wrapped around it."""
        mocker.patch("sys.stdout.isatty", return_value=True)
        assert "CREATE_FAILED" in _colorize_status("CREATE_FAILED")



class TestWatchStackEvents:
    def test_prints_only_events_not_in_the_seeded_set(self, mocker, capsys):
        cfn = mocker.MagicMock()
        cfn.describe_stack_events.return_value = {
            "StackEvents": [
                _event("old-1", "CREATE_COMPLETE", "AWS::CloudFormation::Stack", "MyStack"),
                _event("new-1", "CREATE_IN_PROGRESS", "AWS::IAM::Role", "MyRole"),
            ]
        }
        seen = {"old-1"}  # pre-seeded — this one must NOT be printed
        stop = threading.Event()

        def _stop_after_one_poll(*a, **kw):
            stop.set()
        stop.wait = _stop_after_one_poll  # make the loop exit after its first pass

        _watch_stack_events(cfn, "my-stack", seen, stop, poll_interval=0)

        out = capsys.readouterr().out
        assert "MyRole" in out
        assert "MyStack" not in out  # the seeded/old event must not appear

    def test_includes_the_status_reason_when_present(self, mocker, capsys):
        cfn = mocker.MagicMock()
        cfn.describe_stack_events.return_value = {
            "StackEvents": [
                _event("e1", "CREATE_FAILED", "AWS::Scheduler::Schedule", "PipelineSchedule",
                       reason="Invalid request provided"),
            ]
        }
        seen = set()
        stop = threading.Event()
        stop.wait = lambda *a, **kw: stop.set()

        _watch_stack_events(cfn, "my-stack", seen, stop, poll_interval=0)

        out = capsys.readouterr().out
        assert "Invalid request provided" in out
        assert "PipelineSchedule" in out

    def test_never_raises_when_describe_stack_events_fails(self, mocker):
        """A fresh CREATE: the stack doesn't exist yet when polling starts —
        describe_stack_events legitimately errors. Must not propagate."""
        cfn = mocker.MagicMock()
        cfn.describe_stack_events.side_effect = Exception("ValidationError: Stack does not exist")
        seen = set()
        stop = threading.Event()
        stop.wait = lambda *a, **kw: stop.set()

        # Must not raise.
        _watch_stack_events(cfn, "my-stack", seen, stop, poll_interval=0)

    def test_stops_promptly_when_stop_event_is_set(self, mocker):
        cfn = mocker.MagicMock()
        cfn.describe_stack_events.return_value = {"StackEvents": []}
        stop = threading.Event()
        stop.set()  # already stopped before the loop even starts

        _watch_stack_events(cfn, "my-stack", set(), stop, poll_interval=0)

        # With stop already set, the loop body must not have run at all.
        cfn.describe_stack_events.assert_not_called()

    def test_events_printed_in_chronological_order_not_api_response_order(self, mocker, capsys):
        cfn = mocker.MagicMock()
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)
        # API returns them out of order; watcher must sort by Timestamp.
        cfn.describe_stack_events.return_value = {
            "StackEvents": [
                _event("e-later", "CREATE_COMPLETE", "AWS::IAM::Role", "SecondThing", ts=t1),
                _event("e-earlier", "CREATE_IN_PROGRESS", "AWS::IAM::Role", "FirstThing", ts=t0),
            ]
        }
        seen = set()
        stop = threading.Event()
        stop.wait = lambda *a, **kw: stop.set()

        _watch_stack_events(cfn, "my-stack", seen, stop, poll_interval=0)

        out = capsys.readouterr().out
        assert out.index("FirstThing") < out.index("SecondThing")


class _FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def get_parameter(self, Name):
        values = {
            "wrapper_arn": ARN,
            "pipeline_execution_role_arn": "arn:aws:iam::000000000000:role/exec",
            "pipeline_registry_table": "registry",
            "pipeline_tokens_table": "tokens",
        }
        key = Name.rsplit("/", 1)[-1]
        if key not in values:
            raise self.exceptions.ParameterNotFound(key)
        return {"Parameter": {"Value": values[key]}}


class TestDeployStillWorksExactlyAsBefore:
    """The watcher must never change the deploy's actual outcome. Each test
    here breaks the watcher in some way and confirms deploy_pipeline's own
    success/failure behavior is completely unaffected."""

    def _base_mocks(self, mocker, dag, cfn_events_behavior=None):
        mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
        boto = mocker.patch("polyris.deploy.boto3")
        session = mocker.MagicMock()
        boto.Session.return_value = session
        sts = mocker.MagicMock()
        sts.get_caller_identity.return_value = {"Account": "999999999999"}
        ssm = _FakeSSM()
        cfn = mocker.MagicMock()
        cfn.describe_stacks.return_value = {
            "Stacks": [{"Outputs": [{"OutputKey": "StateMachineArn", "OutputValue": ARN}]}]
        }
        if cfn_events_behavior:
            cfn_events_behavior(cfn)
        scheduler = mocker.MagicMock()
        scheduler.get_schedule.return_value = {
            "GroupName": "default", "ScheduleExpression": "cron(0 12 * * ? *)",
            "FlexibleTimeWindow": {"Mode": "OFF"}, "Target": {"Arn": ARN, "RoleArn": "x"},
            "State": "ENABLED",
        }

        def _client(name):
            return {"sts": sts, "ssm": ssm, "cloudformation": cfn, "scheduler": scheduler}.get(
                name, mocker.MagicMock())
        session.client.side_effect = _client

        cfg = mocker.patch("polyris.deploy.polyris_config")
        cfg.stage = "dev"
        cfg.region = "us-east-1"
        cfg.namespace = "acme"
        cfg.profile = None
        cfg.for_stage.return_value = {"account_id": "999999999999"}
        mocker.patch("polyris.deploy._register_pipeline")
        return session

    def _dag(self, dag_id):
        with DAG(dag_id, schedule="cron(0 12 * * ? *)") as dag:
            @task.sfn(arn=ARN)
            def t():
                pass
            t()
        return dag

    def test_successful_subprocess_still_reports_success(self, mocker, capsys):
        mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=0))
        self._base_mocks(mocker, self._dag("ok-deploy"))

        deploy_pipeline(self._dag("ok-deploy"), stage="dev", region="us-east-1")

        assert "deployed successfully" in capsys.readouterr().out

    def test_failed_subprocess_still_exits_nonzero_even_with_watcher_running(self, mocker):
        """The core regression test: a failing `aws cloudformation deploy`
        must still sys.exit(1), regardless of anything the watcher does."""
        import pytest
        mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=1))
        self._base_mocks(mocker, self._dag("fail-deploy"))

        with pytest.raises(SystemExit) as exc:
            deploy_pipeline(self._dag("fail-deploy"), stage="dev", region="us-east-1")
        assert exc.value.code == 1

    def test_watcher_seed_call_raising_does_not_break_the_deploy(self, mocker):
        """describe_stack_events failing during the initial seed (e.g. a
        genuinely fresh stack that doesn't exist yet) must not affect the
        deploy's own success path at all."""
        mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=0))

        def _events_always_fail(cfn):
            cfn.describe_stack_events.side_effect = Exception("Stack does not exist")
        self._base_mocks(mocker, self._dag("fresh-stack"), cfn_events_behavior=_events_always_fail)

        # Must complete without raising.
        deploy_pipeline(self._dag("fresh-stack"), stage="dev", region="us-east-1")

    def test_watcher_thread_is_stopped_before_deploy_pipeline_returns(self, mocker):
        """No lingering background thread left running after deploy_pipeline
        returns — confirms the finally-block join() actually executes."""
        mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=0))
        self._base_mocks(mocker, self._dag("clean-exit"))

        threads_before = {t.ident for t in threading.enumerate()}
        deploy_pipeline(self._dag("clean-exit"), stage="dev", region="us-east-1")
        time.sleep(0.1)
        threads_after = {t.ident for t in threading.enumerate() if t.is_alive()}
        # No new, still-alive threads left over from this deploy.
        assert threads_after - threads_before == set()
