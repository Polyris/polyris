"""polyris-deploy --all / --only — bulk deploy across pipeline directories.

Each directory is scanned for *every* .py file (not just dag.py), and every DAG
found in every file is deployed — a directory can hold more than one pipeline
file, and a file with zero DAGs (a shared config.py, a utils module) is a
no-op, not an error. One DAG failing must not stop the rest of the batch.
"""
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
        (tmp_path / "broken.py").write_text("this is not ) valid python (((")
        found = deploy._discover_dags_in_dir(tmp_path)
        assert [d.dag_id for _, d in found] == ["real-one"]
        assert "Skipping" in capsys.readouterr().out

    def test_empty_directory_returns_nothing(self, tmp_path):
        assert deploy._discover_dags_in_dir(tmp_path) == []


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
