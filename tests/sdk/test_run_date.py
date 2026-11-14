"""A run has one date, the same for every task (ADR #133, B-D1).

The date expressions are evaluated with jsonata-python, as the other template
tests do; the harness runs them end to end (tests/backend/coordination,
``test_a_scheduled_run_carries_the_day_it_was_scheduled_for``).
"""

from __future__ import annotations

import json
from typing import Any, Dict

import jsonata
import pytest

from polyris import DAG, task
from polyris.deploy import _generate_cfn_template
from polyris.generators import (
    JSONATA_DATE,
    JSONATA_PARTITION,
    SCHEDULED_TIME_FIELD,
    generate_step_function_json,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:user-load"
STARTED = "2026-11-14T00:01:00.123Z"           # the execution started just after midnight
FIRED_FOR = "2026-11-13T23:59:00Z"             # the time its schedule fired it for


def _evaluate(expression: str, run_input: Dict[str, Any]) -> Any:
    body = expression[2:-2].strip() if expression.startswith("{%") else expression
    query = jsonata.Jsonata(body)
    query.assign("states", {"input": run_input, "context": {"Execution": {"StartTime": STARTED}}})
    return query.evaluate({})


@pytest.mark.parametrize("run_input,expected", [
    ({"current_date": "2026-11-10"}, "2026-11-10"),                               # the console, a backfill
    ({"current_date": "2026-11-10", "variables": {"current_date": "2026-11-09"}}, "2026-11-09"),  # yours wins
    ({"triggered_by": "schedule", SCHEDULED_TIME_FIELD: FIRED_FOR}, "2026-11-13"),  # started late: still its day
    ({"current_date": None, SCHEDULED_TIME_FIELD: FIRED_FOR}, "2026-11-13"),         # a null is no date
    ({}, "2026-11-14"),                                                            # a bare start: the day it started
], ids=["given", "variable", "scheduled", "null", "bare"])
def test_the_run_date(run_input, expected):
    assert _evaluate(JSONATA_DATE, run_input) == expected


def test_the_partition_falls_back_to_the_run_date():
    assert _evaluate(JSONATA_PARTITION, {"PARTITION_ARG": "2026-W46"}) == "2026-W46"
    assert _evaluate(JSONATA_PARTITION, {"PARTITION_ARG": None, SCHEDULED_TIME_FIELD: FIRED_FOR}) == "2026-11-13"


def _nightly() -> DAG:
    with DAG("nightly-load", schedule="cron(59 23 * * ? *)", variables={"region": "eu"}) as dag:
        @task.sfn(arn=ARN)
        def load():
            pass
        load()
    return dag


def _define_inputs() -> Dict[str, Any]:
    asl = json.loads(generate_step_function_json(
        _nightly(), wrapper_arn=ARN, registry_table="registry", tokens_table="tokens",
        asset_subscriptions_table="asset-subs"))
    output: Dict[str, Any] = asl["States"]["Define_Inputs"]["Output"]
    return output


#: Each starter's run input, as it builds it, and the date the run must get.
STARTERS = {
    "schedule": ({"triggered_by": "schedule", SCHEDULED_TIME_FIELD: FIRED_FOR}, "2026-11-13"),
    "asset push": ({"triggered_by": "asset", "current_date": "2026-11-05", "PARTITION_ARG": "2026-11-05",
                    "variables": {"current_date": "2026-11-05", "PARTITION_ARG": "2026-11-05"}}, "2026-11-05"),
    "asset push before 1.2": ({"triggered_by": "asset",
                               "variables": {"current_date": "2026-11-05", "PARTITION_ARG": "2026-11-05"}}, "2026-11-05"),
    "console": ({"triggered_by": "console_run", "current_date": "2026-11-07", "PARTITION_ARG": "2026-11-07",
                 "variables": {"current_date": "2026-11-07", "PARTITION_ARG": "2026-11-07"}}, "2026-11-07"),
}


@pytest.mark.parametrize("starter", sorted(STARTERS))
def test_define_inputs_gives_each_starters_run_its_date(starter):
    """Its Output replaces the whole input, the declared variables included: a field it drops is lost
    for every task (B-D1). sam/lambdas/coordination/asset_fanout.py and routes/pipelines_actions.py
    build the push and console inputs; deploy.py the schedule's."""
    run_input, expected = STARTERS[starter]
    output = _define_inputs()
    assert _evaluate(output["current_date"], run_input) == expected
    assert _evaluate(output["PARTITION_ARG"], run_input) == expected
    assert _evaluate(output["triggered_by"], run_input) == run_input["triggered_by"]


def test_the_schedule_hands_each_run_the_time_it_fired_it_for():
    """Principle #28: deploy.py writes the field the run date reads; Scheduler fills in the placeholder."""
    template = _generate_cfn_template(
        _nightly(), namespace="acme", stage="prod", region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/x", wrapper_arn=ARN,
        registry_table="registry", tokens_table="tokens", asset_subscriptions_table="asset-subs")
    run_input = json.loads(template["Resources"]["PipelineSchedule"]["Properties"]["Target"]["Input"])
    assert run_input == {"triggered_by": "schedule", "schedule": "cron(59 23 * * ? *)",
                         SCHEDULED_TIME_FIELD: "<aws.scheduler.scheduled-time>"}
