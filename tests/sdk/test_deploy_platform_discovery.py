"""polyris-deploy finds the platform stack without config.py (ADR #131, D20).

CloudFormation is faked at the boto3 client boundary (moto's CloudFormation
backend needs extra dependencies, and discovery only reads names and tags).
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from polyris import deploy as deploy_mod
from polyris.platform import PLATFORM_TAG_KEY, PLATFORM_TAG_VALUE

REGION = "eu-central-1"


class _Session:
    """Minimal boto3.Session stand-in: one CloudFormation client, paginated describe_stacks."""

    def __init__(self):
        self.stacks: List[Dict[str, Any]] = []

    def client(self, name):
        assert name == "cloudformation"
        stacks = self.stacks

        class _Cfn:
            def get_paginator(self, op):
                assert op == "describe_stacks"

                class _P:
                    def paginate(self):
                        # two pages, to prove every page is read
                        return [{"Stacks": stacks[:1]}, {"Stacks": stacks[1:]}]
                return _P()
        return _Cfn()


@pytest.fixture
def session():
    return _Session()


def _stack(session, name: str, platform: bool) -> None:
    tags = [{"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE}] if platform else []
    session.stacks.append({"StackName": name, "Tags": tags})


def test_no_platform_stack(session):
    _stack(session, "sam-deployed", platform=False)
    assert deploy_mod._discover_platform_stack(session, REGION) == (None, None)


def test_single_platform_stack_is_used(session, capsys):
    _stack(session, "sam-deployed", platform=False)
    _stack(session, "acme-dev", platform=True)
    name, stack = deploy_mod._discover_platform_stack(session, REGION)
    assert name == "acme-dev" and stack["StackName"] == "acme-dev"
    assert "acme-dev" in capsys.readouterr().out


def test_several_platform_stacks_exit_and_ask_for_stack(session, capsys):
    _stack(session, "acme-dev", platform=True)
    _stack(session, "acme-prod", platform=True)
    with pytest.raises(SystemExit) as exc:
        deploy_mod._discover_platform_stack(session, REGION)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "acme-dev, acme-prod" in out and "--stack" in out


class _StageCfg:
    def __init__(self, region=None):
        self._region = region

    def get(self, key, default=None):
        return self._region if key == "region" else default


@pytest.mark.parametrize("cli,env,config,expected", [
    ("eu-west-1", "us-west-2", "ap-south-1", "eu-west-1"),
    (None, "us-west-2", "ap-south-1", "us-west-2"),
    (None, None, "ap-south-1", "ap-south-1"),
    (None, None, None, None),  # → the AWS session falls back to the profile's region
])
def test_explicit_region_priority(monkeypatch, cli, env, config, expected):
    monkeypatch.delenv("POLYRIS_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    if env:
        monkeypatch.setenv("AWS_REGION", env)
    assert deploy_mod._explicit_region(cli, _StageCfg(config)) == expected


def test_fail_message_points_at_polyris_up(mocker, capsys):
    mocker.patch("polyris.config._find_project_config", return_value=None)
    with pytest.raises(SystemExit):
        deploy_mod._fail_no_stack_name("dev")
    assert "polyris-up" in capsys.readouterr().out


# ── Without config.py, pipelines take namespace/stage from the platform ─────

_PLATFORM = {"StackName": "pupsmoke-dev", "Parameters": [
    {"ParameterKey": "Namespace", "ParameterValue": "pupsmoke"},
    {"ParameterKey": "Stage", "ParameterValue": "prod"},
]}


def test_platform_naming_adopts_platform_values(monkeypatch):
    """Found in the live smoke: defaulting to namespace 'polyris' would let two
    platforms in one account share pipeline stack names."""
    monkeypatch.delenv("POLYRIS_NAMESPACE", raising=False)
    monkeypatch.delenv("POLYRIS_STAGE", raising=False)
    assert deploy_mod._platform_naming(_PLATFORM, namespace="polyris", stage="dev", cli_stage=None) == (
        "pupsmoke", "prod")


def test_platform_naming_respects_explicit_choices(monkeypatch):
    monkeypatch.setenv("POLYRIS_NAMESPACE", "mine")
    monkeypatch.delenv("POLYRIS_STAGE", raising=False)
    assert deploy_mod._platform_naming(_PLATFORM, namespace="mine", stage="dev", cli_stage="dev") == ("mine", "dev")
    monkeypatch.delenv("POLYRIS_NAMESPACE")
    monkeypatch.setenv("POLYRIS_STAGE", "qa")
    assert deploy_mod._platform_naming(_PLATFORM, namespace="polyris", stage="qa", cli_stage=None) == (
        "pupsmoke", "qa")


def test_platform_naming_without_parameters_keeps_defaults(monkeypatch):
    monkeypatch.delenv("POLYRIS_NAMESPACE", raising=False)
    assert deploy_mod._platform_naming({"StackName": "x"}, namespace="polyris", stage="dev", cli_stage=None) == (
        "polyris", "dev")


def test_stage_flag_must_match_the_discovered_platform(mocker, tmp_path, monkeypatch, capsys):
    """--stage prod with only a dev platform must not deploy prod pipelines onto dev's tables."""
    from polyris import DAG, task

    monkeypatch.chdir(tmp_path)  # no config.py anywhere above
    mocker.patch("polyris.config._find_project_config", return_value=None)
    mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
    session = _Session()
    _stack(session, "acme-dev", platform=True)
    session.stacks[0]["Parameters"] = [{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                       {"ParameterKey": "Stage", "ParameterValue": "dev"}]

    class _Sts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    real_client = session.client
    session.client = lambda name: _Sts() if name == "sts" else real_client(name)
    session.region_name = REGION
    boto = mocker.patch("polyris.deploy.boto3")
    boto.Session.return_value = session

    with DAG("orders", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:eu-central-1:123456789012:stateMachine:x")
        def t():
            pass
        t()

    with pytest.raises(SystemExit) as e:
        deploy_mod.deploy_pipeline(dag, stage="prod", region=REGION)
    assert e.value.code == 1
    assert "--stage prod" in capsys.readouterr().out


def test_explicit_stack_without_config_takes_that_platforms_naming(mocker, tmp_path, monkeypatch, capsys):
    """Review finding: with two platforms the user is told to pass --stack; the pipeline
    stack must then be named after THAT platform, or dev and prod pipelines collide."""
    from polyris import DAG, task

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POLYRIS_NAMESPACE", raising=False)
    monkeypatch.delenv("POLYRIS_STAGE", raising=False)
    mocker.patch("polyris.config._find_project_config", return_value=None)
    mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
    session = _Session()
    for name, stage in (("acme-dev", "dev"), ("acme-prod", "prod")):
        _stack(session, name, platform=True)
        session.stacks[-1]["Parameters"] = [{"ParameterKey": "Namespace", "ParameterValue": "acme"},
                                            {"ParameterKey": "Stage", "ParameterValue": stage}]

    class _Sts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    stacks = session.stacks

    class _Cfn:
        def describe_stacks(self, StackName):
            return {"Stacks": [s for s in stacks if s["StackName"] == StackName]}

    session.client = lambda name: _Sts() if name == "sts" else _Cfn()
    session.region_name = REGION
    mocker.patch("polyris.deploy.boto3").Session.return_value = session

    with DAG("orders", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:eu-central-1:123456789012:stateMachine:x")
        def t():
            pass
        t()

    with pytest.raises(SystemExit):  # the fake platform has no outputs; the summary is printed first
        deploy_mod.deploy_pipeline(dag, stack="acme-prod", region=REGION)
    out = capsys.readouterr().out
    assert "acme-prod-polyris-orders" in out and "polyris-dev-polyris-orders" not in out
