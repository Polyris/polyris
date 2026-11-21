"""``polyris-validate --all`` catches every limit of ADR #137 §8 (D106): step 0 of the 1.3 upgrade.

Each pipeline folder runs in its own process, as ``polyris-deploy --all`` deploys
them; every refusal names the limit and the size it measured. A ``DAG`` or task
field that a release removes fails the file's load, which ``--all`` reports with
the field's name; the fields D79 removes are removed in TR-8.
"""

import json
import sys
import textwrap
from pathlib import Path

import pytest

from polyris.coordination import task_spec
from polyris.validation import main

HEAD = """
    from polyris import DAG, Asset, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
"""

PIPELINES = {
    "many_outlets": HEAD + f"""
    with DAG("many-outlets", schedule=None) as dag:
        @task.sfn(arn=ARN, outlets=[Asset(f"s3://lake/a{{i}}/") for i in range({task_spec.MAX_OUTLETS + 1})])
        def wide():
            pass
        wide()
    """,
    "long_id": HEAD + """
    with DAG("long-id", schedule=None) as dag:
        @task.sfn(arn=ARN, task_id="t" * 129)
        def long():
            pass
        long()
    """,
    "many_tasks": HEAD + f"""
    with DAG("many-tasks", schedule=None) as dag:
        for i in range({task_spec.MAX_TASKS + 1}):
            task.sfn(arn=ARN, task_id=f"t{{i}}")(lambda: None)()
    """,
    "dupe_outlet": HEAD + """
    orders = Asset("s3://lake/orders/")
    with DAG("dupe-outlet", schedule=None) as dag:
        @task.sfn(arn=ARN, outlets=[orders, orders])
        def twice():
            pass
        twice()
    """,
    "removed_field": HEAD + """
    with DAG("removed-field", schedule=None, field_removed_by_a_release=True) as dag:
        pass
    """,
}


@pytest.fixture
def report(tmp_path, monkeypatch, capsys):
    for folder, body in PIPELINES.items():
        path = tmp_path / folder / "dag.py"
        path.parent.mkdir()
        path.write_text(textwrap.dedent(body).lstrip())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all", "--json"])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 1
    return "\n".join(json.loads(capsys.readouterr().out)["errors"])


def test_validate_all_reports_each_limit_with_its_size(report):
    assert f"has {task_spec.MAX_OUTLETS + 1} outlets, over the limit of {task_spec.MAX_OUTLETS}" in report
    assert "its task_id takes 129 bytes, over the limit of 128 bytes" in report
    assert f"has {task_spec.MAX_TASKS + 1} tasks, over the limit of {task_spec.MAX_TASKS} tasks per pipeline" in report


def test_validate_all_reports_a_duplicate_outlet_and_a_removed_field(report):
    assert "name asset 'lake/orders' twice" in report
    assert "field_removed_by_a_release" in report


def test_every_folder_is_reported(report):
    for folder in PIPELINES:
        assert str(Path(folder) / "dag.py") in report
