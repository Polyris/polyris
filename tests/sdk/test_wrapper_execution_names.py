"""Task ids that 1.2's execution names could not tell apart are refused (ADR #130 §6.6).

1.2 named a task's wrapper "{task id}-{date}-{run suffix}" in 80 characters,
cutting a long id to the budget that the date and the suffix leave. From 1.3 no
pipeline starts a wrapper (ADR #137 §1); the generator still refuses two ids
that the budget cannot tell apart, until run_task's names replace the check.
"""
from datetime import datetime

import pytest

from polyris import DAG, Task
from polyris.coordination.run_name import MAX_NAME, fit_task_id
from polyris.generators import _TASK_ID_BUDGET, _WRAPPER_NAME_TAIL, generate_step_function_json
from polyris.partitions import GRANULARITIES, MAX_PARTITION_KEY_LENGTH, _format_key

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
RUN = "orders-run-2026-11-12-abcdef12"


def _dag(*task_ids: str) -> DAG:
    dag = DAG("orders")
    for task_id in task_ids:
        dag.add_task(Task(task_id=task_id, arn=ARN))
    return dag


def test_ids_that_would_share_a_name_fail_generation():
    long_id = "load_" + "x" * 70
    dag = _dag(long_id, fit_task_id(long_id, _TASK_ID_BUDGET))

    with pytest.raises(ValueError, match="would start their wrappers under the same name"):
        generate_step_function_json(dag)


def test_two_long_ids_that_differ_only_at_the_end_are_told_apart():
    first, second = "load_" + "x" * 70 + "_eu", "load_" + "x" * 70 + "_us"

    assert fit_task_id(first, _TASK_ID_BUDGET) != fit_task_id(second, _TASK_ID_BUDGET)
    generate_step_function_json(_dag(first, second))


def test_the_budget_leaves_room_for_every_partition_key():
    keys = [_format_key(datetime(2026, 12, 31, 23), g) for g in GRANULARITIES]

    assert max(len(k) for k in keys) == MAX_PARTITION_KEY_LENGTH
    assert _TASK_ID_BUDGET + _WRAPPER_NAME_TAIL == MAX_NAME
    assert _WRAPPER_NAME_TAIL == len(f"-{max(keys, key=len)}-") + len(RUN[-8:])
