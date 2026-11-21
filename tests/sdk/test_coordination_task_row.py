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
    import yaml
    from pathlib import Path

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = yaml.load((Path(__file__).resolve().parents[2] / "sam" / "template.yaml").read_text(), Loader=Loader)
    indexes = template["Resources"]["PipelineTokensTable"]["Properties"]["GlobalSecondaryIndexes"]
    [index] = [i for i in indexes if i["IndexName"] == task_row.RUN_ROWS_INDEX]
    assert index["KeySchema"] == [{"AttributeName": task_row.PIPELINE_EXECUTION, "KeyType": "HASH"}]


class _Pages:
    """A DynamoDB client whose index query answers page by page."""

    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0)


def test_the_rows_of_a_run_are_read_from_its_index_page_by_page_task_rows_only():
    load, report = task_row.key("load", "2026-11-21", RUN), task_row.key("report", "2026-11-21", RUN)
    client = _Pages({"Items": [{"execution_name": {"S": load}},
                               {"execution_name": {"S": f"{records.WARNING_PREFIX}coordination_load"}}],
                     "LastEvaluatedKey": {"execution_name": {"S": load}}},
                    {"Items": [{"execution_name": {"S": report}}]})
    assert task_row.run_rows(client, "tokens", RUN) == [load, report]
    first, second = client.calls
    assert first["IndexName"] == task_row.RUN_ROWS_INDEX and first["ExpressionAttributeValues"] == {
        ":run": {"S": RUN}}
    assert "ExclusiveStartKey" not in first and second["ExclusiveStartKey"] == {"execution_name": {"S": load}}
