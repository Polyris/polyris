"""Tests for the EventBridge Scheduler migration and schedule-state fixes,
found and built via real AWS testing plus a follow-up migration off the
classic "legacy" EventBridge Rules onto EventBridge Scheduler.

History, in order:

1. `dag.is_paused_upon_creation=True` had ZERO effect on the real deployed
   CloudFormation resource — the template's "State" property was hardcoded
   "ENABLED" unconditionally in deploy.py's real code path, while a function
   nothing in the deploy flow called had the right logic. 1.4 removed that
   function; deploy.py's template is the one place a schedule is written.

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

4. 1.4 (ADR #142 §4): the pipeline stack creates no role. Its schedule
   starts the state machine with the platform's shared schedule role, imported
   from the platform's export; polyris-deploy needs no IAM write action.
"""
import json

import pytest

from polyris import DAG, task
from polyris.deploy import _generate_cfn_template, deploy_pipeline


ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:x"
EXPORT = "acme-dev-polyris-schedule-role-arn"


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
            role_arn="arn:aws:iam::000000000000:role/x", start_run_function_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
            schedule_role_export=EXPORT,
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Type"] == "AWS::Scheduler::Schedule"
        assert rule["Properties"]["State"] == "DISABLED"

    def test_default_unpaused_dag_yields_enabled_state(self):
        dag = _scheduled_dag(is_paused_upon_creation=None)
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", start_run_function_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
            schedule_role_export=EXPORT,
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Properties"]["State"] == "ENABLED"

    def test_flexible_time_window_present(self):
        """AWS::Scheduler::Schedule requires FlexibleTimeWindow — omitting it
        is a real deploy-time failure, not just a lint nitpick."""
        dag = _scheduled_dag()
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", start_run_function_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
            schedule_role_export=EXPORT,
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Properties"]["FlexibleTimeWindow"] == {"Mode": "OFF"}

    def test_the_pipeline_stack_holds_no_iam_resource(self):
        """An author holds no IAM write action (ADR #142 §1): the stack creates no role, so its deploy needs none."""
        template = _generate_cfn_template(
            _scheduled_dag(), namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/x", start_run_function_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
            schedule_role_export=EXPORT,
        )
        assert not [r for r in template["Resources"].values() if r["Type"].startswith("AWS::IAM::")]

    def test_schedule_target_imports_the_platforms_schedule_role(self):
        """The schedule's Target.RoleArn is the platform's schedule role, imported from its export, not
        role_arn (the pipeline's Step Functions execution role): two roles, two trust policies. The import
        keeps the platform from dropping the role while a pipeline uses it (ADR #142 §4)."""
        dag = _scheduled_dag()
        template = _generate_cfn_template(
            dag, namespace="test", stage="dev", region="us-east-1",
            role_arn="arn:aws:iam::000000000000:role/pipeline-exec", start_run_function_arn=ARN,
            registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs",
            schedule_role_export=EXPORT,
        )
        rule = template["Resources"]["PipelineSchedule"]
        assert rule["Properties"]["Target"]["RoleArn"] == {"Fn::ImportValue": EXPORT}
        assert rule["DependsOn"] == ["PipelineStateMachine"]


_SAM_OUTPUTS = [
    {"OutputKey": "StartRunFunctionArn", "OutputValue": ARN},
    {"OutputKey": "OrchestrationRoleArn",
     "OutputValue": "arn:aws:iam::000000000000:role/exec"},
    {"OutputKey": "PipelineRegistryTable", "OutputValue": "registry"},
    {"OutputKey": "PipelineTokensTable", "OutputValue": "tokens"},
    {"OutputKey": "AssetSubscriptionsTable", "OutputValue": "asset-subs"},
    {"OutputKey": "PipelineSpecsTable", "OutputValue": "acme-dev-polyris-pipeline-specs"},
    {"OutputKey": "ResultsBucket", "OutputValue": "results-bucket"},
    {"OutputKey": "PipelineScheduleRoleArn", "OutputValue": "arn:aws:iam::000000000000:role/acme-dev-polyris-schedule-role"},
    {"OutputKey": "PipelineDeployPolicyArn", "OutputValue": "arn:aws:iam::000000000000:policy/acme-dev-polyris-deploy-policy"},
]


