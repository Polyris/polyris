"""Which keys of the tasks table name task rows (ADR #137 §3, invariant 11).

A pass over a run whose run row is missing names the run's rows through
``task_row.RUN_ROWS_INDEX``, which also files the platform's own rows of the
run; only task rows are stopped.
"""

import pytest

from polyris.coordination import records, run_row, task_row

RUN = "orders-run-2026-11-21-abcd1234"


@pytest.mark.parametrize("key", [task_row.key("load", "2026-11-21", RUN), "load-2026-11-21T10-x"])
def test_a_task_rows_key_names_a_task_row(key):
    assert task_row.is_task_key(key)


@pytest.mark.parametrize("key", [
    run_row.key(RUN),                                     # the run row
    run_row.input_key(RUN),                               # the run's input
    f"{records.WARNING_PREFIX}coordination_load",         # a warning
    "output#orders#load#2026-11-21",                      # a store
    "input#orders#load#2026-11-21",
    f"{records.DAG_SNAPSHOT_PREFIX}{RUN}",                # a 1.2 run's DAG snapshot
])
def test_the_platforms_own_rows_are_no_task_rows(key):
    assert not task_row.is_task_key(key)


def test_the_index_of_a_runs_rows_is_keyed_by_its_execution():
    """The template names it; the dispatcher's role may query it (tests/sdk, sam/lambdas/coordination/tests)."""
    from sam_template import load_template

    template = load_template(tagged=False)
    indexes = template["Resources"]["PipelineTokensTable"]["Properties"]["GlobalSecondaryIndexes"]
    [index] = [i for i in indexes if i["IndexName"] == task_row.RUN_ROWS_INDEX]
    assert index["KeySchema"] == [{"AttributeName": task_row.PIPELINE_EXECUTION, "KeyType": "HASH"}]
