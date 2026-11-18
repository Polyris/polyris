"""polyris.folder_process — each folder's own Python process in ``--all`` (ADR #139).

``polyris-validate --all`` here goes through real processes; ``polyris-deploy --all`` through
the subprocess boundary in ``test_deploy_bulk.py``, since a real one would reach AWS.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from polyris import folder_process
from polyris.validation import collect_folder, validate_all


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


def test_validate_all_runs_each_folder_in_its_own_process(tmp_path):
    """One folder's settings.py must never reach the next folder's pipeline, wherever it lives."""
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
    _write(tmp_path / "a" / "dag.py", """
        import os
        os._exit(3)  # no exception, no answer
    """)
    results = validate_all(str(tmp_path), verbose=False)
    assert results["pipelines"] == [] and len(results["errors"]) == 1
    assert "exited with code 3 before reporting" in results["errors"][0]


def test_a_folders_process_starts_without_the_run_directory_on_its_path(tmp_path, monkeypatch):
    """``python -m`` put the current directory first, so a run directory with a ``json.py`` (or
    an ``email/`` package) broke polyris's own imports in every folder's process."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "json.py", "raise RuntimeError('the run directory shadowed the standard library')\n")
    _write(tmp_path / "orders" / "dag.py", """
        from polyris import DAG
        with DAG("orders", schedule="@daily"):
            pass
    """)
    results = validate_all(".", verbose=False)
    assert results["errors"] == [] and [d.dag_id for d in results["pipelines"]] == ["orders"]


def test_what_a_pipeline_prints_is_shown_except_under_json(tmp_path, capfd):
    """D112: as before the folder processes, a pipeline's own output reaches the user; --json
    (quiet) keeps stdout clean and still shows errors on stderr."""
    _write(tmp_path / "a" / "dag.py", """
        import sys
        from polyris import DAG
        print("hello from the pipeline")
        print("a warning", file=sys.stderr)
        with DAG("a", schedule="@daily"):
            pass
    """)
    validate_all(str(tmp_path), verbose=False)
    out, err = capfd.readouterr()
    assert "hello from the pipeline" in out and "a warning" in err
    validate_all(str(tmp_path), verbose=False, quiet=True)
    out, err = capfd.readouterr()
    assert "hello from the pipeline" not in out and "a warning" in err


def test_verbose_prints_the_loaded_dags_before_their_checks(tmp_path, capsys):
    _write(tmp_path / "a" / "dag.py", """
        from polyris import DAG, task
        with DAG("a", schedule="@daily"):
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def go(): pass
            go()
    """)
    validate_all(str(tmp_path), verbose=True, check_asl=True)
    out = capsys.readouterr().out
    assert out.index("Loaded") < out.index("Validating:")


def test_collect_folder_answers_dag_info_errors_and_the_checks_output(tmp_path):
    """The folder's side of validate_all, run here so coverage sees it."""
    f = _write(tmp_path / "dag.py", """
        from polyris import Asset, DAG, task
        with DAG("orders", schedule=[Asset("raw/orders")]):
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def a(): pass
            a()
    """)
    answer = collect_folder([str(f), str(tmp_path / "missing.py")], check_asl=True, verbose=True, color=True)
    assert [d["dag_id"] for d in answer["infos"]] == ["orders"]
    assert isinstance(answer["infos"][0]["schedule"], str) and answer["infos"][0]["trigger_assets"] == ["raw/orders"]
    assert len(answer["errors"]) == 1 and "missing.py" in answer["errors"][0]
    # Coloured for a terminal; an asset schedule (a list) once crashed the coloured line
    assert "Validating:" in answer["report"] and "\033[" in answer["report"]


def test_one_files_unreportable_dag_info_is_its_error_not_the_folders(tmp_path):
    """A schema default that JSON cannot carry crashed the folder's process, and its other files
    were lost with it."""
    bad = _write(tmp_path / "dag.py", """
        import datetime
        from polyris import DAG, task, Asset, Column
        from polyris import types as t
        orders = Asset("s3://b/orders/", schema=[Column("d", t.date(), default=datetime.date(2026, 1, 1))])
        with DAG("p1", schedule="@daily"):
            @task.lambda_function(function_name="f", outlets=[orders])
            def a(): pass
            a()
    """)
    good = _write(tmp_path / "second_dag.py", """
        from polyris import DAG
        with DAG("p2", schedule="@daily"):
            pass
    """)
    answer = collect_folder([str(bad), str(good)])
    assert [d["dag_id"] for d in answer["infos"]] == ["p2"]
    assert len(answer["errors"]) == 1 and answer["errors"][0].startswith(f"{bad}: ")


