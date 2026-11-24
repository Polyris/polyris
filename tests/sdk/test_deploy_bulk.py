"""polyris-deploy --all / --only — bulk deploy across pipeline directories.

Each directory is scanned for *every* .py file (not just dag.py), and every DAG
found in every file is deployed — a directory can hold more than one pipeline
file, and a file with zero DAGs (a shared config.py, a utils module) is a
no-op, not an error. One DAG failing must not stop the rest of the batch.
"""
import sys
import textwrap

import pytest

from polyris import deploy


def _write_dag_file(path, dag_id, extra=""):
    path.write_text(textwrap.dedent(f"""
        from polyris import DAG, task

        with DAG("{dag_id}", schedule="@daily") as dag:
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def go():
                pass
            go()
        {extra}
    """))


class TestDiscoverDagsInDir:
    def test_finds_a_single_dag_in_dag_py(self, tmp_path):
        _write_dag_file(tmp_path / "dag.py", "solo")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert [d.dag_id for _, d in found] == ["solo"]

    def test_finds_multiple_dags_across_multiple_files(self, tmp_path):
        """The exact case this feature was built for: a directory with more
        than one named pipeline file, not just a single dag.py."""
        _write_dag_file(tmp_path / "orders.py", "orders-pipeline")
        _write_dag_file(tmp_path / "inventory.py", "inventory-pipeline")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert sorted(d.dag_id for _, d in found) == ["inventory-pipeline", "orders-pipeline"]

    def test_multiple_dags_in_one_file_all_found(self, tmp_path):
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"

            with DAG("dag-a", schedule="@daily") as dag_a:
                @task.sfn(arn=ARN)
                def a(): pass
                a()

            with DAG("dag-b", schedule="@daily") as dag_b:
                @task.sfn(arn=ARN)
                def b(): pass
                b()
        """))
        found = deploy._discover_dags_in_dir(tmp_path)
        assert sorted(d.dag_id for _, d in found) == ["dag-a", "dag-b"]

    def test_file_with_no_dags_is_silently_skipped(self, tmp_path):
        _write_dag_file(tmp_path / "dag.py", "real-one")
        (tmp_path / "config.py").write_text("STAGE = 'dev'\nREGION = 'us-east-1'\n")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert [d.dag_id for _, d in found] == ["real-one"]

    def test_file_that_fails_to_load_is_skipped_not_fatal(self, tmp_path, capsys):
        _write_dag_file(tmp_path / "dag.py", "real-one")
        (tmp_path / "broken_dag.py").write_text("from polyris import DAG\nthis is not ) valid python (((")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert [d.dag_id for _, d in found] == ["real-one"]
        assert "Skipping" in capsys.readouterr().out

    def test_a_file_that_is_not_a_pipeline_is_never_run(self, tmp_path, capsys):
        """A Glue script next to dag.py failed every batch; validate --all never ran it."""
        _write_dag_file(tmp_path / "dag.py", "real-one")
        (tmp_path / "glue_job.py").write_text("import awsglue  # only on Glue\nfrom polyris import xcom\n")
        failed: list = []
        found = deploy._discover_dags_in_dir(tmp_path, failed)
        assert [d.dag_id for _, d in found] == ["real-one"]
        assert failed == [] and "Skipping" not in capsys.readouterr().out

    def test_empty_directory_returns_nothing(self, tmp_path):
        assert deploy._discover_dags_in_dir(tmp_path) == []

    def test_file_that_exits_nonzero_is_skipped_with_warning(self, tmp_path, capsys):
        """A pipeline file that calls sys.exit(1) is treated as a load error."""
        _write_dag_file(tmp_path / "good.py", "good-dag")
        (tmp_path / "bad_pipeline.py").write_text("import sys\nfrom polyris import DAG\nsys.exit(1)\n")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert [d.dag_id for _, d in found] == ["good-dag"]
        assert "Skipping" in capsys.readouterr().out


class TestLoadDagFromFile:
    def test_imported_dag_is_excluded(self, tmp_path, monkeypatch):
        """A DAG imported into the pipeline file must not be returned."""
        (tmp_path / "shared_mod.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("shared-dag", schedule="@daily") as shared_dag:
                @task.sfn(arn=ARN)
                def s(): pass
                s()
        """))
        (tmp_path / "dag.py").write_text(textwrap.dedent(f"""
            import sys; sys.path.insert(0, r"{tmp_path}")
            from shared_mod import shared_dag  # imported, not defined here
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("local-dag", schedule="@daily") as local_dag:
                @task.sfn(arn=ARN)
                def go(): pass
                go()
        """))
        monkeypatch.delitem(sys.modules, "shared_mod", raising=False)

        dags = deploy._load_dag_from_file(tmp_path / "dag.py")
        assert [d.dag_id for d in dags] == ["local-dag"]

    def test_a_dag_without_as_is_found(self, tmp_path):
        """F18: `with DAG("x"):` binds no name; the loader finds it by its `with` block."""
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("first", schedule="@daily"):
                @task.sfn(arn=ARN)
                def a(): pass
                a()
            with DAG("second", schedule="@daily") as named:
                @task.sfn(arn=ARN)
                def b(): pass
                b()
        """))

        dags = deploy._load_dag_from_file(tmp_path / "dag.py")
        assert [d.dag_id for d in dags] == ["first", "second"]

    def test_a_dag_an_imported_module_defines_without_as_is_excluded(self, tmp_path, monkeypatch):
        (tmp_path / "shared_unbound.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("shared-unbound", schedule="@daily"):
                @task.sfn(arn=ARN)
                def s(): pass
                s()
        """))
        (tmp_path / "dag.py").write_text(textwrap.dedent(f"""
            import sys; sys.path.insert(0, r"{tmp_path}")
            import shared_unbound  # noqa: F401  defines a DAG of its own, with no name
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("local-dag", schedule="@daily"):
                @task.sfn(arn=ARN)
                def go(): pass
                go()
        """))
        monkeypatch.delitem(sys.modules, "shared_unbound", raising=False)

        dags = deploy._load_dag_from_file(tmp_path / "dag.py")
        assert [d.dag_id for d in dags] == ["local-dag"]

    @pytest.mark.parametrize("files, expected", [
        pytest.param({"dag.py": """
            from polyris import task
            from polyris.helpers import dag
            @dag(schedule="@daily")
            def unbound_etl():
                @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                def a(): pass
                a()
            unbound_etl()
        """}, ["unbound_etl"], id="an @dag call with no name"),
        pytest.param({"common.py": """
            from polyris import DAG, task
            def make(name):
                with DAG(name, schedule="@daily") as d:
                    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                    def a(): pass
                    a()
                return d
        """, "dag.py": """
            import sys, os; sys.path.insert(0, os.path.dirname(__file__))
            from common import make
            for n in ("f1", "f2"):
                make(n)
        """}, ["f1", "f2"], id="a factory in another module, called in a loop"),
        pytest.param({"dag.py": """
            SRC = (
                "from polyris import DAG, task\\n"
                "with DAG('exec-dag', schedule='@daily'):\\n"
                "    @task.sfn(arn='arn:aws:states:us-east-1:123456789012:stateMachine:x')\\n"
                "    def a(): pass\\n"
                "    a()\\n"
            )
            exec(compile(SRC, "<generated>", "exec"))
        """}, ["exec-dag"], id="exec inside the file"),
    ])
    def test_a_dag_this_file_makes_through_a_call_is_found(self, tmp_path, monkeypatch, files, expected):
        """polyris-validate finds these; the deploy must too (F18)."""
        for name, body in files.items():
            (tmp_path / name).write_text(textwrap.dedent(body))
        monkeypatch.delitem(sys.modules, "common", raising=False)

        assert [d.dag_id for d in deploy._load_dag_from_file(tmp_path / "dag.py")] == expected

    def test_a_file_that_defines_one_dag_id_twice_is_refused(self, tmp_path, capsys):
        """Each DAG is one stack: the second would silently replace the first."""
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            for schedule in ("@daily", "@hourly"):
                with DAG("orders", schedule=schedule):
                    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                    def a(): pass
                    a()
        """))
        with pytest.raises(SystemExit) as exc:
            deploy._load_dag_from_file(tmp_path / "dag.py")
        assert exc.value.code == 1
        assert "'orders' more than once" in capsys.readouterr().out

    def test_a_dag_is_another_files_only_when_an_import_made_it(self):
        """ADR #139: an `import` between the DAG and the loader makes it the imported module's."""
        import types
        from polyris.loader import _made_in_an_import

        def frame(module, code="f", back=None):
            return types.SimpleNamespace(f_globals={"__name__": module}, f_code=types.SimpleNamespace(co_name=code), f_back=back)
        loader = frame("polyris.loader", "open_pipeline")
        assert _made_in_an_import(frame("common", back=frame("polyris_pipeline", "<module>", loader)), loader) is False
        imported = frame("helpers", "<module>", frame("importlib._bootstrap", "_find_and_load", frame("polyris_pipeline", "<module>", loader)))
        assert _made_in_an_import(imported, loader) is True
        assert _made_in_an_import(frame("worker"), loader) is False  # a thread: never reaches the loader

    def test_a_helper_that_imports_the_pipeline_file_back_does_not_make_a_second_dag(self, tmp_path, monkeypatch):
        """The file ran again as module `dag`; matched by file name, it counted twice."""
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            import sys, os; sys.path.insert(0, os.path.dirname(__file__))
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("orders", schedule="@daily") as dag:
                @task.sfn(arn=ARN)
                def a(): pass
                a()
            import helpers  # noqa: F401  imports this file back, as module `dag`
        """))
        (tmp_path / "helpers.py").write_text("from dag import ARN  # noqa: F401\n")
        monkeypatch.delitem(sys.modules, "dag", raising=False)
        monkeypatch.delitem(sys.modules, "helpers", raising=False)

        assert [d.dag_id for d in deploy._load_dag_from_file(tmp_path / "dag.py")] == ["orders"]

    def test_a_dag_whose_block_raised_does_not_count(self, tmp_path):
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            try:
                with DAG("optional", schedule="@daily"):
                    raise ImportError("an optional dependency is missing")
            except ImportError:
                pass
            with DAG("real", schedule="@daily"):
                @task.sfn(arn=ARN)
                def a(): pass
                a()
        """))
        assert [d.dag_id for d in deploy._load_dag_from_file(tmp_path / "dag.py")] == ["real"]

    def test_a_dag_made_in_a_worker_thread_the_file_starts_counts(self, tmp_path):
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            import threading
            from polyris import DAG, task
            def build():
                with DAG("threaded", schedule="@daily"):
                    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                    def a(): pass
                    a()
            worker = threading.Thread(target=build)
            worker.start()
            worker.join()
        """))
        assert [d.dag_id for d in deploy._load_dag_from_file(tmp_path / "dag.py")] == ["threaded"]

    def test_a_file_that_loads_another_gets_its_dags_too(self, tmp_path):
        """Airflow-style aggregation (a file that runs other files) is the file's code, not an import."""
        (tmp_path / "other.py").write_text(textwrap.dedent("""
            from polyris import DAG
            with DAG("other", schedule="@daily"):
                pass
        """))
        (tmp_path / "dag.py").write_text(textwrap.dedent(f"""
            from polyris import DAG
            from polyris.loader import load_pipeline
            with DAG("before", schedule="@daily"):
                pass
            assert [d.dag_id for d in load_pipeline(r"{tmp_path / 'other.py'}").dags] == ["other"]
            with DAG("after", schedule="@daily"):
                pass
        """))
        assert [d.dag_id for d in deploy._load_dag_from_file(tmp_path / "dag.py")] == ["before", "other", "after"]

    def test_the_dag_class_is_restored_when_the_file_fails(self, tmp_path):
        from polyris.dag import DAG
        original = DAG.__exit__
        (tmp_path / "dag.py").write_text("raise RuntimeError('boom')\n")
        with pytest.raises(SystemExit):
            deploy._load_dag_from_file(tmp_path / "dag.py")
        assert DAG.__exit__ is original

    def test_systemexit_zero_is_tolerated(self, tmp_path):
        """exit(0) in a pipeline file must not abort loading."""
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("d", schedule="@daily") as dag:
                @task.sfn(arn=ARN)
                def go(): pass
                go()
            import sys; sys.exit(0)
        """))
        dags = deploy._load_dag_from_file(tmp_path / "dag.py")
        assert [d.dag_id for d in dags] == ["d"]

    def test_systemexit_nonzero_is_a_load_error(self, tmp_path, capsys):
        """exit(1) in a pipeline file must propagate as a load error."""
        (tmp_path / "dag.py").write_text("import sys; sys.exit(1)\n")
        with pytest.raises(SystemExit) as exc:
            deploy._load_dag_from_file(tmp_path / "dag.py")
        assert exc.value.code == 1
        assert "Error loading pipeline" in capsys.readouterr().out


class TestRunBulk:
    @pytest.fixture(autouse=True)
    def _folders(self, mocker):
        self.requests = _folders_run_here(mocker)

    def test_deploys_every_dag_across_two_directories(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        (tmp_path / "dir_b").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        _write_dag_file(tmp_path / "dir_b" / "dag.py", "dag-b")

        deployed = mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(
                ["dir_a", "dir_b"], select=None, stage=None, region=None,
                dry_run=False, destroy=False, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        assert exc.value.code == 0
        assert deployed.call_count == 2
        deployed_ids = {c.kwargs["dag"].dag_id for c in deployed.call_args_list}
        assert deployed_ids == {"dag-a", "dag-b"}

    def test_one_failure_does_not_stop_the_rest(self, tmp_path, mocker, monkeypatch):
        """The core promise of --all/--only: a bad DAG doesn't take the whole
        batch down with it."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        (tmp_path / "dir_b").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "will-fail")
        _write_dag_file(tmp_path / "dir_b" / "dag.py", "will-succeed")

        def _side_effect(dag, **kwargs):
            if dag.dag_id == "will-fail":
                raise SystemExit(1)

        deployed = mocker.patch("polyris.deploy.deploy_pipeline", side_effect=_side_effect)

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(
                ["dir_a", "dir_b"], select=None, stage=None, region=None,
                dry_run=False, destroy=False, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        # Overall exit is non-zero (something failed)...
        assert exc.value.code == 1
        # ...but BOTH dags were attempted — dir_b was not skipped because
        # dir_a's dag failed first.
        assert deployed.call_count == 2

    def test_select_filters_within_each_directory(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "multi").mkdir()
        (tmp_path / "multi" / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            with DAG("keep-me", schedule="@daily") as dag_a:
                @task.sfn(arn=ARN)
                def a(): pass
                a()
            with DAG("skip-me", schedule="@daily") as dag_b:
                @task.sfn(arn=ARN)
                def b(): pass
                b()
        """))
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(
                ["multi"], select="keep-me", stage=None, region=None,
                dry_run=False, destroy=False, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        assert exc.value.code == 0
        assert deployed.call_count == 1
        assert deployed.call_args.kwargs["dag"].dag_id == "keep-me"

    def test_directory_without_the_selected_dag_is_skipped_not_an_error(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "has_it").mkdir()
        (tmp_path / "lacks_it").mkdir()
        _write_dag_file(tmp_path / "has_it" / "dag.py", "target-dag")
        _write_dag_file(tmp_path / "lacks_it" / "dag.py", "other-dag")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(
                ["has_it", "lacks_it"], select="target-dag", stage=None, region=None,
                dry_run=False, destroy=False, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        assert exc.value.code == 0
        assert deployed.call_count == 1

    def test_a_file_that_does_not_load_fails_the_batch(self, tmp_path, mocker, monkeypatch, capsys):
        """F18: a refused file (here one dag_id twice) was skipped, and the batch said 0 failed."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "p1").mkdir()
        (tmp_path / "p1" / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            for schedule in ("@daily", "@hourly"):
                with DAG("orders", schedule=schedule):
                    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                    def a(): pass
                    a()
        """))
        (tmp_path / "p2").mkdir()
        _write_dag_file(tmp_path / "p2" / "dag.py", "fine")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(
                ["p1", "p2"], select=None, stage=None, region=None,
                dry_run=False, destroy=True, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        assert exc.value.code == 1
        assert [c.kwargs["dag"].dag_id for c in deployed.call_args_list] == ["fine"]
        assert "1 succeeded, 1 failed" in capsys.readouterr().out

    def test_nonexistent_directory_is_reported_as_a_failure(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(
                ["does_not_exist"], select=None, stage=None, region=None,
                dry_run=False, destroy=False, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        assert exc.value.code == 1

    def test_destroy_flag_is_passed_through_to_deploy_pipeline(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit):
            deploy._run_bulk(
                ["dir_a"], select=None, stage=None, region=None,
                dry_run=False, destroy=True, log_level="ERROR",
                log_retention_days=30, profile=None,
            )
        assert deployed.call_args.kwargs["destroy"] is True

    def test_every_option_reaches_the_folders_process(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "-dash").mkdir()  # a name argparse would take for a flag: no argv to parse now
        _write_dag_file(tmp_path / "-dash" / "dag.py", "dag-a")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(["-dash"], select="dag-a", stage="dev", region="eu-west-1", dry_run=True,
                             destroy=False, log_level="ALL", log_retention_days=7, profile="p", stack="s")
        assert exc.value.code == 0
        assert {k: v for k, v in deployed.call_args.kwargs.items() if k != "dag"} == dict(
            stage="dev", stack="s", region="eu-west-1", dry_run=True, destroy=False,
            log_level="ALL", log_retention_days=7, profile="p")

    def test_a_dag_id_two_files_make_stops_the_run_before_anything_is_deployed(self, tmp_path, mocker, monkeypatch, capsys):
        """D111: each DAG is one stack, so the second deploy would replace the first. Two files of
        one folder, or two folders: nothing is deployed (before, a warning or nothing at all)."""
        monkeypatch.chdir(tmp_path)
        for folder, name in (("a", "dag.py"), ("a", "second.py"), ("b", "dag.py")):
            (tmp_path / folder).mkdir(exist_ok=True)
            _write_dag_file(tmp_path / folder / name, "orders" if folder == "a" else "billing")
        (tmp_path / "c").mkdir()
        _write_dag_file(tmp_path / "c" / "dag.py", "billing")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(["a", "b", "c"], select=None, stage=None, region=None, dry_run=False,
                             destroy=True, log_level="ERROR", log_retention_days=30, profile=None)
        assert exc.value.code == 1
        assert deployed.call_count == 0
        out = capsys.readouterr().out
        assert "dag_id 'billing' is made by b/dag.py and c/dag.py" in out
        assert "dag_id 'orders' is made by a/dag.py and a/second.py" in out
        assert "Nothing was destroyed" in out
        assert [r["command"] for r in self.requests] == ["collect"] * 3  # no deploy started

    def test_a_folder_whose_dag_ids_could_not_be_checked_is_not_deployed(self, tmp_path, mocker, monkeypatch, capsys):
        """A folder whose process died while its dag_ids were collected skipped the duplicate check,
        and was then deployed anyway."""
        from polyris import folder_process
        monkeypatch.chdir(tmp_path)
        for folder in ("a", "b"):
            (tmp_path / folder).mkdir()
            _write_dag_file(tmp_path / folder / "dag.py", f"dag-{folder}")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        real_run = folder_process.run
        def run(command, request, **kwargs):
            if command == "collect" and request["files"][0].startswith("a"):
                return folder_process.Answer(None, "its process exited with code 9 before reporting")
            return real_run(command, request, **kwargs)
        mocker.patch("polyris.folder_process.run", side_effect=run)
        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(["a", "b", "a"], select=None, stage=None, region=None, dry_run=False,
                             destroy=False, log_level="ERROR", log_retention_days=30, profile=None)
        assert exc.value.code == 1
        assert [c.kwargs["dag"].dag_id for c in deployed.call_args_list] == ["dag-b"]  # "a" once, not deployed
        out = capsys.readouterr().out
        assert "a/-: not deployed: its dag_ids could not be checked (its process exited with code 9" in out
        assert "1 succeeded, 1 failed" in out

    def test_a_dag_id_made_twice_does_not_stop_a_run_that_selects_another(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for folder in ("a", "b"):
            (tmp_path / folder).mkdir()
            _write_dag_file(tmp_path / folder / "dag.py", "orders")
        _write_dag_file(tmp_path / "a" / "billing.py", "billing")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(["a", "b"], select="billing", stage=None, region=None, dry_run=False,
                             destroy=False, log_level="ERROR", log_retention_days=30, profile=None)
        assert exc.value.code == 0
        assert [c.kwargs["dag"].dag_id for c in deployed.call_args_list] == ["billing"]


def _folders_run_here(mocker):
    """Run each folder's process (``python -P -m polyris.folder_process``) in this one, through
    the subprocess boundary: the request and answer files are the ones a real process gets.
    Returns the requests, in order."""
    import json
    import types
    from pathlib import Path
    from polyris import folder_process

    requests = []

    def popen(cmd, env, stdout=None, stderr=None):
        assert cmd[1:4] == ["-P", "-m", "polyris.folder_process"]
        requests.append(json.loads(Path(cmd[4]).read_text()))
        code = folder_process.main(cmd[4:])
        return types.SimpleNamespace(wait=lambda: code)

    mocker.patch("polyris.folder_process.subprocess.Popen", side_effect=popen)
    return requests


class TestBulkRealProcesses:
    """--all through real child processes, on folders that never reach AWS."""

    def test_each_folder_runs_in_a_real_process_and_its_results_come_back(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "-dash").mkdir()  # a name argparse would take for a flag
        (tmp_path / "-dash" / "dag.py").write_text("from polyris import DAG\nraise RuntimeError('broken on purpose')\n")
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "readme.txt").write_text("not a pipeline\n")

        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(["-dash", "notes"], select=None, stage=None, region=None, dry_run=False,
                             destroy=True, log_level="ERROR", log_retention_days=30, profile=None)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "0 succeeded, 1 failed" in out and "-dash/dag.py: failed to load" in out

    def test_a_folder_whose_process_ends_without_results_fails_even_with_exit_code_zero(self, tmp_path, monkeypatch, capsys):
        """Such a folder vanished from the summary and the run exited 0."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "quiet").mkdir()
        (tmp_path / "quiet" / "dag.py").write_text("import os\nfrom polyris import DAG\nos._exit(0)\n")
        with pytest.raises(SystemExit) as exc:
            deploy._run_bulk(["quiet"], select=None, stage=None, region=None, dry_run=False,
                             destroy=True, log_level="ERROR", log_retention_days=30, profile=None)
        assert exc.value.code == 1
        assert "exited with code 0 before reporting" in capsys.readouterr().out


def test_destroys_help_describes_the_destroy(monkeypatch, capsys):
    """polyris-destroy --help said "Deploy Polyris pipeline via CloudFormation" and listed
    --destroy (the docs audit after 1.3)."""
    monkeypatch.setattr("sys.argv", ["polyris-destroy", "--help"])
    with pytest.raises(SystemExit) as exc:
        deploy.main(force_destroy=True)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Remove a Polyris pipeline" in out and "polyris-destroy --stage prod" in out
    assert "Deploy Polyris pipeline" not in out and "--destroy" not in out


class TestStageReachesThePipelineFile:
    """``--stage`` names the stage of the pipeline file too: a file that picks its ARNs by
    ``POLYRIS_STAGE`` took the shell's stage, or its default, under ``polyris-deploy --stage prod``
    (the docs audit after 1.3)."""

    def test_one_file(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("POLYRIS_STAGE", "dev")
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            import os
            from polyris import DAG, task

            with DAG(f"etl-{os.environ['POLYRIS_STAGE']}", schedule="@daily") as dag:
                @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
                def go():
                    pass
                go()
        """))
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--stage", "prod"])
        deploy.main()
        assert deployed.call_args.kwargs["dag"].dag_id == "etl-prod"
        assert deployed.call_args.kwargs["stage"] == "prod"

    def test_every_folders_process_of_all(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("POLYRIS_STAGE", "dev")
        (tmp_path / "dir_a").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--all", "--stage", "prod"])
        requests = _folders_run_here(mocker)
        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 0
        assert [(r["command"], r["stage"]) for r in requests] == [("collect", "prod"), ("deploy", "prod")]
        assert deployed.call_args.kwargs["stage"] == "prod"


class TestMainCliBulkFlags:
    def test_all_and_only_are_mutually_exclusive(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--all", "--only", "dir1"])
        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 2  # argparse's own usage-error exit code
        assert "not allowed with" in capsys.readouterr().err

    def test_file_rejected_together_with_all(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--all", "--file", "custom.py"])
        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 1
        assert "--file is not used with --all/--only" in capsys.readouterr().out

    def test_all_discovers_every_immediate_subdirectory(self, tmp_path, mocker, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        (tmp_path / "dir_b").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        _write_dag_file(tmp_path / "dir_b" / "dag.py", "dag-b")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / ".venv").mkdir()
        _write_dag_file(tmp_path / "root_dag.py", "at-the-root")  # --all does not take it: warned
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--all"])
        requests = _folders_run_here(mocker)

        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 0
        assert deployed.call_count == 2
        # dag_ids first, then the deploys; no process for __pycache__ or .venv
        assert [(r["command"], r.get("dir_name")) for r in requests] == [
            ("collect", None), ("collect", None), ("deploy", "dir_a"), ("deploy", "dir_b")]
        assert "root_dag.py: not loaded by --all" in capsys.readouterr().out

    def test_sigterm_stops_children_in_the_parent_and_ends_a_folders_process_by_default(self, tmp_path, mocker, monkeypatch):
        """With the handler set in a folder's process too, SIGTERM there became one
        DAG's failure and the process went on deploying the folder's other DAGs."""
        import json
        import signal
        from polyris import folder_process
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        in_the_child = []
        mocker.patch("polyris.deploy.deploy_pipeline",
                     side_effect=lambda *a, **k: in_the_child.append(signal.getsignal(signal.SIGTERM)))
        in_the_parent = []
        mocker.patch("polyris.folder_process._run_child",
                     side_effect=lambda *a, **k: in_the_parent.append(signal.getsignal(signal.SIGTERM)) or 0)
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--all"])
        with pytest.raises(SystemExit):
            deploy.main()
        # The dag_ids check (here it answers nothing, so the folder is not deployed after it)
        assert in_the_parent == [folder_process._raise_on_sigterm]

        # The folder's process itself keeps SIGTERM's default
        request, answer = tmp_path / "request.json", tmp_path / "answer.json"
        request.write_text(json.dumps({"command": "deploy", "dir_name": "dir_a", "select": None, "stage": None,
                                       "region": None, "dry_run": False, "destroy": False, "log_level": "ERROR",
                                       "log_retention_days": 30, "profile": None, "stack": None}))
        assert folder_process.main([str(request), str(answer)]) == 0
        assert in_the_child == [signal.SIG_DFL]
        assert json.loads(answer.read_text()) == [["dir_a", "dag-a", True, None]]

    def test_only_discovers_just_the_listed_directories(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        (tmp_path / "dir_b").mkdir()
        (tmp_path / "dir_c").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        _write_dag_file(tmp_path / "dir_b" / "dag.py", "dag-b")
        _write_dag_file(tmp_path / "dir_c" / "dag.py", "dag-c")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--only", "dir_a", "dir_c"])
        requests = _folders_run_here(mocker)

        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 0
        assert deployed.call_count == 2
        deployed_ids = {c.kwargs["dag"].dag_id for c in deployed.call_args_list}
        assert deployed_ids == {"dag-a", "dag-c"}
        # One fresh process per folder (ADR #139)
        assert [r["dir_name"] for r in requests if r["command"] == "deploy"] == ["dir_a", "dir_c"]

    def test_single_file_mode_is_unaffected_when_no_bulk_flag_given(self, tmp_path, mocker, monkeypatch):
        """Control: the pre-existing, single-directory behavior must not
        change at all when --all/--only aren't used."""
        monkeypatch.chdir(tmp_path)
        _write_dag_file(tmp_path / "dag.py", "solo")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        monkeypatch.setattr("sys.argv", ["polyris-deploy"])

        deploy.main()  # does NOT sys.exit on the plain success path

        assert deployed.call_count == 1
        assert deployed.call_args.kwargs["dag"].dag_id == "solo"

    def test_destroy_of_a_file_with_several_dags_says_it_destroys(self, tmp_path, mocker, monkeypatch, capsys):
        """F12: polyris-destroy said "Deploying all..." for a file with several DAGs."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dag.py").write_text(textwrap.dedent("""
            from polyris import DAG, task
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:x"
            for name in ("one", "two"):
                with DAG(name, schedule="@daily"):
                    @task.sfn(arn=ARN)
                    def go(): pass
                    go()
        """))
        destroyed = mocker.patch("polyris.deploy.deploy_pipeline")
        monkeypatch.setattr("sys.argv", ["polyris-destroy"])

        deploy.main(force_destroy=True)

        out = capsys.readouterr().out
        assert "Destroying all..." in out and "Deploying all" not in out
        assert [c.kwargs["dag"].dag_id for c in destroyed.call_args_list] == ["one", "two"]
