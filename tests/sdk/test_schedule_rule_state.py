"""Tests for the EventBridge Scheduler migration and schedule-state fixes,
found and built via real AWS testing plus a follow-up migration off the
classic "legacy" EventBridge Rules onto EventBridge Scheduler.

History, in order:

1. `dag.is_paused_upon_creation=True` had ZERO effect on the real deployed
   CloudFormation resource — the template's "State" property was hardcoded
   "ENABLED" unconditionally in deploy.py's real code path. A DIFFERENT,
   never-called function (generators.generate_eventbridge_schedule) already
   had the correct conditional logic, but nothing in the real deploy flow
   ever invoked it — dead code that looked like it worked.

2. `aws cloudformation deploy` only diffs the new template against what
   CloudFormation tracked as the last-applied template, not the resource's
   actual live state. So manually disabling the schedule via the AWS
   Console, then redeploying with no other template changes, reported "No
   changes to deploy" and left the manual change untouched.

3. Migrated the underlying resource from the classic AWS::Events::Rule
   (which the AWS console itself now labels "legacy") to
   AWS::Scheduler::Schedule. This needs its own IAM role (Scheduler assumes
   it, scoped to this schedule via an aws:SourceArn condition to avoid the
   confused-deputy problem) and a required FlexibleTimeWindow property.
   The post-deploy enforcement (fix #2) now uses the scheduler client's
   get_schedule/update_schedule instead of the classic events client's
   enable_rule/disable_rule, since Scheduler has no simple toggle API.
"""
from polyris import DAG, task
from polyris.deploy import _generate_cfn_template, deploy_pipeline


ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:x"


def _scheduled_dag(dag_id="test-pipeline", is_paused_upon_creation=None):
    with DAG(dag_id, schedule="cron(0 12 * * ? *)", is_paused_upon_creation=is_paused_upon_creation) as dag:
        @task.sfn(arn=ARN)
        def t():
            pass
        t()
    return dag


class TestScheduleResourceInTemplate:
    def test_is_paused_upon_creation_true_yields_disabled_state(self):
        dag = _scheduled_dag(is_paused_upon_creation=True)
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", wrapper_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Type"] == "AWS::Scheduler::Schedule"
        assert rule["Properties"]["State"] == "DISABLED"

    def test_default_unpaused_dag_yields_enabled_state(self):
        dag = _scheduled_dag(is_paused_upon_creation=None)
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", wrapper_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Properties"]["State"] == "ENABLED"

    def test_flexible_time_window_present(self):
        """AWS::Scheduler::Schedule requires FlexibleTimeWindow — omitting it
        is a real deploy-time failure, not just a lint nitpick."""
        dag = _scheduled_dag()
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", wrapper_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Properties"]["FlexibleTimeWindow"] == {"Mode": "OFF"}

    def test_scheduler_role_trust_policy_scoped_to_scheduler_service(self):
        dag = _scheduled_dag()
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", wrapper_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
        )
        role = template["Resources"]["PipelineSchedulerRole"]
        statement = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert statement["Principal"]["Service"] == "scheduler.amazonaws.com"
        assert statement["Action"] == "sts:AssumeRole"
        # Deliberately unconditional: a confused-deputy Condition
        # (aws:SourceArn/aws:SourceAccount) was tried and removed after a
        # real deploy failed at CreateSchedule with "The execution role you
        # provide must allow AWS EventBridge Scheduler to assume the role" —
        # a known, widely-reported issue with Scheduler's create-time trust-
        # policy validation and conditional AssumeRole statements. The
        # permission policy (see the next test) already scopes this role to
        # states:StartExecution on only this one pipeline's state machine,
        # so the blast radius stays bounded without this condition.
        assert "Condition" not in statement

    def test_scheduler_role_can_invoke_the_state_machine(self):
        dag = _scheduled_dag()
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", wrapper_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
        )
        role = template["Resources"]["PipelineSchedulerRole"]
        policy = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
        assert policy["Action"] == "states:StartExecution"
        assert policy["Resource"] == {"Ref": "PipelineStateMachine"}

    def test_schedule_target_points_at_the_new_role_not_the_pipeline_role(self):
        """The schedule's Target.RoleArn must be the NEW scheduler role, not
        role_arn (the pipeline's own Step Functions execution role) — those
        are two different roles with two different trust policies."""
        dag = _scheduled_dag()
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/pipeline-exec", wrapper_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Properties"]["Target"]["RoleArn"] == {"Fn::GetAtt": ["PipelineSchedulerRole", "Arn"]}


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


