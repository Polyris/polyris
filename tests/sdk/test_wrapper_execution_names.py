"""Wrapper execution names keep their date and run suffix (ADR #130 §6.6).

A wrapper is started as "{task id}-{date}-{run suffix}", at most 80
characters. Truncating the whole string used to cut the date and the suffix of
a long task id, so runs of different dates (or two long ids) collided. The
names here are evaluated from the generated JSONata, as Step Functions would.
"""
import json
from datetime import datetime
from typing import Any, Dict, Iterator

import pytest

from polyris import DAG, Task
from polyris.coordination.run_name import MAX_NAME, fit_task_id
from polyris.generators import _TASK_ID_BUDGET, _WRAPPER_NAME_TAIL, generate_step_function_json
from polyris.partitions import GRANULARITIES, MAX_PARTITION_KEY_LENGTH, _format_key
from sfn_jsonata import evaluate

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
RUN = "orders-run-2026-11-12-abcdef12"


def _states(definition: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for state in definition["States"].values():
        yield state
        for branch in state.get("Branches", []):
            yield from _states(branch)


def _name(dag: DAG, task_id: str, date: str, run: str = RUN) -> str:
    definition = json.loads(generate_step_function_json(dag, wrapper_arn=ARN))
    part = fit_task_id(task_id, _TASK_ID_BUDGET)
    (expr,) = [s["Arguments"]["Name"] for s in _states(definition)
               if f"'{part}-'" in str(s.get("Arguments", {}).get("Name", ""))]
    return evaluate(expr, input={"variables": {"current_date": date}}, context={"Execution": {"Name": run}})


def _dag(*task_ids: str) -> DAG:
    dag = DAG("orders")
    for task_id in task_ids:
        dag.add_task(Task(task_id=task_id, arn=ARN))
    return dag


def test_a_short_task_id_is_kept_whole():
    assert _name(_dag("extract"), "extract", "2026-11-12") == "extract-2026-11-12-abcdef12"


def test_a_long_task_id_keeps_the_date_and_the_run_suffix():
    long_id = "load_" + "x" * 75
    dag = _dag(long_id)

    hourly = _name(dag, long_id, "2026-11-12T05")
    other_day = _name(dag, long_id, "2026-11-13T05")

    assert hourly.endswith("-2026-11-12T05-abcdef12")
    assert len(hourly) == MAX_NAME
    assert hourly.startswith(long_id[: _TASK_ID_BUDGET - 7])
    assert other_day != hourly


def test_two_long_ids_that_differ_only_at_the_end_get_different_names():
    first, second = "load_" + "x" * 70 + "_eu", "load_" + "x" * 70 + "_us"
    dag = _dag(first, second)

    assert _name(dag, first, "2026-11-12") != _name(dag, second, "2026-11-12")


def test_ids_that_would_share_a_name_fail_generation():
    long_id = "load_" + "x" * 70
    dag = _dag(long_id, fit_task_id(long_id, _TASK_ID_BUDGET))

    with pytest.raises(ValueError, match="would start their wrappers under the same name"):
        generate_step_function_json(dag, wrapper_arn=ARN)


def test_the_budget_leaves_room_for_every_partition_key():
    keys = [_format_key(datetime(2026, 12, 31, 23), g) for g in GRANULARITIES]

    assert max(len(k) for k in keys) == MAX_PARTITION_KEY_LENGTH
    assert _TASK_ID_BUDGET + _WRAPPER_NAME_TAIL == MAX_NAME
    assert _WRAPPER_NAME_TAIL == len(f"-{max(keys, key=len)}-") + len(RUN[-8:])