#: The platform's DynamoDB, on moto (conftest.platform_dynamodb): deploy writes and reads back the specs.
_PLATFORM: dict = {}


@pytest.fixture(autouse=True)
def _platform_tables(platform_dynamodb):
    _PLATFORM["dynamodb"] = platform_dynamodb
    yield
    _PLATFORM.clear()



def _sam_stack_response(outputs=None):
    return {"Stacks": [{"Outputs": _SAM_OUTPUTS if outputs is None else outputs, "Parameters": [
        {"ParameterKey": "Stage", "ParameterValue": "dev"},
        {"ParameterKey": "Namespace", "ParameterValue": "acme"},
    ]}]}


def _pipeline_stack_response(state_machine_arn):
    return {"Stacks": [{"Outputs": [
        {"OutputKey": "StateMachineArn", "OutputValue": state_machine_arn},
    ]}]}


def _stage_cfg_mock(mocker):
    """Shape a MagicMock like polyris.config._StageConfig — the resolver reads
    stack_name/namespace/region/profile as attributes and .get('account_id')."""
    m = mocker.MagicMock()
    m.stack_name = "acme-dev"
    m.namespace = "acme"
    m.region = "us-east-1"
    m.profile = None
    m.get = lambda k, default=None: {"account_id": "999999999999"}.get(k, default)
    return m


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


def _deploy(mocker, dag, *, current_schedule_state="ENABLED", outputs=None, destroy=False, cfn=None, s3=None):
    """deploy_pipeline() of ``dag``, mocked at the AWS boundary only (the boto3 Session; CloudFormation
    change sets go through the boto3 client, the specs through moto). Returns the clients it used."""
    mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
    boto = mocker.patch("polyris.deploy.boto3")
    session = mocker.MagicMock()
    boto.Session.return_value = session
    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": "999999999999"}
    cfn = cfn or mocker.MagicMock()

    # describe_stacks is called for two stacks in a deploy: the SAM infra
    # stack (returns wrapper ARN, tables, results bucket) and the pipeline
    # stack itself (returns StateMachineArn after CloudFormation deploy).
    def _describe_stacks(StackName):
        if StackName == "acme-dev":
            return _sam_stack_response(outputs)
        return _pipeline_stack_response(ARN)
    cfn.describe_stacks.side_effect = _describe_stacks
    cfn.describe_change_set.return_value = {"Status": "CREATE_COMPLETE", "ChangeSetName": "cs"}

    scheduler = mocker.MagicMock()
    scheduler.get_schedule.return_value = _fake_current_schedule(current_schedule_state)
    s3 = s3 or mocker.MagicMock()

    def _client(name, **_kw):
        return {"sts": sts, "cloudformation": cfn, "scheduler": scheduler, "s3": s3, **_PLATFORM}.get(
            name, mocker.MagicMock())
    session.client.side_effect = _client

    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    cfg.region = "us-east-1"
    cfg.namespace = "acme"
    cfg.profile = None
    cfg.for_stage.return_value = _stage_cfg_mock(mocker)

    mocker.patch("polyris.deploy._register_pipeline")
    mocker.patch("polyris.deploy.destroy_stack")
    mocker.patch("polyris.deploy.unregister_pipeline", return_value=True)

    deploy_pipeline(dag, stage="dev", region="us-east-1", destroy=destroy)
    return {"cfn": cfn, "scheduler": scheduler, "s3": s3}


