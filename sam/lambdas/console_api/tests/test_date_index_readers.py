"""Every read of the two date indexes skips internal rows and backfill records (ADR #137 §5).

Backfill records carry a ``date``, the day they started, so that the search of
them can read ``pipeline-date-index``; that puts them in ``date-pipeline-index``
too. A reader that lists them as task rows shows a failed backfill as a failed
task, in the bell among others. So every read of those indexes goes through
``executions_repo``, whose readers drop such rows with ``should_skip_token_row``:

- the census below finds the index names in the console's code, and in the paid
  overlay when it is checked out next to it; only the repositories may name them;
- every reader of ``executions_repo`` that names one is listed in ``READS`` and
  run against moto, with the tokens table of sam/template.yaml, over a date that
  holds a task row, a backfill record and internal rows;
- the bell's read of failures went to the attention index (ADR #137 §6), which a
  backfill record never enters; a check below keeps it so.
"""
import ast
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

from polyris.coordination import row_write

_KIT_PATH = Path(__file__).resolve().parents[2] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

CONSOLE = Path(__file__).resolve().parents[1]
OVERLAY = CONSOLE.parents[3] / "polyris-ee" / "sam" / "lambdas" / "console_api" / "ee"
#: The files that may name a date index: the repository of task rows, and the search of backfill records.
READERS = {CONSOLE / "dal" / "executions_repo.py", CONSOLE / "dal" / "backfills_repo.py"}
DATE = datetime.now(timezone.utc).date().isoformat()
RUN = f"orders-run-{DATE}-abcd1234"
TASK = f"load-{DATE}-{RUN[-20:]}"


def _date_indexes():
    from dal.executions_repo import DATE_INDEXES

    return DATE_INDEXES


def _sources():
    roots = [CONSOLE] + ([OVERLAY] if OVERLAY.is_dir() else [])
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "tests" not in path.parts and ".aws-sam" not in path.parts:
                yield path


def _named(tree: ast.AST, names) -> list:
    """The lines where a string other than a docstring or a bare statement equals one of ``names``."""
    prose = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in names and id(node) not in prose]


def test_only_the_repositories_name_a_date_index():
    found = [f"{path.relative_to(path.parents[2])}:{line}" for path in _sources() if path not in READERS
             for line in _named(ast.parse(path.read_text(), str(path)), _date_indexes())]
    assert found == []


def test_the_census_sees_the_repository_s_reads():
    path = CONSOLE / "dal" / "executions_repo.py"
    assert _named(ast.parse(path.read_text(), str(path)), _date_indexes())


# ── every reader of executions_repo that names a date index ─────────────────

#: Each reader, with a call that reads the date of ``DATE`` and gives back its rows.
READS = {
    "query_by_date": lambda repo: repo.query_by_date(DATE),
    "query_runs_by_date": lambda repo: repo.query_runs_by_date(DATE, min_rows=10),
    "query_by_date_raw": lambda repo: repo.query_by_date_raw(KeyConditionExpression=Key("date").eq(DATE))["Items"],
    "rows_of_pipeline_date": lambda repo: repo.rows_of_pipeline_date("orders", DATE),
}


def _readers_of_the_date_indexes() -> set:
    path = CONSOLE / "dal" / "executions_repo.py"
    tree = ast.parse(path.read_text(), str(path))
    [repo] = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExecutionsRepo"]
    return {method.name for method in repo.body if isinstance(method, ast.FunctionDef)
            and (_named(method, _date_indexes())
                 or any(isinstance(n, ast.Name) and n.id == "DATE_INDEXES" for n in ast.walk(method)))}


def test_every_reader_of_a_date_index_is_checked_below():
    assert _readers_of_the_date_indexes() == set(READS)


@pytest.fixture
def tokens(mocker):
    import config
    from dal import backfills_repo, executions_repo

    logical, name = kit.TABLES["TOKENS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(config, "_dynamodb", resource)
        mocker.patch.object(executions_repo, "_table_name", name)
        mocker.patch.object(backfills_repo, "_table_name", name)
        table = resource.Table(name)
        now = datetime.now(timezone.utc).isoformat()
        failed_entry = row_write.entry_after("failed", None, datetime.now(timezone.utc), TASK)
        for item in (
            # A failed task row, with the attention entry its writer gives it, which the bell reads (ADR #137 §6).
            {"execution_name": TASK, "task_name": "load", "pipeline_execution": RUN, "pipeline_name": "orders",
             "date": DATE, "task_date": DATE, "status": "failed", "error": "boom", "finished_at": now,
             "attention": failed_entry[0], "attention_at": failed_entry[1]},
            # A backfill record of the day, failed: it is no task.
            {"execution_name": "bf-1234abcd", "backfill_id": "bf-1234abcd", "pipeline_name": "_polyris_bulk_backfill",
             "record_type": "backfill", "date": DATE, "status": "failed", "started_at": now, "finished_at": now,
             "target_pipeline": "orders"},
            # Internal rows that carry a date: a 1.2 warning, a date's output, a run's DAG snapshot.
            {"execution_name": f"_notify_warn_notify_{TASK}", "pipeline_name": "orders", "date": DATE,
             "status": "failed"},
            {"execution_name": f"output#orders#load#{DATE}", "pipeline_name": "orders", "date": DATE,
             "status": "failed"},
            {"execution_name": f"dag_snapshot::{RUN}", "pipeline_name": "orders", "date": DATE, "status": "failed"},
        ):
            table.put_item(Item=item)
        yield table


@pytest.mark.parametrize("reader", sorted(READS))
def test_a_reader_gives_back_the_task_rows_alone(tokens, reader):
    from dal import executions_repo

    assert [row["execution_name"] for row in READS[reader](executions_repo)] == [TASK]


def test_a_projection_still_lets_the_reader_drop_them(tokens):
    from dal import executions_repo

    rows = executions_repo.query_by_date(DATE, projection="#s", expr_names={"#s": "status"})
    assert [row["status"] for row in rows] == ["failed"]


def test_the_other_date_index_through_the_raw_read(tokens):
    from dal import executions_repo

    page = executions_repo.query_by_date_raw(IndexName="pipeline-date-index",
                                             KeyConditionExpression=Key("pipeline_name").eq("_polyris_bulk_backfill"))
    assert page["Items"] == []


def test_a_count_of_a_date_index_is_refused(tokens):
    """DynamoDB's count would include the backfill records; a caller counts the rows it gets back."""
    from dal import executions_repo

    with pytest.raises(ValueError):
        executions_repo.query_by_date_raw(KeyConditionExpression=Key("date").eq(DATE), Select="COUNT")


def test_the_raw_read_reads_the_date_indexes_only(tokens):
    from dal import executions_repo

    with pytest.raises(ValueError):
        executions_repo.query_by_date_raw(IndexName="backfill-id-index",
                                          KeyConditionExpression=Key("backfill_id").eq("bf-1234abcd"))


def test_the_bell_shows_a_failed_backfill_as_a_backfill_not_as_a_failed_task(tokens):
    """The bell reads failures from the attention index (ADR #137 §6), and a backfill record has no entry there."""
    from routes import notifications

    body = json.loads(notifications.get_notifications({"queryStringParameters": {"limit": "50"}})["body"])
    assert sorted((n["type"], n.get("task_name") or n.get("backfill_id")) for n in body["notifications"]) == [
        ("backfill", "bf-1234abcd"), ("failure", "load")]


def test_the_health_check_counts_task_failures_alone(tokens):
    from routes import health

    assert health._check_recent_failures()["failures_last_hour"] == 1
