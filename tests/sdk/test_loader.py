"""polyris.loader.load_pipeline — the one way every CLI runs a pipeline file (ADR #139).

Until 1.2.2 each CLI had its own loader, and three times a file passed polyris-validate and
then could not be deployed. These tests pin the loader's contract; the last one pins that no
CLI runs a pipeline file any other way.
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

from polyris.dag import DAG
from polyris.loader import PipelineLoadError, PipelineNotFoundError, load_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


def test_returns_every_dag_the_file_makes_in_order_with_or_without_with(tmp_path):
    f = _write(tmp_path / "dag.py", f"""
        from polyris import DAG, task
        template = DAG("template", schedule="@daily")
        with DAG("unbound", schedule="@daily"):
            @task.sfn(arn="{ARN}")
            def a(): pass
            a()
        with DAG("bound", schedule="@daily") as dag:
            pass
    """)
    loaded = load_pipeline(f)
    assert [d.dag_id for d in loaded.dags] == ["template", "unbound", "bound"]
    assert loaded.module.template is loaded.dags[0]


def test_a_dag_an_imported_module_makes_or_one_it_imports_is_not_this_files(tmp_path, monkeypatch):
    _write(tmp_path / "shared.py", """
        from polyris import DAG
        with DAG("shared", schedule="@daily") as shared_dag:
            pass
        with DAG("shared-unbound", schedule="@daily"):
            pass
    """)
    f = _write(tmp_path / "dag.py", """
        from shared import shared_dag  # noqa: F401
        from polyris import DAG
        with DAG("local", schedule="@daily"):
            pass
    """)
    monkeypatch.delitem(sys.modules, "shared", raising=False)
    assert [d.dag_id for d in load_pipeline(f).dags] == ["local"]


def test_the_files_folder_and_the_one_above_are_importable_only_while_it_runs(tmp_path, monkeypatch):
    _write(tmp_path / "project_settings.py", "SCHEDULE = '@daily'\n")
    _write(tmp_path / "orders" / "naming.py", "NAME = 'orders'\n")
    f = _write(tmp_path / "orders" / "dag.py", """
        from naming import NAME
        from project_settings import SCHEDULE
        from polyris import DAG
        with DAG(NAME, schedule=SCHEDULE):
            pass
    """)
    monkeypatch.delitem(sys.modules, "naming", raising=False)
    monkeypatch.delitem(sys.modules, "project_settings", raising=False)
    before = list(sys.path)

    assert [d.dag_id for d in load_pipeline(f).dags] == ["orders"]
    assert sys.path == before


@pytest.mark.parametrize("ending", ["sys.exit(0)", "sys.exit()"])
def test_a_clean_exit_ends_the_file_normally(tmp_path, ending):
    f = _write(tmp_path / "dag.py", f"""
        import sys
        from polyris import DAG
        with DAG("done", schedule="@daily"):
            pass
        {ending}
    """)
    assert [d.dag_id for d in load_pipeline(f).dags] == ["done"]


@pytest.mark.parametrize("body, words", [
    ("import sys\nsys.exit(3)\n", r"sys\.exit\(3\)"),
    ("raise RuntimeError('no credentials at import time')\n", "no credentials at import time"),
    ("this is not ) python\n", "invalid syntax|unmatched"),
])
def test_a_file_that_fails_is_a_load_error_and_the_dag_class_is_restored(tmp_path, body, words):
    original = (DAG.__post_init__, DAG.__exit__)
    f = _write(tmp_path / "dag.py", body)
    with pytest.raises(PipelineLoadError, match=words):
        load_pipeline(f)
    assert (DAG.__post_init__, DAG.__exit__) == original


def test_a_missing_file_is_its_own_error(tmp_path):
    with pytest.raises(PipelineNotFoundError, match="not found"):
        load_pipeline(tmp_path / "nope.py")


def test_one_dag_id_twice_is_refused(tmp_path):
    f = _write(tmp_path / "dag.py", """
        from polyris import DAG
        for schedule in ("@daily", "@hourly"):
            with DAG("orders", schedule=schedule):
                pass
    """)
    with pytest.raises(PipelineLoadError, match="'orders' more than once"):
        load_pipeline(f)


def test_a_file_without_dags_loads_with_none(tmp_path):
    assert load_pipeline(_write(tmp_path / "config.py", "STAGE = 'dev'\n")).dags == []


def test_the_projects_pipelines_modules_are_dropped_after_each_load(tmp_path, monkeypatch):
    _write(tmp_path / "pipelines" / "__init__.py", "")
    _write(tmp_path / "pipelines" / "config.py", "SCHEDULE = '@daily'\n")
    f = _write(tmp_path / "pipelines" / "orders" / "dag.py", """
        from pipelines.config import SCHEDULE
        from polyris import DAG
        with DAG("orders", schedule=SCHEDULE):
            pass
    """)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert [d.dag_id for d in load_pipeline(f).dags] == ["orders"]
    assert "pipelines" not in sys.modules and "pipelines.config" not in sys.modules


def test_no_cli_runs_a_pipeline_file_but_through_the_loader():
    """ADR #139: a second way to run a pipeline file is how validate and deploy came to disagree."""
    allowed = {"polyris/loader.py", "polyris/config.py"}  # config.py runs the project's config.py, not a pipeline
    runners = []
    for path in sorted((REPO_ROOT / "polyris").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("polyris/_ee") or rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in ("exec_module", "spec_from_file_location"):
                runners.append(f"{rel}:{node.lineno}")
    assert runners == []
