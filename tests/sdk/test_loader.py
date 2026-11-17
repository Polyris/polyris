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
#: The ways to run a Python file as a module (importlib, runpy).
RUNNERS = {"exec_module", "spec_from_file_location", "run_path", "run_module"}
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


def test_a_module_whose_getattr_raises_does_not_break_the_load(tmp_path):
    """Fourth review: reading `__path__` ran the module's `__getattr__`, which raised KeyError."""
    _write(tmp_path / "settings_lazy.py", "_VALUES = {}\ndef __getattr__(name):\n    return _VALUES[name]\n")
    f = _write(tmp_path / "dag.py", """
        import settings_lazy  # noqa: F401
        from polyris import DAG
        with DAG("orders", schedule="@daily"):
            pass
    """)
    assert [d.dag_id for d in load_pipeline(f).dags] == ["orders"]


def test_a_path_with_dotdot_has_the_right_folder_above(tmp_path, monkeypatch):
    _write(tmp_path / "common.py", "SCHEDULE = '@daily'\n")
    _write(tmp_path / "orders" / "dag.py", """
        from common import SCHEDULE
        from polyris import DAG
        with DAG("orders", schedule=SCHEDULE):
            pass
    """)
    (tmp_path / "orders" / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "orders" / "sub")
    monkeypatch.delitem(sys.modules, "common", raising=False)
    assert [d.dag_id for d in load_pipeline("../dag.py").dags] == ["orders"]


def test_inside_open_pipeline_a_task_body_can_import_a_sibling(tmp_path, monkeypatch):
    """Fourth review: polyris-local --mock and validate --test run bodies after the load."""
    from polyris.loader import open_pipeline
    _write(tmp_path / "helpers.py", "VALUE = 42\n")
    f = _write(tmp_path / "dag.py", """
        from polyris import DAG
        def body():
            from helpers import VALUE
            return VALUE
        with DAG("orders", schedule="@daily"):
            pass
    """)
    monkeypatch.delitem(sys.modules, "helpers", raising=False)
    with open_pipeline(f) as loaded:
        assert loaded.module.body() == 42
    assert str(tmp_path) not in sys.path


def test_a_relative_path_makes_the_folder_above_importable_too(tmp_path, monkeypatch):
    """Third review: `polyris-deploy` reads "dag.py", whose parent is "." and has no folder above."""
    _write(tmp_path / "project_settings.py", "SCHEDULE = '@daily'\n")
    _write(tmp_path / "orders" / "dag.py", """
        from project_settings import SCHEDULE
        from polyris import DAG
        with DAG("orders", schedule=SCHEDULE):
            pass
    """)
    monkeypatch.chdir(tmp_path / "orders")
    monkeypatch.delitem(sys.modules, "project_settings", raising=False)
    assert [d.dag_id for d in load_pipeline("dag.py").dags] == ["orders"]


@pytest.mark.parametrize("body", [
    'exec(compile(SRC, __file__.replace("dag.py", "generated.py"), "exec"), {})',
    'import runpy; runpy.run_path(OTHER)',
], ids=["exec of code compiled with a real file name", "runpy.run_path"])
def test_code_the_file_runs_itself_is_the_files(tmp_path, body):
    """Third review: a <module> frame with a real file name was taken for another module."""
    _write(tmp_path / "other.py", """
        from polyris import DAG
        with DAG("orders", schedule="@daily"):
            pass
    """)
    f = _write(tmp_path / "dag.py", f"""
        SRC = open(r"{tmp_path / 'other.py'}").read()
        OTHER = r"{tmp_path / 'other.py'}"
        {body}
    """)
    assert [d.dag_id for d in load_pipeline(f).dags] == ["orders"]


@pytest.mark.parametrize("inside", ["sys.exit(0)", "yield"], ids=["sys.exit(0) in the block", "a closed generator"])
def test_a_block_that_ends_without_an_error_keeps_its_dag(tmp_path, inside):
    if inside == "yield":
        body = """
            from polyris import DAG
            def make():
                with DAG("orders", schedule="@daily"):
                    yield
            gen = make(); next(gen); gen.close()
        """
    else:
        body = """
            import sys
            from polyris import DAG
            with DAG("orders", schedule="@daily"):
                sys.exit(0)
        """
    assert [d.dag_id for d in load_pipeline(_write(tmp_path / "dag.py", body)).dags] == ["orders"]


def test_an_unreadable_py_is_not_a_pipeline_file(tmp_path):
    from polyris.loader import is_pipeline_file
    (tmp_path / "odd.py").mkdir()  # reading it fails like an unreadable file
    assert is_pipeline_file(tmp_path / "odd.py") is False


def test_validate_all_runs_each_folder_in_its_own_process(tmp_path):
    """Third and fifth reviews: one folder's settings.py reached the next folder's pipeline.
    A fresh process per folder (ADR #139) makes that impossible, wherever settings.py lives."""
    from polyris.validation import validate_all
    for name in ("a", "b"):
        _write(tmp_path / name / "settings.py", f"NAME = 'orders-{name}'\n")
        _write(tmp_path / name / "dag.py", """
            from settings import NAME
            from polyris import DAG
            with DAG(NAME, schedule="@daily"):
                pass
        """)
    results = validate_all(str(tmp_path), verbose=False)
    assert results["errors"] == []
    assert sorted(d.dag_id for d in results["pipelines"]) == ["orders-a", "orders-b"]
    assert "settings" not in sys.modules  # nothing was imported into this process


def test_a_folder_whose_process_dies_is_an_error_not_silence(tmp_path):
    from polyris.validation import validate_all
    _write(tmp_path / "a" / "dag.py", """
        import os
        os._exit(3)  # no exception, no report
    """)
    results = validate_all(str(tmp_path), verbose=False)
    assert results["pipelines"] == [] and len(results["errors"]) == 1
    assert "ended before reporting" in results["errors"][0]


def test_a_folders_own_process_reports_its_dag_info_as_json(tmp_path, monkeypatch):
    """The child side of validate_all, run here so coverage sees it."""
    import json
    from polyris import validation
    f = _write(tmp_path / "dag.py", """
        from polyris import Asset, DAG, task
        with DAG("orders", schedule=[Asset("raw/orders")]):
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def a(): pass
            a()
    """)
    results = tmp_path / "out.json"
    monkeypatch.setenv(validation.DAG_INFO_RESULTS_ENV, str(results))
    monkeypatch.setattr(sys, "argv", ["polyris-validate", str(f), str(tmp_path / "missing.py")])
    validation.main()
    data = json.loads(results.read_text())
    assert [d["dag_id"] for d in data["infos"]] == ["orders"] and data["infos"][0]["schedule"] is None
    assert len(data["errors"]) == 1 and "missing.py" in data["errors"][0]


def test_no_cli_runs_a_pipeline_file_but_through_the_loader():
    """ADR #139: a second way to run a pipeline file is how validate and deploy came to disagree."""
    allowed = {"polyris/loader.py", "polyris/config.py"}  # config.py runs the project's config.py, not a pipeline
    runners = []
    for path in sorted((REPO_ROOT / "polyris").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("polyris/_ee") or rel in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            used = (node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name)
                    else None)
            imported = [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else []
            if used in RUNNERS or RUNNERS.intersection(imported):
                runners.append(f"{rel}:{node.lineno}")
    assert runners == []