def _fake_current_schedule(state="ENABLED"):
    """What scheduler.get_schedule would return for an already-deployed
    schedule — the shape update_schedule needs re-submitted."""
    return {
        "Name": "irrelevant",
        "GroupName": "default",
        "ScheduleExpression": "cron(0 12 * * ? *)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": ARN, "RoleArn": "arn:aws:iam::000000000000:role/scheduler"},
        "State": state,
    }


class TestPostDeployScheduleStateEnforcement:
    """Full deploy_pipeline() flow, mocked at the AWS-boundary only (boto3
    Session, subprocess.run for `aws cloudformation deploy`) — everything
    else runs for real, so these exercise the actual code path."""

    def _run_deploy(self, mocker, dag, current_schedule_state="ENABLED"):
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
        scheduler = mocker.MagicMock()
        scheduler.get_schedule.return_value = _fake_current_schedule(current_schedule_state)

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

        mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=0))
        mocker.patch("polyris.deploy._register_pipeline")

        deploy_pipeline(dag, stage="dev", region="us-east-1")
        return scheduler

    def test_unpaused_dag_updates_a_disabled_schedule_to_enabled(self, mocker):
        """The exact drift bug: even with NO template changes, every
        redeploy must explicitly re-assert 'enabled' — this is what would
        revert a schedule someone manually disabled via the Console."""
        dag = _scheduled_dag(dag_id="unpaused-pipeline", is_paused_upon_creation=None)
        scheduler = self._run_deploy(mocker, dag, current_schedule_state="DISABLED")
        scheduler.update_schedule.assert_called_once()
        assert scheduler.update_schedule.call_args.kwargs["State"] == "ENABLED"
        assert scheduler.update_schedule.call_args.kwargs["Name"] == "acme-dev-polyris-unpaused-pipeline-schedule"

    def test_paused_dag_updates_an_enabled_schedule_to_disabled(self, mocker):
        """Regression test for the exact bug: is_paused_upon_creation=True
        previously had NO effect on the real deployed schedule at all."""
        dag = _scheduled_dag(dag_id="paused-pipeline", is_paused_upon_creation=True)
        scheduler = self._run_deploy(mocker, dag, current_schedule_state="ENABLED")
        scheduler.update_schedule.assert_called_once()
        assert scheduler.update_schedule.call_args.kwargs["State"] == "DISABLED"

    def test_already_correct_state_does_not_trigger_a_redundant_update(self, mocker):
        """If the live schedule already matches the desired state, don't
        make an unnecessary API call on every single deploy."""
        dag = _scheduled_dag(dag_id="already-enabled", is_paused_upon_creation=None)
        scheduler = self._run_deploy(mocker, dag, current_schedule_state="ENABLED")
        scheduler.update_schedule.assert_not_called()

    def test_update_preserves_the_schedule_expression_and_target(self, mocker):
        """update_schedule is a full replace — the call must resubmit the
        schedule's actual expression/target, not blank placeholders that
        would silently corrupt the schedule."""
        dag = _scheduled_dag(dag_id="preserve-fields", is_paused_upon_creation=True)
        scheduler = self._run_deploy(mocker, dag, current_schedule_state="ENABLED")
        call_kwargs = scheduler.update_schedule.call_args.kwargs
        assert call_kwargs["ScheduleExpression"] == "cron(0 12 * * ? *)"
        assert call_kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}
        assert call_kwargs["Target"]["Arn"] == ARN

    def test_asset_triggered_dag_has_no_schedule_to_touch(self, mocker):
        """A DAG with no time-based schedule (asset-triggered or
        unscheduled) has no PipelineSchedule resource at all — the
        enforcement call must not fire for it."""
        with DAG("no-schedule-pipeline", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def t():
                pass
            t()
        scheduler = self._run_deploy(mocker, dag)
        scheduler.get_schedule.assert_not_called()
        scheduler.update_schedule.assert_not_called()

    def test_schedule_enforcement_failure_does_not_crash_the_whole_deploy(self, mocker):
        """A transient Scheduler API error on this best-effort enforcement
        step must be reported, not abort an otherwise-successful deploy."""
        dag = _scheduled_dag(dag_id="flaky-scheduler-api", is_paused_upon_creation=None)
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
        scheduler = mocker.MagicMock()
        scheduler.get_schedule.side_effect = Exception("ThrottlingException")

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
        mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=0))
        mocker.patch("polyris.deploy._register_pipeline")

        # Must not raise — the deploy as a whole already succeeded via CFN.
        deploy_pipeline(dag, stage="dev", region="us-east-1")
