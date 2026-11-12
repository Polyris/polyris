"""`polyris-init <name> --hello` — a runnable first pipeline on the installed platform (ADR #131, D22).

STS runs on moto; CloudFormation is a fake client at the boto3 boundary
(moto's CloudFormation backend needs extra dependencies, and the lookup only
reads stack tags and outputs).
"""
from __future__ import annotations

import sys

import boto3
import pytest
from moto import mock_aws

from polyris.deploy import _load_dag_from_file
from polyris.init import HELLO_PIPELINE, init_hello_pipeline, main
from polyris.platform import OUTPUT_TEST_SFN_ARN, PLATFORM_TAG_KEY, PLATFORM_TAG_VALUE
from polyris.validation import validate_asl_from_dag

ARN = "arn:aws:states:eu-central-1:123456789012:stateMachine:acme-dev-polyris-test-quick"


def test_hello_pipeline_is_a_valid_dag(tmp_path, capsys):
    init_hello_pipeline("hello-world", arn=ARN, stack="acme-dev", base_dir=str(tmp_path))

    dag_file = tmp_path / "hello-world" / "dag.py"
    (dag,) = _load_dag_from_file(dag_file)
    assert dag.dag_id == "hello-world" and dag.schedule is None
    is_valid, errors, _ = validate_asl_from_dag(dag)
    assert is_valid, errors
    assert ARN in dag_file.read_text() and "REGION" not in dag_file.read_text()
    assert "polyris-deploy" in capsys.readouterr().out


def test_hello_refuses_existing_directory(tmp_path):
    (tmp_path / "hello-world").mkdir()
    with pytest.raises(SystemExit) as e:
        init_hello_pipeline("hello-world", arn=ARN, stack="s", base_dir=str(tmp_path))
    assert e.value.code == 1


def test_template_names_its_inputs():
    assert all(p in HELLO_PIPELINE for p in ("{name}", "{arn}", "{stack}"))


# ── CLI: finds the platform through AWS ──────────────────────────────────────


class _Cfn:
    def __init__(self, stacks):
        self.stacks = stacks

    def get_paginator(self, _op):
        stacks = self.stacks

        class _P:
            def paginate(self):
                return [{"Stacks": stacks}]
        return _P()

    def describe_stacks(self, StackName):
        return {"Stacks": [s for s in self.stacks if s["StackName"] == StackName]}


@pytest.fixture
def aws(monkeypatch, mocker):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    holder = {"stacks": []}
    real_client = boto3.Session.client

    def client(self, service, *a, **kw):
        if service == "cloudformation":
            return _Cfn(holder["stacks"])
        return real_client(self, service, *a, **kw)

    mocker.patch.object(boto3.Session, "client", client)
    with mock_aws():
        yield holder


def _platform(outputs):
    return {"StackName": "acme-dev", "Tags": [{"Key": PLATFORM_TAG_KEY, "Value": PLATFORM_TAG_VALUE}],
            "Outputs": outputs}


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["polyris-init", *argv])
    main()


def test_cli_hello_uses_the_platform_test_sfn(aws, tmp_path, monkeypatch):
    aws["stacks"] = [_platform([{"OutputKey": OUTPUT_TEST_SFN_ARN, "OutputValue": ARN}])]
    _run(monkeypatch, "hello-world", "--hello", "--region", "eu-central-1", "--dir", str(tmp_path))
    assert ARN in (tmp_path / "hello-world" / "dag.py").read_text()


def test_cli_hello_repeats_explicit_target_in_next_step(aws, tmp_path, monkeypatch, capsys):
    aws["stacks"] = [_platform([{"OutputKey": OUTPUT_TEST_SFN_ARN, "OutputValue": ARN}])]
    _run(monkeypatch, "hello-world", "--hello", "--stack", "acme-dev", "--region", "eu-central-1",
         "--dir", str(tmp_path))
    assert "polyris-deploy --stack acme-dev --region eu-central-1" in capsys.readouterr().out


def test_cli_hello_without_platform_explains(aws, tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "hello-world", "--hello", "--region", "eu-central-1", "--dir", str(tmp_path))
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "No Polyris platform stack" in out and "polyris-up" in out
    assert not (tmp_path / "hello-world").exists()


def test_cli_hello_stack_without_output(aws, tmp_path, monkeypatch, capsys):
    aws["stacks"] = [_platform([])]
    with pytest.raises(SystemExit):
        _run(monkeypatch, "hello-world", "--hello", "--region", "eu-central-1", "--dir", str(tmp_path))
    assert OUTPUT_TEST_SFN_ARN in capsys.readouterr().out


def test_cli_hello_needs_a_name(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "--hello", "--dir", str(tmp_path))
    assert e.value.code == 2


def test_cli_hello_reports_aws_errors(aws, tmp_path, monkeypatch, mocker, capsys):
    from botocore.exceptions import ClientError

    mocker.patch.object(_Cfn, "get_paginator", side_effect=ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no ListStacks"}}, "DescribeStacks"))
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, "hello-world", "--hello", "--region", "eu-central-1", "--dir", str(tmp_path))
    assert e.value.code == 1
    assert "Could not look up the platform stack" in capsys.readouterr().out