class TestPostDeployScheduleStateEnforcement:
    """Full deploy_pipeline() flow, mocked at the AWS-boundary only (boto3
    Session; CloudFormation change sets go through the boto3 client) — everything
    else runs for real, so these exercise the actual code path."""

    def _run_deploy(self, mocker, dag, current_schedule_state="ENABLED"):
        return _deploy(mocker, dag, current_schedule_state=current_schedule_state)["scheduler"]

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
        cfn = mocker.MagicMock()

        def _describe_stacks(StackName):
            if StackName == "acme-dev":
                return _sam_stack_response()
            return _pipeline_stack_response(ARN)
        cfn.describe_stacks.side_effect = _describe_stacks
        cfn.describe_change_set.return_value = {"Status": "CREATE_COMPLETE", "ChangeSetName": "cs"}

        scheduler = mocker.MagicMock()
        scheduler.get_schedule.side_effect = Exception("ThrottlingException")

        def _client(name, **_kw):
            return {"sts": sts, "cloudformation": cfn, "scheduler": scheduler, **_PLATFORM}.get(
                name, mocker.MagicMock())
        session.client.side_effect = _client

        cfg = mocker.patch("polyris.deploy.polyris_config")
        cfg.stage = "dev"
        cfg.region = "us-east-1"
        cfg.namespace = "acme"
        cfg.profile = None
        cfg.for_stage.return_value = _stage_cfg_mock(mocker)
        mocker.patch("polyris.deploy._register_pipeline")

        # Must not raise — the deploy as a whole already succeeded via CFN.
        deploy_pipeline(dag, stage="dev", region="us-east-1")


def _unscheduled_dag(dag_id="manual-pipeline"):
    with DAG(dag_id, schedule=None) as dag:
        @task.sfn(arn=ARN)
        def t():
            pass
        t()
    return dag


#: A platform older than 1.4: no schedule role, no deploy policy.
_OUTPUTS_1_3 = [o for o in _SAM_OUTPUTS if o["OutputKey"] not in ("PipelineScheduleRoleArn", "PipelineDeployPolicyArn")]


class TestTheDeployHoldsNoIam:
    """ADR #142 §2, §4: the pipeline stack creates no role, so polyris-deploy needs no IAM
    write and no capability; its schedule imports the platform's role; a role without the deploy policy
    is told which policy to attach."""

    def test_the_change_set_asks_for_no_capability_and_imports_the_platforms_schedule_role(self, mocker):
        cfn = _deploy(mocker, _scheduled_dag())["cfn"]
        request = cfn.create_change_set.call_args.kwargs
        assert request["Capabilities"] == []
        schedule = json.loads(request["TemplateBody"])["Resources"]["PipelineSchedule"]
        # The platform's Namespace and Stage name the export, whatever the pipeline's own are.
        assert schedule["Properties"]["Target"]["RoleArn"] == {"Fn::ImportValue": EXPORT}

    def test_a_scheduled_pipeline_on_a_platform_without_the_schedule_role_is_refused_with_the_cause(
            self, mocker, capsys):
        with pytest.raises(SystemExit) as exc:
            _deploy(mocker, _scheduled_dag(), outputs=_OUTPUTS_1_3)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Missing outputs: PipelineScheduleRoleArn" in out
        assert "Ask whoever installs the platform to run" in out and "polyris-up" in out

    def test_an_unscheduled_pipeline_deploys_on_a_platform_without_the_schedule_role(self, mocker):
        cfn = _deploy(mocker, _unscheduled_dag(), outputs=_OUTPUTS_1_3)["cfn"]
        cfn.execute_change_set.assert_called_once()

    def test_a_scheduled_pipeline_is_destroyed_on_a_platform_without_the_schedule_role(self, mocker):
        import polyris.deploy as deploy

        _deploy(mocker, _scheduled_dag(), outputs=_OUTPUTS_1_3, destroy=True)
        deploy.destroy_stack.assert_called_once()

    def test_a_refused_pass_role_names_the_deploy_policy_to_attach(self, mocker, capsys):
        from botocore.exceptions import WaiterError

        cfn = mocker.MagicMock()
        cfn.get_waiter.return_value.wait.side_effect = WaiterError(
            name="stack_update_complete", reason="terminal failure", last_response={})
        cfn.describe_stack_events.return_value = {"StackEvents": [
            {"ResourceType": "AWS::CloudFormation::Stack", "ResourceStatus": "UPDATE_ROLLBACK_IN_PROGRESS",
             "ResourceStatusReason": "The following resource(s) failed to update: [PipelineStateMachine]."},
            {"ResourceType": "AWS::StepFunctions::StateMachine", "ResourceStatus": "UPDATE_FAILED",
             "ResourceStatusReason": "User: arn:aws:sts::999999999999:assumed-role/ci/x is not authorized to "
                                     "perform: iam:PassRole on resource: arn:aws:iam::999999999999:role/r"},
            {"ResourceType": "AWS::CloudFormation::Stack", "ResourceStatus": "UPDATE_IN_PROGRESS",
             "ResourceStatusReason": "User Initiated"},
        ]}
        with pytest.raises(SystemExit) as exc:
            _deploy(mocker, _scheduled_dag(), cfn=cfn)
        assert exc.value.code == 1
        assert ("Attach the platform's deploy policy to it: "
                "arn:aws:iam::000000000000:policy/acme-dev-polyris-deploy-policy") in capsys.readouterr().out

    def test_a_failure_of_an_earlier_operation_gives_no_pass_role_hint(self, mocker, capsys):
        from botocore.exceptions import WaiterError

        cfn = mocker.MagicMock()
        cfn.get_waiter.return_value.wait.side_effect = WaiterError(
            name="stack_update_complete", reason="terminal failure", last_response={})
        cfn.describe_stack_events.return_value = {"StackEvents": [
            {"ResourceType": "AWS::StepFunctions::StateMachine", "ResourceStatus": "UPDATE_FAILED",
             "ResourceStatusReason": "Invalid State Machine Definition"},
            {"ResourceType": "AWS::CloudFormation::Stack", "ResourceStatus": "UPDATE_IN_PROGRESS",
             "ResourceStatusReason": "User Initiated"},
            {"ResourceType": "AWS::StepFunctions::StateMachine", "ResourceStatus": "UPDATE_FAILED",
             "ResourceStatusReason": "is not authorized to perform: iam:PassRole"},
        ]}
        with pytest.raises(SystemExit):
            _deploy(mocker, _scheduled_dag(), cfn=cfn)
        assert "deploy policy" not in capsys.readouterr().out


