"""Validation CLI tests — pipeline discovery, the test runner, and `main`.

Exercises the command-line surface of ``polyris.validation`` through the real
dispatch (CLAUDE.md #13): ``sys.argv`` is patched, ``cwd`` is pointed at a
temp project, and ``SystemExit`` / captured stdout are asserted — no internal
mocking. Also covers the verbose ``validate_asl_from_dag`` path and the
``_validate_single`` load-failure branch.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from polyris.validation import (
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

INVALID_ROLE_DAG = """
    from polyris import DAG, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("bad-role", schedule=None) as dag:
        @task.sfn(arn=ARN, role="not-a-real-role")
        def step():
            pass
        step()
"""

UNBOUND_DAG = """
    from polyris import DAG, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("unbound", schedule=None):
        @task.sfn(arn=ARN)
        def step():
            pass
        step()
"""

SYSEXIT_DAG = """
    import sys
    sys.exit(42)
"""


# ============================================================ #
# discovery + test runner
# ============================================================ #
class TestDiscoveryAndRunner:
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

    def test_run_test_returns_false_on_failure(self, tmp_path):
        """_run_test must return False when any callable raises — so main()
        can call sys.exit(1). Previously it returned None in all cases."""
        f = _write(tmp_path, "dag.py", RAISING_DAG)
        assert _run_test(str(f)) is False

    def test_run_test_returns_true_on_success(self, tmp_path):
        f = _write(tmp_path, "dag.py", SOLO_DAG)
        assert _run_test(str(f)) is True

    def test_run_test_finds_unbound_dag(self, tmp_path):
        """_run_test must not report 'No DAG found' for a pipeline written
        without `as dag` — it uses DAG.__exit__ instrumentation, not vars(mod)."""
        f = _write(tmp_path, "dag.py", UNBOUND_DAG)
        assert _run_test(str(f)) is True

    def test_run_test_sysexit_dag_exits_one_with_message(self, tmp_path, capsys):
        """_run_test must catch SystemExit from a pipeline that calls sys.exit()
        at import time and produce a clean error instead of letting the SystemExit
        propagate through (which would exit with the pipeline's own code, not 1)."""
        f = _write(tmp_path, "dag.py", SYSEXIT_DAG)
        with pytest.raises(SystemExit) as exc:
            _run_test(str(f))
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "❌" in out
        assert "sys.exit" in out


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

    def test_validate_asl_from_dag_verbose_with_errors(self, capsys):
        from polyris import DAG, task

        with DAG("bad-role-v", schedule=None) as dag:
            @task.sfn(arn=ARN, role="not-a-real-role")
            def step():
                pass
            step()
        ok, errors, _warnings = validate_asl_from_dag(dag, verbose=True)
        out = capsys.readouterr().out
        assert ok is False
        assert errors
        assert "❌" in out
        assert "not-a-real-role" in out

    def test_validate_single_finds_dag_without_as_binding(self, tmp_path):
        """_validate_single must use DAG.__exit__ instrumentation, not vars(mod).
        vars(mod) only finds names bound at module level and misses DAGs written
        as `with DAG("name"):` without an `as dag` clause."""
        f = _write(tmp_path, "dag.py", UNBOUND_DAG)
        valid, errors = _validate_single(str(f), verbose=False)
        assert valid is True
        assert errors == []

    def test_validate_single_load_failure_returns_false(self, tmp_path):
        f = _write(tmp_path, "dag.py", BROKEN_DAG)
        valid, errors = _validate_single(str(f), verbose=False)
        assert valid is False
        assert errors
        assert "Failed to load" in errors[0]

    def test_validate_single_sysexit_returns_error(self, tmp_path):
        """_validate_single must catch SystemExit and return (False, [...]) —
        not let the SystemExit propagate and kill the process."""
        f = _write(tmp_path, "dag.py", SYSEXIT_DAG)
        valid, errors = _validate_single(str(f), verbose=False)
        assert valid is False
        assert any("sys.exit" in e for e in errors)


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
        data = json.loads(capsys.readouterr().out)
        assert data["errors"] == []
        assert data["pipeline_count"] == 1
        assert "warnings" in data

    def test_all_with_no_pipelines_json_exits_one(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all", "--json"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["errors"] == []
        assert data["pipeline_count"] == 0
        assert "warnings" in data

    def test_all_json_verbose_clean_stdout(self, tmp_path, monkeypatch, capsys):
        """--all --json --verbose must produce parseable JSON — verbose prints
        are suppressed when --json is active so consumers don't get mixed output."""
        _write(tmp_path, "proj/dag.py", SOLO_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all", "--json", "-v"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["errors"] == []
        assert data["pipeline_count"] == 1

    def test_all_with_unbound_dag_exits_zero(self, tmp_path, monkeypatch):
        """--all must not report 'No DAG found' for pipelines written without `as dag`."""
        _write(tmp_path, "dag.py", UNBOUND_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0

    def test_all_asl_invalid_exits_one(self, tmp_path, monkeypatch, capsys):
        # DAG loads cleanly (extract_dag_info succeeds) but _validate_single
        # catches the invalid role and exits 1 — exercises the ASL-validation
        # path that validate_all's cross-pipeline pass does not cover.
        _write(tmp_path, "proj/dag.py", INVALID_ROLE_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1
        out = capsys.readouterr().out
        # Exactly one print path fires (main() non-verbose non-json branch) — not two.
        assert out.count("not-a-real-role") == 1

    def test_all_asl_invalid_json_includes_error_detail(self, tmp_path, monkeypatch, capsys):
        _write(tmp_path, "proj/dag.py", INVALID_ROLE_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all", "--json"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert any("not-a-real-role" in err for err in data["errors"])

    def test_single_file_invalid_prints_error(self, tmp_path, monkeypatch, capsys):
        f = _write(tmp_path, "dag.py", INVALID_ROLE_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "-f", str(f)])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1
        assert "not-a-real-role" in capsys.readouterr().out

    def test_single_file_json_output(self, tmp_path, monkeypatch, capsys):
        f = _write(tmp_path, "dag.py", SOLO_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--json", "-f", str(f)])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["valid"] is True
        assert data["file"] == str(f)
        assert data["errors"] == []

    def test_single_file_json_invalid_includes_errors(self, tmp_path, monkeypatch, capsys):
        f = _write(tmp_path, "dag.py", INVALID_ROLE_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--json", "-f", str(f)])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["valid"] is False
        assert any("not-a-real-role" in err for err in data["errors"])

    def test_single_file_json_verbose_clean_stdout(self, tmp_path, monkeypatch, capsys):
        """--json -v on a single file must produce parseable JSON."""
        f = _write(tmp_path, "dag.py", SOLO_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--json", "-v", "-f", str(f)])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["valid"] is True

    def test_test_mode_runs_without_exit(self, tmp_path, monkeypatch):
        f = _write(tmp_path, "dag.py", SOLO_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--test", "-f", str(f)])
        # --test path runs callables and returns normally (no SystemExit).
        assert main() is None

    def test_test_mode_exits_one_on_failure(self, tmp_path, monkeypatch):
        """--test must exit 1 when any callable raises — previously it always
        exited 0 because _run_test swallowed errors and returned None."""
        f = _write(tmp_path, "dag.py", RAISING_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--test", "-f", str(f)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_test_mode_broken_import_exits_one_with_message(self, tmp_path, monkeypatch, capsys):
        """--test must exit 1 with a clean error (not a raw traceback) when the
        pipeline file raises at import time."""
        f = _write(tmp_path, "dag.py", BROKEN_DAG)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--test", "-f", str(f)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "❌" in out
        assert "Failed to load" in out

    def test_all_verbose_invalid_shows_error(self, tmp_path, monkeypatch, capsys):
        """--all -v with an invalid pipeline must print error detail and exit 1."""
        _write(tmp_path, "proj/dag.py", INVALID_ROLE_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all", "-v"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1
        out = capsys.readouterr().out
        assert "not-a-real-role" in out
