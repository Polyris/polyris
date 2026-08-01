"""Validation CLI tests — pipeline discovery, the test runner, and `main`.

Exercises the command-line surface of ``polyris.validation`` through the real
dispatch (CLAUDE.md #13): ``sys.argv`` is patched, ``cwd`` is pointed at a
temp project, and ``SystemExit`` / captured stdout are asserted — no internal
mocking. Also covers the verbose ``validate_asl_from_dag`` path and the
``_validate_single`` load-failure branch.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from polyris.validation import (
    _find_all_pipelines,
    _run_test,
    _validate_single,
    validate_asl_from_dag,
    main,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())
    return p


SOLO_DAG = """
    from polyris import DAG, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("solo", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def step():
            pass
        step()
"""

RAISING_DAG = """
    from polyris import DAG, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("boomer", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def boom():
            raise ValueError("kaboom")
        boom()
"""

BROKEN_DAG = """
    from polyris import DAG  # noqa: F401
    raise RuntimeError("import explodes")
"""


# ============================================================ #
# discovery + test runner
# ============================================================ #
class TestDiscoveryAndRunner:
    def test_find_all_pipelines(self, tmp_path, monkeypatch):
        _write(tmp_path, "proj/dag.py", "x = 1\n")
        monkeypatch.chdir(tmp_path)
        found = _find_all_pipelines()
        assert any(f.endswith("/dag.py") for f in found)

    def test_run_test_executes_callables(self, tmp_path, capsys):
        f = _write(tmp_path, "dag.py", SOLO_DAG)
        _run_test(str(f))
        assert "Testing DAG" in capsys.readouterr().out

    def test_run_test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _run_test(str(tmp_path / "nope.py"))

    def test_run_test_no_dag_exits(self, tmp_path):
        f = _write(tmp_path, "plain.py", "x = 1\n")
        with pytest.raises(SystemExit):
            _run_test(str(f))

    def test_run_test_catches_callable_error(self, tmp_path, capsys):
        f = _write(tmp_path, "dag.py", RAISING_DAG)
        _run_test(str(f))  # must not propagate
        assert "Error" in capsys.readouterr().out


# ============================================================ #
# validate_asl_from_dag verbose + _validate_single failure
# ============================================================ #
class TestSingleAndVerbose:
    def test_validate_asl_from_dag_verbose(self, capsys):
        from polyris import DAG, task

        with DAG("v", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass
            a()
        ok, _errors, _warnings = validate_asl_from_dag(dag, verbose=True)
        out = capsys.readouterr().out
        assert ok is True
        assert "Tasks:" in out and "States:" in out

    def test_validate_single_load_failure_returns_false(self, tmp_path):
        f = _write(tmp_path, "dag.py", BROKEN_DAG)
        assert _validate_single(str(f), verbose=False) is False


# ============================================================ #
# main() dispatch
# ============================================================ #
class TestMain:
    def test_default_valid_exits_zero(self, tmp_path, monkeypatch):
        _write(tmp_path, "dag.py", SOLO_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0

    def test_all_with_no_pipelines_exits_one(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # empty project
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1

    def test_all_valid_with_json(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path, "proj/dag.py", SOLO_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all", "--json"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0
        assert '"valid"' in capsys.readouterr().out  # JSON results printed

    def test_test_mode_runs_without_exit(self, tmp_path, monkeypatch):
        f = _write(tmp_path, "dag.py", SOLO_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--test", "-f", str(f)])
        # --test path runs callables and returns normally (no SystemExit).
        assert main() is None
