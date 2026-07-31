"""
Tests for `polyris-output` (polyris/output.py).

This module was at 0% coverage while being the command the README's "Try It Now"
section tells every new user to run three times. It was in the coverage `omit`
list under the AWS/CLI justification — but it touches no AWS at all (zero boto3
references), and `tests/e2e/` needs a live API, so nothing reached it.

The generators underneath are covered elsewhere; what is exercised here is the
CLI shell — argument dispatch, file/DAG selection, and the exit paths a user
hits by getting something wrong.
"""

import json
import sys

import pytest

from polyris.output import _load_dags, _select_dag, main


SINGLE_DAG = '''
from polyris import DAG, task

with DAG("solo-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:000000000000:stateMachine:a")
    def extract(): pass

    @task.sfn(arn="arn:aws:states:us-east-1:000000000000:stateMachine:b")
    def load(): pass

    extract() >> load()
'''

MULTI_DAG = '''
from polyris import DAG, task

with DAG("first-dag", schedule="@daily") as d1:
    @task.sfn(arn="arn:aws:states:us-east-1:000000000000:stateMachine:a")
    def a(): pass
    a()

with DAG("second-dag", schedule="@hourly") as d2:
    @task.sfn(arn="arn:aws:states:us-east-1:000000000000:stateMachine:b")
    def b(): pass
    b()
'''

ASSET_DAG = '''
from polyris import DAG, task, Asset

out = Asset("sales/daily")

with DAG("asset-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:000000000000:stateMachine:a", outlets=[out])
    def produce(): pass
    produce()
'''

NO_DAG = "x = 1\n"

BROKEN = "def broken(:\n"


@pytest.fixture
def in_dir(tmp_path, monkeypatch):
    """Run each test in its own directory, as the CLI reads ./dag.py."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(tmp_path, source, name="dag.py"):
    p = tmp_path / name
    p.write_text(source)
    return p


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["polyris-output", *argv])
    main()


# ── _load_dags ───────────────────────────────────────────────────────────────

class TestLoadDags:

    def test_missing_file_exits_1_with_a_named_reason(self, in_dir, capsys):
        with pytest.raises(SystemExit) as e:
            _load_dags("dag.py")
        assert e.value.code == 1
        assert "not found" in capsys.readouterr().out

    def test_file_without_a_dag_exits_1(self, in_dir, capsys):
        _write(in_dir, NO_DAG)
        with pytest.raises(SystemExit) as e:
            _load_dags("dag.py")
        assert e.value.code == 1
        assert "No DAG found" in capsys.readouterr().out

    def test_unimportable_file_exits_1_and_reports_the_error(self, in_dir, capsys):
        _write(in_dir, BROKEN)
        with pytest.raises(SystemExit) as e:
            _load_dags("dag.py")
        assert e.value.code == 1
        assert "Failed to load" in capsys.readouterr().out

    def test_returns_every_dag_in_the_file(self, in_dir):
        _write(in_dir, MULTI_DAG)
        dags, _ = _load_dags("dag.py")
        assert sorted(d.dag_id for d in dags) == ["first-dag", "second-dag"]


# ── _select_dag ──────────────────────────────────────────────────────────────

class TestSelectDag:

    def test_single_dag_needs_no_selector(self, in_dir):
        _write(in_dir, SINGLE_DAG)
        dags, _ = _load_dags("dag.py")
        assert _select_dag(dags).dag_id == "solo-dag"

    def test_multi_dag_without_select_exits_1_and_lists_options(self, in_dir, capsys):
        _write(in_dir, MULTI_DAG)
        dags, _ = _load_dags("dag.py")
        with pytest.raises(SystemExit) as e:
            _select_dag(dags)
        assert e.value.code == 1
        out = capsys.readouterr().out
        assert "first-dag" in out and "--select" in out

    def test_select_picks_the_named_dag(self, in_dir):
        _write(in_dir, MULTI_DAG)
        dags, _ = _load_dags("dag.py")
        assert _select_dag(dags, "second-dag").dag_id == "second-dag"

    def test_unknown_select_exits_1_and_shows_what_is_available(self, in_dir, capsys):
        _write(in_dir, MULTI_DAG)
        dags, _ = _load_dags("dag.py")
        with pytest.raises(SystemExit) as e:
            _select_dag(dags, "nope")
        assert e.value.code == 1
        out = capsys.readouterr().out
        assert "not found" in out and "first-dag" in out


# ── main() dispatch ──────────────────────────────────────────────────────────

class TestMainDispatch:

    def test_json_emits_parseable_asl(self, in_dir, monkeypatch, capsys):
        _write(in_dir, SINGLE_DAG)
        _run(monkeypatch, "--json")
        asl = json.loads(capsys.readouterr().out)
        assert "States" in asl and "StartAt" in asl

    def test_mermaid_emits_a_graph_block(self, in_dir, monkeypatch, capsys):
        _write(in_dir, SINGLE_DAG)
        _run(monkeypatch, "--mermaid")
        out = capsys.readouterr().out
        assert out.lstrip().startswith("graph")
        assert "extract" in out and "load" in out

    def test_graph_renders_ascii_with_the_task_names(self, in_dir, monkeypatch, capsys):
        _write(in_dir, SINGLE_DAG)
        _run(monkeypatch, "--graph")
        out = capsys.readouterr().out
        assert "solo-dag" in out and "extract" in out

    def test_assets_emits_registry_json_across_all_dags(self, in_dir, monkeypatch, capsys):
        _write(in_dir, ASSET_DAG)
        _run(monkeypatch, "--assets")
        payload = json.loads(capsys.readouterr().out)
        assert payload  # asset-producing pipeline yields a non-empty registry

    def test_file_flag_reads_a_non_default_filename(self, in_dir, monkeypatch, capsys):
        _write(in_dir, SINGLE_DAG, name="my_pipeline.py")
        _run(monkeypatch, "--json", "--file", "my_pipeline.py")
        assert json.loads(capsys.readouterr().out)["StartAt"]

    def test_select_flag_reaches_main(self, in_dir, monkeypatch, capsys):
        _write(in_dir, MULTI_DAG)
        _run(monkeypatch, "--mermaid", "--select", "second-dag")
        assert "b" in capsys.readouterr().out

    def test_assets_ignores_select_and_covers_the_whole_file(self, in_dir, monkeypatch, capsys):
        # --assets short-circuits before _select_dag, so a multi-DAG file does
        # not trip the "use --select" exit.
        _write(in_dir, MULTI_DAG)
        _run(monkeypatch, "--assets")
        json.loads(capsys.readouterr().out)

    def test_no_output_flag_is_rejected_by_argparse(self, in_dir, monkeypatch):
        _write(in_dir, SINGLE_DAG)
        with pytest.raises(SystemExit) as e:
            _run(monkeypatch)
        assert e.value.code == 2  # argparse usage error, not our exit(1)

    def test_two_output_flags_are_mutually_exclusive(self, in_dir, monkeypatch):
        _write(in_dir, SINGLE_DAG)
        with pytest.raises(SystemExit) as e:
            _run(monkeypatch, "--json", "--mermaid")
        assert e.value.code == 2