class TestALargeTemplateIsTheOneUploaded:
    """ADR #142 §8: a template over the inline limit goes through the results bucket. Its key is
    one nobody can predict, and the change set runs only the template that was uploaded."""

    @pytest.fixture(autouse=True)
    def _small_limit(self, mocker):
        mocker.patch("polyris.deploy.TEMPLATE_BODY_LIMIT", 10)

    @staticmethod
    def _answer_with(cfn, s3, swap=False):
        def _get_template(StackName, ChangeSetName, TemplateStage):
            assert TemplateStage == "Original"
            body = json.loads(s3.put_object.call_args.kwargs["Body"])
            if swap:
                body["Resources"]["Evil"] = {"Type": "AWS::IAM::Role"}
            return {"TemplateBody": body}  # boto3 parses a JSON body
        cfn.get_template.side_effect = _get_template

    def test_the_uploaded_template_is_deployed_under_a_key_of_its_own(self, mocker):
        keys = []
        for _ in range(2):
            cfn, s3 = mocker.MagicMock(), mocker.MagicMock()
            self._answer_with(cfn, s3)
            _deploy(mocker, _scheduled_dag(), cfn=cfn, s3=s3)
            cfn.execute_change_set.assert_called_once()
            keys.append(s3.put_object.call_args.kwargs["Key"])
            assert cfn.create_change_set.call_args.kwargs["TemplateURL"].endswith(keys[-1])
        assert keys[0] != keys[1] and all(k.startswith("cfn-pipeline-templates/") for k in keys)

    def test_a_swapped_template_is_refused_and_its_change_set_deleted(self, mocker, capsys):
        cfn = mocker.MagicMock()
        s3 = mocker.MagicMock()
        self._answer_with(cfn, s3, swap=True)
        with pytest.raises(SystemExit) as exc:
            _deploy(mocker, _scheduled_dag(), cfn=cfn, s3=s3)
        assert exc.value.code == 1
        cfn.execute_change_set.assert_not_called()
        cfn.delete_change_set.assert_called_once()
        assert "is not the one uploaded" in capsys.readouterr().out
