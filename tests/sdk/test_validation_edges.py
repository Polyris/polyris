"""Validation edge tests — discovery skip, validate_all warnings, --all.

Closes the reachable remainder of ``polyris.validation`` (CLAUDE.md #13): the
binary-file skip in ``discover_pipeline_files``, the no-DAG and verbose/warning
paths of ``validate_all``, and ``main --all`` over an invalid pipeline.
"""
from __future__ import annotations

import sys
import textwrap

import pytest

from polyris.validation import (
    discover_pipeline_files,
    validate_all,
    main,
)


def _write(tmp_path, rel, body):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())
    return p


_CONSUMER_DAG = """
    from polyris import DAG, task, Asset
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("consumer", schedule=Asset("ns/unproduced")) as dag:
        @task.sfn(arn=ARN)
        def go():
            pass
        go()
"""

_BROKEN_DAG = """
    from polyris import DAG  # noqa: F401
    raise RuntimeError("kaboom on import")
    with DAG("never"):
        pass
"""

# Looks like a pipeline to the discoverer (contains the marker) but defines no DAG.
_NO_DAG_FILE = '''
    marker = "with DAG("  # discovery sees this; there is no real DAG here
    value = 1
'''

_VALID_DAG = """
    from polyris import DAG, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("ok", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def go():
            pass
        go()
"""


class TestDiscovery:
    def test_skips_binary_file(self, tmp_path):
        (tmp_path / "blob.py").write_bytes(b"\xff\xfe\x00\x01 not utf-8 \x80")
        found = discover_pipeline_files(str(tmp_path))
        assert all("blob.py" not in f for f in found)


class TestValidateAll:
    def test_pipeline_file_without_dag_warns(self, tmp_path):
        _write(tmp_path, "dag.py", _NO_DAG_FILE)
        results = validate_all(str(tmp_path), verbose=False)
        assert any("No DAGs" in w for w in results["warnings"])

    def test_verbose_reports_unproduced_asset_warning(self, tmp_path, capsys):
        _write(tmp_path, "dag.py", _CONSUMER_DAG)
        results = validate_all(str(tmp_path), verbose=True)
        out = capsys.readouterr().out
        assert any("no producer" in w for w in results["warnings"])
        assert "warning" in out.lower()
        assert "No cycles detected" in out


class TestMainAll:
    def test_all_with_broken_pipeline_exits_one(self, tmp_path, monkeypatch):
        _write(tmp_path, "dag.py", _BROKEN_DAG)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-validate", "--all"])
        with pytest.raises(SystemExit) as e:
            main()
        assert e.value.code == 1


class TestValidateSingle:
    def test_valid_file_returns_true(self, tmp_path):
        from polyris.validation import _validate_single

        f = _write(tmp_path, "dag.py", _VALID_DAG)
        assert _validate_single(str(f), verbose=True) is True


# Two pipelines declaring the *same* asset with conflicting column types — the
# cross-pipeline schema check collects the per-DAG types and warns.
_SCHEMA_A = """
    from polyris import DAG, task, Asset, Column, types as t
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("prod_a", schedule=None) as dag:
        @task.sfn(arn=ARN, outlets=[Asset("ns/orders", schema=[Column("id", t.bigint())])])
        def a():
            pass
        a()
"""

_SCHEMA_B = """
    from polyris import DAG, task, Asset, Column, types as t
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG("prod_b", schedule=None) as dag:
        @task.sfn(arn=ARN, outlets=[Asset("ns/orders", schema=[Column("id", t.string())])])
        def b():
            pass
        b()
"""


class TestSchemaConsistency:
    def test_conflicting_column_types_warn(self, tmp_path):
        _write(tmp_path, "a.py", _SCHEMA_A)
        _write(tmp_path, "b.py", _SCHEMA_B)
        results = validate_all(str(tmp_path), verbose=False)
        assert any("id" in w for w in results["warnings"])


class TestAslFromDag:
    def test_verbose_prints_summary(self, capsys):
        from polyris import DAG, task
        from polyris.validation import validate_asl_from_dag

        # A well-formed DAG validates clean — no errors, no warnings. Verbose mode
        # prints the summary and the valid marker. (The errors/warnings print
        # branches only fire for hand-written ASL — see their pragmas.)
        with DAG("one_task", schedule="@daily") as dag:
            @task.sfn(arn="arn:aws:states:us-east-1:1:stateMachine:s")
            def only():
                pass
            only()
        is_valid, errors, warnings = validate_asl_from_dag(dag, verbose=True)
        out = capsys.readouterr().out
        assert is_valid and not errors and not warnings
        assert "Tasks:" in out  # verbose summary line
        assert "✅ Valid" in out


def test_validate_asl_rejects_empty_parallel():
    """AWS rejects a Parallel state with no branches; the validator must too.
    (An empty DAG generates exactly this — Run_All_Tasks with Branches: [].)"""
    from polyris.generators import validate_asl

    asl = {
        "StartAt": "P",
        "States": {
            "P": {"Type": "Parallel", "Branches": [], "End": True},
        },
    }
    is_valid, errors, _warnings = validate_asl(asl)
    assert not is_valid
    assert any("no branches" in e for e in errors)
