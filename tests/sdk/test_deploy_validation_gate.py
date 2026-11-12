"""polyris-deploy must validate the DAG before touching AWS.

Deploying an invalid DAG otherwise only fails later in CloudFormation (or ships a
broken pipeline), so deploy_pipeline runs the same check as polyris-validate first.
"""
import pytest
from botocore.exceptions import BotoCoreError

from polyris import deploy


def test_deploy_rejects_invalid_dag_before_aws(mocker):
    dag = mocker.MagicMock()
    dag.dag_id = "broken"
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(False, ["Cycle detected: a -> b -> a"], []),
    )
    boto = mocker.patch("polyris.deploy.boto3")

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)

    assert exc.value.code == 1
    boto.Session.assert_not_called()  # the gate fired before any AWS work


def _stage_cfg(mocker, *, stack_name="acme-prod", **kw):
    """Return a MagicMock shaped like polyris.config._StageConfig — the
    attribute surface the deploy resolver reads."""
    m = mocker.MagicMock()
    m.stack_name = stack_name
    m.namespace = kw.get("namespace", "acme")
    m.region = kw.get("region", "us-east-1")
    m.profile = kw.get("profile", None)
    account = kw.get("account_id", "999999999999")
    m.get = lambda k, default=None: {"account_id": account}.get(k, default)
    return m


def test_deploy_proceeds_past_gate_when_valid(mocker):
    dag = mocker.MagicMock()
    dag.dag_id = "ok"
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    # Config is resolved before boto3.Session is called; provide the minimum
    # so the resolver doesn't exit early on stack_name being missing.
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    cfg.region = "us-east-1"
    cfg.namespace = "acme"
    cfg.profile = None
    cfg.for_stage.return_value = _stage_cfg(mocker, stack_name="acme-dev")
    # Make the next step (credentials) exit so we don't reach real AWS, but prove
    # the validation gate did not block a valid DAG.
    boto = mocker.patch("polyris.deploy.boto3")
    boto.Session.side_effect = SystemExit(2)

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)

    assert exc.value.code == 2  # passed the gate, failed later (as arranged)
    boto.Session.assert_called_once()


def test_deploy_refuses_when_account_cannot_be_verified(mocker):
    """Regression test: previously, any STS error not matching the specific
    substrings "credentials"/"access"/"AuthFailure" (e.g. a generic/unusual
    STS failure — an endpoint timeout, a malformed profile, anything else)
    fell through to `caller_identity = None` and CONTINUED past the
    account-mismatch guard entirely, since `if expected_account and
    caller_identity` silently evaluates False when caller_identity is None
    — the safety check meant to prevent an accidental wrong-account deploy
    was bypassed, not enforced, for this whole class of STS failure. It
    must now refuse to proceed instead."""
    dag = mocker.MagicMock()
    dag.dag_id = "acct-check"
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    boto = mocker.patch("polyris.deploy.boto3")
    session = mocker.MagicMock()
    boto.Session.return_value = session
    sts = mocker.MagicMock()
    sts.get_caller_identity.side_effect = BotoCoreError()
    session.client.side_effect = lambda name, **_kw: sts if name == "sts" else mocker.MagicMock()

    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "prod"
    cfg.region = "us-east-1"
    cfg.namespace = "acme"
    cfg.profile = None
    cfg.for_stage.return_value = _stage_cfg(mocker, stack_name="acme-prod")

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)

    assert exc.value.code == 1
    # Must fail at the account-verification guard, not proceed to read the
    # SAM stack outputs for the (unverified) account.
    session.client.assert_any_call("sts")
    assert not any(
        call.args and call.args[0] == "cloudformation"
        for call in session.client.call_args_list
    )


def test_deploy_proceeds_when_account_matches(mocker):
    """Control: a real, matching caller identity must still let deployment
    proceed past the account guard (the fix must not make every deploy fail)."""
    dag = mocker.MagicMock()
    dag.dag_id = "acct-ok"
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    boto = mocker.patch("polyris.deploy.boto3")
    session = mocker.MagicMock()
    boto.Session.return_value = session
    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": "999999999999"}

    # SAM stack exists but returns no Outputs — simulates the "stack was
    # deployed against an older polyris/SAM template that predates the
    # required outputs" case. The point of this control test is to prove the
    # code got past the account guard and reached the SAM read.
    cfn = mocker.MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [], "Parameters": []}]}

    def _client(name, **_kw):
        return {"sts": sts, "cloudformation": cfn}.get(name, mocker.MagicMock())
    session.client.side_effect = _client

    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "prod"
    cfg.region = "us-east-1"
    cfg.namespace = "acme"
    cfg.profile = None
    cfg.for_stage.return_value = _stage_cfg(mocker, stack_name="acme-prod")

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)

    # Passed the account guard (reached cloudformation), then correctly
    # stopped later because the SAM stack was missing required outputs.
    session.client.assert_any_call("cloudformation")
    cfn.describe_stacks.assert_called_with(StackName="acme-prod")
    assert exc.value.code == 1


def test_generate_cfn_template_rejects_unsubstituted_table_name():
    """If a required table parameter comes through empty (e.g. its SSM
    parameter was never created), _generate_cfn_template must fail loudly
    with a clear ValueError rather than silently deploy a state machine whose
    DynamoDB TableName is the literal string '${asset_subscriptions_table}' —
    the exact failure mode that shipped to production undetected."""
    from polyris import DAG, task, Asset
    from polyris.deploy import _generate_cfn_template

    trigger = Asset("ns/trigger")
    with DAG("gate-test", schedule=trigger) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
        def go():
            pass
        go()

    with pytest.raises(ValueError, match="asset_subscriptions_table"):
        _generate_cfn_template(
            dag=dag,
            namespace="acme",
            stage="dev",
            region="us-east-1",
            role_arn="arn:aws:iam::123456789012:role/x",
            wrapper_arn="arn:aws:states:us-east-1:123456789012:stateMachine:wrapper",
            registry_table="real-registry",
            tokens_table="real-tokens",
            asset_subscriptions_table="",  # simulates a missing SSM parameter
        )


