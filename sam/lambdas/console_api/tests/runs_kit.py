"""Run rows and task rows on a moto tokens table, for the console's run lists (ADR #137 §3, §5).

The table is the one ``sam/template.yaml`` declares, its indexes and projections
included (``coordination_testkit.create_tables``), so a read of ``run-start-index``
gets what the deployed index projects. The rows carry the fields their writers
write: ``Start_Run`` and the dispatcher for a run row, a task row of 1.3 or of 1.2.
"""
import importlib
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from polyris.coordination import run_row, task_row

_KIT_PATH = Path(__file__).resolve().parents[2] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

#: The clock of every run list in these tests.
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class Frozen(datetime):
    """``datetime`` whose ``now`` is ``NOW``; the rest (``fromisoformat``) is datetime's own."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def open_tables(mocker, resource) -> Any:
    """Create the tokens table and point the console's repositories at it; returns the table."""
    import boto3
    import config

    logical, name = kit.TABLES["TOKENS_TABLE"]
    kit.create_tables(resource, {logical: name})
    mocker.patch.object(config, "_dynamodb", resource)
    mocker.patch.object(config, "_dynamodb_client", boto3.client("dynamodb", region_name="us-east-1"))
    mocker.patch.object(importlib.import_module("dal.executions_repo").executions_repo, "_table_name", name)
    mocker.patch.object(importlib.import_module("dal.backfills_repo").backfills_repo, "_table_name", name)
    return resource.Table(name)


def freeze(mocker, *modules: str) -> None:
    """Fix ``datetime.now`` at ``NOW`` in ``feed`` and in each module named."""
    for module in ("feed", *modules):
        mocker.patch.object(importlib.import_module(module), "datetime", Frozen)


def iso(moment: datetime) -> str:
    """A time as the engine writes it (``times.format_utc``)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def put_run(table, pipeline: str, run_date: str, started: datetime, *, suffix: str = "abcd1234",
            summary: Optional[Dict[str, int]] = None, step_failure: Optional[Dict[str, str]] = None,
            finished: Optional[datetime] = None, triggered_by: str = "schedule", **fields: Any) -> str:
    """A run row as ``Start_Run`` writes it, with the summary and the answer a pass writes; its run's name."""
    pipeline_execution = f"{pipeline}-run-{run_date}-{suffix}"
    item: Dict[str, Any] = {
        run_row.TABLE_KEY: run_row.key(pipeline_execution), run_row.STATUS: run_row.ACTIVE,
        run_row.EXECUTION: pipeline_execution, run_row.PIPELINE: pipeline, run_row.RUN_DATE: run_date,
        run_row.TRIGGERED_BY: triggered_by, run_row.RUN_VERSION: 2, run_row.TASK_COUNT: sum((summary or {}).values()),
        **run_row.index_keys(started), **fields,
    }
    if summary is not None:
        item[run_row.SUMMARY] = summary
    if step_failure is not None:
        item[run_row.STEP_FAILURE] = step_failure
    if finished is not None:
        item[run_row.FINISHED_AT] = iso(finished)
    table.put_item(Item=item)
    return pipeline_execution


def put_task(table, pipeline_execution: str, task: str, date: str, status: str, *,
             started: Optional[datetime] = None, finished: Optional[datetime] = None, **fields: Any) -> str:
    """A task row of the run named ``pipeline_execution``; its key."""
    pipeline = pipeline_execution.split("-run-")[0]
    key = task_row.key(task, date, pipeline_execution)
    item: Dict[str, Any] = {
        task_row.TABLE_KEY: key, task_row.TASK_NAME: task, task_row.PIPELINE: pipeline,
        task_row.PIPELINE_EXECUTION: pipeline_execution, task_row.DATE: date, task_row.TASK_DATE: date,
        task_row.STATUS: status, **fields,
    }
    if started is not None:
        item[task_row.STARTED_AT] = iso(started)
    if finished is not None:
        item[task_row.FINISHED_AT] = iso(finished)
    table.put_item(Item=item)
    return key


def days_ago(n: int, hour: int = 6) -> datetime:
    """``n`` days before ``NOW``, at ``hour`` UTC."""
    return (NOW - timedelta(days=n)).replace(hour=hour, minute=0, second=0, microsecond=0)
