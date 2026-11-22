"""No console route scans the tasks table (ADR #137 §5).

The tasks table keeps a run's history for a year and 90 days, about ten times what
1.2 kept, so a scan of it grows with it and no longer fits the console's 30 seconds:
`/api/pipeline-status` and `/api/pipeline-dag` scanned it on every pipeline page's
first load, and the bell's search of backfill records on every poll. Every read of
task rows is now a query of an index or a get by key.

The census walks the console's code, and the paid overlay's when it is checked out
beside it, for every call that scans: a table's `scan`, a client's paginator of `scan`,
`scan_all` and a repository method that wraps one. Each one left is named below with why it is no scan of the
tasks table, or a bounded one.
"""
import ast
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

CONSOLE = Path(__file__).resolve().parents[1]
OVERLAY = CONSOLE.parents[3] / "polyris-ee" / "sam" / "lambdas" / "console_api" / "ee"

#: Every scan in the console's code, by file and function, with why it may stay.
ALLOWED = {
    ("utils.py", "scan_all"): "the helper itself",
    ("dal/assets_repo.py", "list_asset_names"): "the asset-events table, not the tasks table",
    ("dal/pipelines_repo.py", "list_all"): "the registry: one item per pipeline",
    ("dal/pipelines_repo.py", "count"): "the registry: one item per pipeline",
    ("dal/api_tokens_repo.py", "list_all"): "the API tokens table",
    ("dal/circuit_breakers_repo.py", "query_open"): "the circuit breakers table",
    ("dal/executions_repo.py", "discover_pipeline_names"):
        "one page of at most `limit` items, whatever the table's size: the pipelines list's fallback when the "
        "registry is empty (ADR #136 §2)",
    ("routes/pipelines_list.py", "list_pipelines"): "calls discover_pipeline_names, the bounded page above",
}
#: The calls that scan: a boto3 table's scan, the paging helper, and the repository's bounded page.
SCANS = {"scan", "scan_all", "discover_pipeline_names"}


def _sources():
    roots = [CONSOLE] + ([OVERLAY] if OVERLAY.is_dir() else [])
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "tests" not in path.parts and ".aws-sam" not in path.parts:
                yield root, path


def _scans():
    """(file, function) of every call in ``SCANS``; a call outside a function counts under the module."""
    found = set()
    for root, path in _sources():
        tree = ast.parse(path.read_text(), str(path))
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name == "get_paginator" and any(isinstance(a, ast.Constant) and a.value == "scan" for a in node.args):
                name = "scan"           # a client's paginator of scan: the review of TR-6d planted one unseen
            if name not in SCANS:
                continue
            containing = [f for f in functions if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)]
            owner = max(containing, key=lambda f: f.lineno).name if containing else "<module>"
            prefix = "ee/" if root == OVERLAY else ""
            found.add((prefix + str(path.relative_to(root)), owner))
    return found


def test_no_route_scans_the_tasks_table():
    """Every scan left is one ALLOWED names; a new one fails here until it is a query, or named with its reason."""
    assert _scans() - set(ALLOWED) == set()


def test_the_census_sees_the_scans_it_allows():
    """A guard that matches nothing guards nothing: each allowed scan is still where it says."""
    assert _scans() >= set(ALLOWED)


def test_the_census_sees_a_client_paginator_of_scan(tmp_path, monkeypatch):
    """Review of TR-6d: a route that scanned through ``get_paginator("scan")`` passed the census unseen."""
    route = tmp_path / "routes" / "planted.py"
    route.parent.mkdir()
    route.write_text("def planted(client):\n    return client.get_paginator('scan').paginate(TableName='t')\n")
    monkeypatch.setitem(globals(), "_sources", lambda: iter([(tmp_path, route)]))
    assert _scans() == {("routes/planted.py", "planted")}


@pytest.fixture
def tokens(mocker):
    from tests.runs_kit import open_tables

    with mock_aws():
        yield open_tables(mocker, boto3.resource("dynamodb", region_name="us-east-1"))


def test_the_one_scan_reads_one_page_of_task_rows_alone(tokens, mocker):
    from dal import executions_repo

    for item in ({"execution_name": "extract-2026-11-22-run", "pipeline_name": "orders"},
                 {"execution_name": "_run_orders-run-2026-11-22-abcd1234", "pipeline_name": "orders-run-row"},
                 {"execution_name": "bf-1234abcd", "pipeline_name": "_polyris_bulk_backfill",
                  "record_type": "backfill"}):
        tokens.put_item(Item=item)
    assert executions_repo.discover_pipeline_names() == ["orders"]
    scan = mocker.patch.object(type(executions_repo), "table", new_callable=mocker.PropertyMock)
    scan.return_value.scan.return_value = {"Items": []}
    executions_repo.discover_pipeline_names(limit=7)
    assert scan.return_value.scan.call_args.kwargs["Limit"] == 7
    assert "ExclusiveStartKey" not in scan.return_value.scan.call_args.kwargs
