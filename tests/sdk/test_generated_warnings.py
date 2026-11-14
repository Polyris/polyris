"""Warnings the generated pipeline state machine writes have the shape of warning_item (ADR #130 §9).

They carry no `date` and sit in the attention index, so the bell finds them by
write time even for a backfill of an old partition (B-N1). Each is evaluated
here from the generated JSONata, as Step Functions would.
"""
import json
from typing import Any, Dict, Iterator

import pytest

from polyris import DAG, Asset, Task
from polyris.coordination.records import warning_item
from polyris.generators import generate_step_function_json
from sfn_jsonata import NOW, evaluate, plain

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:user-load"
RUN = "orders-run-2026-10-13-abcdef12"


def _states(states: Dict[str, Any]) -> Iterator[Any]:
    for name, state in states.items():
        yield name, state
        for branch in state.get("Branches", []):
            yield from _states(branch["States"])
        processor = state.get("ItemProcessor") or state.get("Iterator")
        if processor:
            yield from _states(processor["States"])


@pytest.fixture(scope="module")
def states() -> Dict[str, Any]:
    dag = DAG("orders", schedule=[Asset("lake/prices")])
    dag.add_task(Task(task_id="load", arn=ARN))
    definition = json.loads(generate_step_function_json(dag, wrapper_arn="arn:wrapper", tokens_table="tokens"))
    return dict(_states(definition["States"]))


@pytest.mark.parametrize("state,subject,task_name,error_input", [
    ("Warn_Registration_Failed", f"{RUN}-register_pipeline", "Register_Pipeline",
     {"_registration_error": {"Error": "DynamoDB.AccessDeniedException"}}),
    ("Warn_Snapshot_Failed", f"{RUN}-save_dag_snapshot", "Save_DAG_Snapshot",
     {"_snapshot_error": {"Error": "DynamoDB.AccessDeniedException"}}),
    ("Warn_WriteSubscription_Failed", f"{RUN}-write_subscription-lake/prices", "WriteSubscription",
     {"_asset_name": "lake/prices", "_subscription_error": {"Error": "DynamoDB.AccessDeniedException"}}),
])
def test_a_generated_warning_has_the_shape_of_warning_item(states, state, subject, task_name, error_input):
    written = plain(evaluate(states[state]["Arguments"]["Item"], input={"current_date": "2026-10-13", **error_input},
                             context={"Execution": {"Name": RUN}}))
    expected = warning_item(kind="coordination", execution_name=subject, task_name=task_name, pipeline_name="orders",
                            pipeline_execution=RUN, task_date="2026-10-13", error=written["error"], now=NOW)
    assert written == expected
    assert "AccessDeniedException" in written["error"]
    assert "date" not in written
