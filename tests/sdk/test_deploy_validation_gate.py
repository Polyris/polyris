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
    session.client.side_effect = lambda name: sts if name == "sts" else mocker.MagicMock()

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

    def _client(name):
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
