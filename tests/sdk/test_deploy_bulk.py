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
        (tmp_path / "broken_dag.py").write_text("this is not ) valid python (((")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert [d.dag_id for _, d in found] == ["real-one"]
        assert "Skipping" in capsys.readouterr().out

    def test_a_file_that_is_not_a_pipeline_is_never_run(self, tmp_path, capsys):
        """Third review: a Glue script next to dag.py failed every batch; validate --all never ran it."""
        _write_dag_file(tmp_path / "dag.py", "real-one")
        (tmp_path / "glue_job.py").write_text("import awsglue  # only on Glue\n")
        failed: list = []
        found = deploy._discover_dags_in_dir(tmp_path, failed)
        assert [d.dag_id for _, d in found] == ["real-one"]
        assert failed == [] and "Skipping" not in capsys.readouterr().out

    def test_empty_directory_returns_nothing(self, tmp_path):
        assert deploy._discover_dags_in_dir(tmp_path) == []

    def test_duplicate_dag_id_across_files_is_warned_and_skipped(self, tmp_path, capsys):
        """Same dag_id in two files: first wins, second is warned and dropped."""
        _write_dag_file(tmp_path / "a.py", "shared-id")
        _write_dag_file(tmp_path / "b.py", "shared-id")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert len(found) == 1
        assert found[0][1].dag_id == "shared-id"
        out = capsys.readouterr().out
        assert "shared-id" in out

    def test_file_that_exits_nonzero_is_skipped_with_warning(self, tmp_path, capsys):
        """A pipeline file that calls sys.exit(1) is treated as a load error."""
        _write_dag_file(tmp_path / "good.py", "good-dag")
        (tmp_path / "bad_pipeline.py").write_text("import sys; sys.exit(1)\n")
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
        """polyris-validate finds these; the deploy must too (review of F18)."""
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

    def test_ownership_is_decided_by_the_call_chain(self):
        """No frame of the module and no import on the stack: a thread it started (ADR #139)."""
        import types
        from polyris.dag import _made_by
        mine: dict = {}
        frame = lambda globals_, back=None: types.SimpleNamespace(f_globals=globals_, f_back=back)  # noqa: E731
        assert _made_by(None, mine) is True
        assert _made_by(frame({"__name__": "common"}, frame(mine)), mine) is True
        assert _made_by(frame({"__name__": "helpers"}, frame({"__name__": "importlib._bootstrap"}, frame(mine))), mine) is False

    def test_a_helper_that_imports_the_pipeline_file_back_does_not_make_a_second_dag(self, tmp_path, monkeypatch):
        """Second review: the file ran again as module `dag`; matched by file name, it counted twice."""
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

    def test_loads_nest_and_each_keeps_its_own_dags(self, tmp_path):
        """A pipeline file that loads another one: each load records the DAGs its own file made."""
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
        assert [d.dag_id for d in deploy._load_dag_from_file(tmp_path / "dag.py")] == ["before", "after"]

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
        """Review of F18: a refused file (here one dag_id twice) was skipped, and the batch said 0 failed."""
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

    def test_all_discovers_every_immediate_subdirectory(self, tmp_path, mocker, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "dir_a").mkdir()
        (tmp_path / "dir_b").mkdir()
        _write_dag_file(tmp_path / "dir_a" / "dag.py", "dag-a")
        _write_dag_file(tmp_path / "dir_b" / "dag.py", "dag-b")
        deployed = mocker.patch("polyris.deploy.deploy_pipeline")
        monkeypatch.setattr("sys.argv", ["polyris-deploy", "--all"])

        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 0
        assert deployed.call_count == 2

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

        with pytest.raises(SystemExit) as exc:
            deploy.main()
        assert exc.value.code == 0
        assert deployed.call_count == 2
        deployed_ids = {c.kwargs["dag"].dag_id for c in deployed.call_args_list}
        assert deployed_ids == {"dag-a", "dag-c"}

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
