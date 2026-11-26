"""A task's ``role=`` of the platform's account: only a role under ``/polyris-tasks/`` (D11, PLAN_IAM 1.4.0 §6.4).

The orchestration role may assume a role of its own account only under that path, or the platform's default
task role, so ``polyris-validate`` (with ``account_id`` in ``config.py``) and ``polyris-deploy`` (after
``sts:GetCallerIdentity``) refuse any other with message C. A role in another account passes, as before.
A ``dag_id`` whose state machine would end like a platform machine's is refused too (§6.5).
"""
from __future__ import annotations

import pytest

from polyris import DAG, deploy, task
from polyris.platform import (
    PLATFORM_NAMES,
    TASK_ROLE_PATH,
    default_task_role_name,
    refused_task_role,
    reserved_machine,
    task_role_refusal,
)
from polyris.validation import same_account_role_errors, validate_asl_from_dag

ACCOUNT = "111111111111"
OTHER = "222222222222"
ARN = f"arn:aws:states:us-east-1:{ACCOUNT}:stateMachine:my-etl-job"


@pytest.mark.parametrize("role, refused", [
    (f"arn:aws:iam::{ACCOUNT}:role/etl-role", True),
    (f"arn:aws:iam::{ACCOUNT}:role/admin/OrganizationAdmin", True),
    (f"arn:aws:iam::{ACCOUNT}:role/acme-dev-polyris-orchestration-role", True),
    (f"arn:aws:iam::{ACCOUNT}:user/someone", True),
    (f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/finance-writer", False),
    (f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/team/finance-writer", False),
    (f"arn:aws:iam::{ACCOUNT}:role/acme-dev-polyris-default-task-role", False),
    (f"arn:aws:iam::{OTHER}:role/etl-role", False),
    ("same", False),
    (None, False),
])
def test_a_role_of_the_platforms_account_passes_only_under_the_tasks_path(role, refused):
    assert refused_task_role(role, account=ACCOUNT) is refused


def test_a_known_platform_passes_only_its_own_default_task_role():
    own = default_task_role_name("acme", "dev")
    assert own == "acme-dev-polyris-default-task-role"
    assert not refused_task_role(f"arn:aws:iam::{ACCOUNT}:role/{own}", account=ACCOUNT, default_task_role=own)
    assert refused_task_role(f"arn:aws:iam::{ACCOUNT}:role/beta-dev-polyris-default-task-role", account=ACCOUNT,
                             default_task_role=own)


def test_message_c_names_the_role_the_path_and_the_arn_to_use():
    message = task_role_refusal("load", f"arn:aws:iam::{ACCOUNT}:role/etl-role")
    assert message == (
        f"Task 'load': role arn:aws:iam::{ACCOUNT}:role/etl-role is in the platform's account, and polyris may use "
        f"only roles of this account under the path {TASK_ROLE_PATH}. Ask your platform admin to create the role "
        "under that path (docs: Give one pipeline its own permissions) and use its ARN, e.g. "
        f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/etl-role. Roles in other accounts work as before.")


def _dag(dag_id: str = "reports", role: str = "same") -> DAG:
    with DAG(dag_id, schedule=None) as dag:
        @task.sfn(arn=ARN, role=role)
        def load():
            pass
        load()
    return dag


@pytest.fixture
def account_in_config(mocker):
    get = mocker.patch("polyris.config.config.get")
    get.side_effect = lambda key, default=None: ACCOUNT if key == "account_id" else default


def test_validate_refuses_a_role_of_the_account_in_config(account_in_config):
    valid, errors, _ = validate_asl_from_dag(_dag(role=f"arn:aws:iam::{ACCOUNT}:role/etl-role"))
    assert not valid
    assert errors[0].startswith(f"Task 'load': role arn:aws:iam::{ACCOUNT}:role/etl-role is in the platform's")


@pytest.mark.parametrize("role", [f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/finance-writer",
                                  f"arn:aws:iam::{OTHER}:role/etl-role"])
def test_validate_passes_both_allowed_forms(account_in_config, role):
    assert validate_asl_from_dag(_dag(role=role))[0]


def test_validate_without_an_account_cannot_tell_and_passes():
    assert validate_asl_from_dag(_dag(role=f"arn:aws:iam::{ACCOUNT}:role/etl-role"))[0]


@pytest.mark.parametrize("dag_id", [*PLATFORM_NAMES.state_machines, "x-polyris-bulk-backfill"])
def test_a_dag_id_that_ends_like_a_platform_machine_is_refused(dag_id):
    assert reserved_machine(dag_id) is not None
    valid, errors, _ = validate_asl_from_dag(_dag(dag_id))
    assert not valid and any("Pick another dag_id" in e for e in errors)


@pytest.mark.parametrize("dag_id", ["bulk-backfill-report", "my-bulk-backfill", "test-quick-checks", "reports"])
def test_a_dag_id_that_only_contains_a_platform_machines_name_passes(dag_id):
    assert reserved_machine(dag_id) is None


def test_the_template_names_the_default_task_role_as_the_constant_does():
    from pathlib import Path
    template = (Path(__file__).resolve().parents[2] / "sam" / "template.yaml").read_text()
    assert 'RoleName: !Sub "${Namespace}-${Stage}-polyris-default-task-role"' in template
    assert default_task_role_name("${Namespace}", "${Stage}") == "${Namespace}-${Stage}-polyris-default-task-role"


def test_same_account_role_errors_resolve_a_config_roles_key(monkeypatch):
    monkeypatch.setenv("POLYRIS_ROLE_ETL", f"arn:aws:iam::{ACCOUNT}:role/etl-role")
    assert len(same_account_role_errors(_dag(role="etl"), account=ACCOUNT)) == 1


# ── polyris-deploy: the caller's account, and the platform's own default task role ───────────────────


def _platform(mocker, *, account: str = ACCOUNT):
    mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
    outputs = {"StartRunFunctionArn": "arn:aws:lambda:us-east-1:1:function:f",
               "OrchestrationRoleArn": f"arn:aws:iam::{ACCOUNT}:role/exec", "PipelineSpecsTable": "specs"}
    cfn = mocker.MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
        "Parameters": [{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                       {"ParameterKey": "Stage", "ParameterValue": "dev"}]}]}
    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": account}
    session = mocker.MagicMock()
    session.client.side_effect = lambda name, **_: {"sts": sts, "cloudformation": cfn}.get(name, mocker.MagicMock())
    mocker.patch("polyris.deploy.boto3").Session.return_value = session
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    stage = mocker.MagicMock(stack_name="acme-dev", namespace="acme", region="us-east-1", profile=None)
    stage.get = lambda key, default=None: default
    cfg.for_stage.return_value = stage
    mocker.patch("polyris.config._find_project_config", return_value="config.py")
    return mocker.patch("polyris.deploy.check_resource_references", side_effect=SystemExit(7))


def test_deploy_refuses_a_role_of_the_callers_account_before_anything_is_written(mocker, capsys):
    reached = _platform(mocker)
    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(_dag(role=f"arn:aws:iam::{ACCOUNT}:role/etl-role"), stage="dev")
    assert exc.value.code == 1
    assert not reached.called
    assert f"❌ Task 'load': role arn:aws:iam::{ACCOUNT}:role/etl-role is in the platform's account" in \
        capsys.readouterr().out


@pytest.mark.parametrize("role", [f"arn:aws:iam::{ACCOUNT}:role/polyris-tasks/finance-writer",
                                  f"arn:aws:iam::{OTHER}:role/etl-role",
                                  f"arn:aws:iam::{ACCOUNT}:role/acme-dev-polyris-default-task-role"])
def test_deploy_passes_the_allowed_roles(mocker, role):
    reached = _platform(mocker)
    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(_dag(role=role), stage="dev")
    assert exc.value.code == 7 and reached.called, "past the role check"


def test_deploy_refuses_another_platforms_default_task_role(mocker):
    _platform(mocker)
    with pytest.raises(SystemExit) as exc:
        deploy.deploy_pipeline(_dag(role=f"arn:aws:iam::{ACCOUNT}:role/beta-dev-polyris-default-task-role"),
                               stage="dev")
    assert exc.value.code == 1