def test_main_does_what_the_request_says_and_writes_the_answer(tmp_path):
    f = _write(tmp_path / "a" / "dag.py", "from polyris import DAG\nwith DAG('a'):\n    pass\n")
    request, answer = tmp_path / "request.json", tmp_path / "answer.json"
    request.write_text(json.dumps({"command": "collect", "files": [str(f)]}))
    assert folder_process.main([str(request), str(answer)]) == 0
    assert [d["dag_id"] for d in json.loads(answer.read_text())["infos"]] == ["a"]

    request.write_text(json.dumps({"command": "something else"}))
    with pytest.raises(ValueError, match="unknown folder process command"):
        folder_process.main([str(request), str(answer)])


def test_a_ctrl_c_in_the_folders_process_ends_it_quietly(tmp_path):
    """The parent was stopped too and prints its own traceback: one, not two."""
    f = _write(tmp_path / "dag.py", "raise KeyboardInterrupt\n")
    request, answer = tmp_path / "request.json", tmp_path / "answer.json"
    request.write_text(json.dumps({"command": "collect", "files": [str(f)]}))
    assert folder_process.main([str(request), str(answer)]) == 130
    assert not answer.exists()  # no answer: the parent reports the stop


def test_run_hides_what_the_output_mode_says(tmp_path, capfd):
    f = _write(tmp_path / "dag.py", """
        import sys
        print("to stdout")
        print("to stderr", file=sys.stderr)
    """)
    for output, shown in (("all", ("to stdout", "to stderr")), ("stderr", ("to stderr",)), ("none", ())):
        answer = folder_process.run("collect", {"files": [str(f)]}, output=output)
        assert answer.error is None
        out, err = capfd.readouterr()
        assert [text for text in ("to stdout", "to stderr") if text in out + err] == list(shown)


def test_a_childs_python_path_never_moves_the_users_entries_or_site_packages(monkeypatch):
    """Prepending polyris's folder put site-packages before a user's src/ and the standard
    library. It is appended, and only when polyris is not installed."""
    import polyris
    code_root = str(Path(polyris.__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", "src")
    monkeypatch.setattr(folder_process.site, "getsitepackages", lambda: [])
    assert folder_process.child_env()["PYTHONPATH"] == os.pathsep.join(["src", code_root])
    monkeypatch.setattr(folder_process.site, "getsitepackages", lambda: [code_root])  # installed: found as here
    assert folder_process.child_env()["PYTHONPATH"] == "src"


def test_run_child_returns_the_childs_exit_code_and_stops_it_when_this_process_is_stopped(mocker):
    assert folder_process.run_child(["-c", "import sys; sys.exit(3)"], dict(os.environ)) == 3

    child = mocker.MagicMock()
    child.wait.side_effect = [KeyboardInterrupt, 0]  # Ctrl-C while waiting; then the stopped child's end
    mocker.patch("polyris.folder_process.subprocess.Popen", return_value=child)
    with pytest.raises(KeyboardInterrupt):
        folder_process.run_child(["-c", "pass"], {})
    child.terminate.assert_called_once()


def test_validate_all_stops_its_folders_process_on_sigterm(tmp_path, mocker):
    """Without the handler, a stopped validate left its folder's process running."""
    _write(tmp_path / "a" / "dag.py", "from polyris import DAG\n")
    seen = []
    mocker.patch("polyris.folder_process.run_child",
                 side_effect=lambda *a, **k: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
    results = validate_all(str(tmp_path), verbose=False)
    assert seen == [folder_process._raise_on_sigterm]
    assert "exited with code 0 before reporting" in results["errors"][0]


def test_sigterm_ends_this_process_through_python_only_inside_the_block():
    before = signal.getsignal(signal.SIGTERM)
    with folder_process.sigterm_stops_children():
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as e:
            handler(signal.SIGTERM, None)
        assert e.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is before

    seen = []

    def off_the_main_thread():
        with folder_process.sigterm_stops_children():  # signal.signal would raise ValueError here
            seen.append(signal.getsignal(signal.SIGTERM))

    t = threading.Thread(target=off_the_main_thread)
    t.start()
    t.join()
    assert seen == [before]
