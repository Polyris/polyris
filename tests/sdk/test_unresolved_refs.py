"""A resource reference that holds ``${...}`` is refused before any AWS call.

Since 1.3 a task's ARN or name reaches its run as written, in the task's spec: nothing substitutes a
``${...}`` in it. The pre-deploy check skipped such a reference as "resolved at CloudFormation deploy
time", so the pipeline deployed and its task failed at its first run.
"""
from __future__ import annotations

import pytest

from polyris import DAG, task
from polyris.resource_check import check_resource_references, unresolved_refs
from polyris.validation import validate_asl_from_dag

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:etl"


def _dag(**decorated) -> DAG:
    kind, kwargs = next(iter(decorated.items()))
    with DAG("reports", schedule=None) as dag:
        @getattr(task, kind)(**kwargs)
        def load():
            pass
        load()
    return dag


@pytest.mark.parametrize("decorated", [
    {"sfn": {"arn": "${workflow_arn}"}},
    {"lambda_function": {"function_name": "${loader}"}},
    {"glue_job": {"job_name": "${JobName}"}},
    {"sfn": {"arn": "${workflow_arn}", "role": "arn:aws:iam::444455556666:role/etl"}},
])
def test_validate_refuses_a_reference_with_a_placeholder(decorated):
    valid, errors, _ = validate_asl_from_dag(_dag(**decorated))

    assert not valid
    assert any(e.startswith("Task 'load': ") and "nothing substitutes" in e for e in errors), errors


def test_a_real_reference_passes():
    assert validate_asl_from_dag(_dag(sfn={"arn": ARN}))[0]
    assert unresolved_refs(_dag(sfn={"arn": ARN})) == []


def test_the_existence_check_refuses_it_without_an_aws_call(mocker):
    session = mocker.MagicMock()

    result = check_resource_references(_dag(sfn={"arn": "${workflow_arn}"}), session, "us-east-1")

    assert [r.ref for r in result.errors] == ["${workflow_arn}"]
    session.client.assert_not_called()
