"""tests/e2e/run_coordination_smoke.py against the platform template it relies on.

The runner reads the stack's outputs, derives the test state machines' ARN prefix
from TestQuickSfnArn, and signs in with ADMIN_USER_PASSWORD_AUTH, which needs IAM
(cognito-idp:AdminInitiateAuth). These pin each of those against sam/template.yaml
(Principle #28), and that the temporary console user never outlives the run.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cfnlint.decode import decode

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "tests" / "e2e" / "run_coordination_smoke.py"


def _runner():
    spec = importlib.util.spec_from_file_location("run_coordination_smoke", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _template() -> dict:
    template, matches = decode(str(REPO_ROOT / "sam" / "template.yaml"))
    assert template is not None, matches
    return template


def _of_type(template: dict, kind: str) -> list:
    return [r["Properties"] for r in template["Resources"].values() if r.get("Type") == kind]


def test_the_console_client_allows_admin_sign_in_but_not_a_bare_password():
    """ADMIN_USER_PASSWORD_AUTH needs IAM; USER_PASSWORD_AUTH would let anyone try passwords."""
    [client] = _of_type(_template(), "AWS::Cognito::UserPoolClient")
    flows = set(client["ExplicitAuthFlows"])
    assert "ALLOW_ADMIN_USER_PASSWORD_AUTH" in flows
    assert "ALLOW_USER_PASSWORD_AUTH" not in flows


def test_the_platform_outputs_what_the_runner_reads():
    runner = _runner()
    outputs = _template()["Outputs"]
    missing = [key for key in (*runner.REQUIRED_OUTPUTS, *runner.COGNITO_OUTPUTS) if key not in outputs]
    assert not missing, missing


def test_the_test_state_machines_share_the_prefix_the_runner_derives():
    """The runner cuts TestQuickSfnArn at -test-quick; dag.py appends each test machine's suffix."""
    runner = _runner()
    resources = _template()["Resources"]
    names = {logical: resources[logical]["Properties"]["StateMachineName"]["Fn::Sub"]
             for logical in ("TestQuickSfn", "TestSuccessSfn", "TestFailureSfn")}
    assert names["TestQuickSfn"].endswith(runner.TEST_QUICK_SUFFIX)
    prefix = names["TestQuickSfn"][: -len(runner.TEST_QUICK_SUFFIX)]
    assert names == {"TestQuickSfn": prefix + "-test-quick", "TestSuccessSfn": prefix + "-test-success",
                     "TestFailureSfn": prefix + "-test-failure"}
    smoke_dag = (REPO_ROOT / "tests" / "e2e" / "coordination_smoke" / "dag.py").read_text()
    for suffix in ("-test-quick", "-test-success", "-test-failure"):
        assert '{_PREFIX}' + suffix in smoke_dag, suffix


def test_sfn_prefix_strips_the_quick_suffix():
    runner = _runner()
    arn = "arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris-test-quick"
    assert runner.sfn_prefix(arn) == "arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris"
    with pytest.raises(SystemExit):
        runner.sfn_prefix("arn:aws:states:us-east-1:123456789012:stateMachine:other")


def test_the_temporary_user_is_deleted_even_when_sign_in_fails(mocker):
    runner = _runner()
    cognito = mocker.MagicMock()
    cognito.admin_initiate_auth.side_effect = RuntimeError("sign-in refused")
    with pytest.raises(RuntimeError):
        with runner.temporary_sign_in(cognito, "pool", "client"):
            pass  # pragma: no cover - never reached
    created = cognito.admin_create_user.call_args.kwargs["Username"]
    cognito.admin_delete_user.assert_called_once_with(UserPoolId="pool", Username=created)


def test_the_temporary_user_signs_in_with_the_admin_flow_and_is_deleted(mocker):
    runner = _runner()
    cognito = mocker.MagicMock()
    cognito.admin_initiate_auth.return_value = {"AuthenticationResult": {"IdToken": "id-token"}}
    with runner.temporary_sign_in(cognito, "pool", "client") as token:
        assert token == "id-token"
        cognito.admin_delete_user.assert_not_called()
    assert cognito.admin_initiate_auth.call_args.kwargs["AuthFlow"] == "ADMIN_USER_PASSWORD_AUTH"
    created = cognito.admin_create_user.call_args.kwargs["Username"]
    cognito.admin_delete_user.assert_called_once_with(UserPoolId="pool", Username=created)