def test_namespace_stage_length_guardrail(mocker):
    """deploy_pipeline exits early when namespace+stage exceeds NAMESPACE_STAGE_MAX_LEN,
    which test_platform.py derives from the longest IAM role name in template.yaml.

    Guards are skipped on --destroy (no IAM roles created); test uses deploy path.
    """
    # A project with config.py: its own naming applies, so the guard runs
    # before any AWS call (without config.py the platform's naming is
    # adopted first — see test_deploy_platform_discovery.py).
    mocker.patch("polyris.config._find_project_config", return_value="config.py")
    dag = mocker.MagicMock()
    dag.dag_id = "my-pipeline"
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    cfg = mocker.patch("polyris.deploy.polyris_config")
    # namespace(24) + stage(9) = 33 > 30
    cfg.stage = "stagingxy"
    cfg.for_stage.return_value = _stage_cfg(
        mocker,
        namespace="this-is-a-long-namespace",
        stack_name="some-stack",
    )
    boto = mocker.patch("polyris.deploy.boto3")

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)  # deploy path: guards are active

    assert exc.value.code == 1
    boto.Session.assert_not_called()  # guard fires before any AWS work


def test_namespace_stage_length_guardrail_skipped_on_destroy(mocker):
    """Guards are skipped on --destroy so existing over-limit stacks can still be removed."""
    dag = mocker.MagicMock()
    dag.dag_id = "my-pipeline"
    cfg = mocker.patch("polyris.deploy.polyris_config")
    # namespace(24) + stage(9) = 33 > 30 — would fail on deploy, must pass on destroy
    cfg.stage = "stagingxy"
    cfg.for_stage.return_value = _stage_cfg(
        mocker,
        namespace="this-is-a-long-namespace",
        stack_name="some-stack",
    )
    boto = mocker.patch("polyris.deploy.boto3")
    boto.Session.side_effect = SystemExit(2)  # guard passed → boto reached → arranged exit

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag, destroy=True)

    assert exc.value.code == 2  # reached boto, not the IAM guard


def test_scheduler_role_dag_id_length_guardrail(mocker):
    """deploy_pipeline exits when the scheduler IAM role name would exceed 64 chars.

    Scheduler role: {namespace}-{stage}-polyris-{dag_id}-scheduler-role
    = len(ns)+1+len(stage)+9+len(dag_id)+15 must be ≤ 64
    → len(ns)+len(stage)+len(dag_id) ≤ 39.

    With ns=5, stage=4 (total 9, well under 25), dag_id must be ≤ 30 chars.
    Guard only fires for time-triggered DAGs (dag.schedule truthy, not asset-triggered).
    """
    # A project with config.py: its own naming applies, so the guard runs
    # before any AWS call (without config.py the platform's naming is
    # adopted first — see test_deploy_platform_discovery.py).
    mocker.patch("polyris.config._find_project_config", return_value="config.py")
    dag = mocker.MagicMock()
    # ns(5) + stage(4) + dag_id(31) = 40 > 39 → scheduler role = 65 chars
    dag.dag_id = "this-is-a-very-long-pipeline-id"  # 31 chars
    dag.schedule = "@daily"       # time-triggered → scheduler role IS created
    dag.is_asset_triggered = False
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "prod"
    cfg.for_stage.return_value = _stage_cfg(
        mocker,
        namespace="my-ns",
        stack_name="some-stack",
    )
    boto = mocker.patch("polyris.deploy.boto3")

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)  # deploy path: guards are active

    assert exc.value.code == 1
    boto.Session.assert_not_called()  # guard fires before any AWS work


def test_scheduler_role_guard_skipped_for_unscheduled_dag(mocker):
    """Guard 2 must NOT fire for unscheduled DAGs (no scheduler role is created)."""
    dag = mocker.MagicMock()
    dag.dag_id = "this-is-a-very-long-pipeline-id"  # 31 chars — would fail if guard fired
    dag.schedule = None           # no schedule → no scheduler role
    dag.is_asset_triggered = False
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "prod"
    cfg.for_stage.return_value = _stage_cfg(
        mocker,
        namespace="my-ns",
        stack_name="some-stack",
    )
    boto = mocker.patch("polyris.deploy.boto3")
    boto.Session.side_effect = SystemExit(2)  # guard passed → boto reached → arranged exit

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)

    assert exc.value.code == 2  # reached boto, not the scheduler-role guard


def test_scheduler_role_guard_skipped_for_asset_triggered_dag(mocker):
    """Guard 2 must NOT fire for asset-triggered DAGs (no EventBridge Scheduler role created)."""
    dag = mocker.MagicMock()
    dag.dag_id = "this-is-a-very-long-pipeline-id"  # 31 chars — would fail if guard fired
    dag.schedule = [object()]      # asset schedule object — truthy, but is_asset_triggered=True
    dag.is_asset_triggered = True  # asset-triggered → no scheduler role
    mocker.patch(
        "polyris.validation.validate_asl_from_dag",
        return_value=(True, [], []),
    )
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "prod"
    cfg.for_stage.return_value = _stage_cfg(
        mocker,
        namespace="my-ns",
        stack_name="some-stack",
    )
    boto = mocker.patch("polyris.deploy.boto3")
    boto.Session.side_effect = SystemExit(2)  # guard passed → boto reached → arranged exit

    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(dag)

    assert exc.value.code == 2  # reached boto, not the scheduler-role guard
