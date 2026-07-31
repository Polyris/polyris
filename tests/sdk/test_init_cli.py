"""
Tests for `polyris-init` (polyris/init.py) — the wizard, project scaffolding,
and CLI dispatch.

`init_pipeline`'s two template branches are covered in test_init_pipeline.py.
What this file adds is everything around them, which was unmeasured: the
interactive prompts, the wizard's code generation, `init_project`, and `main`'s
argument dispatch.

`input()` is mocked as an external boundary (Principle #14, same class as the
file and time I/O listed there) — everything inside it runs for real.
"""

import sys

import pytest

from polyris.init import (
    _ask,
    _ask_choice,
    _build_custom_pipeline,
    init_pipeline,
    init_project,
    interactive_init,
    main,
    DEPLOY_METHODS,
    SCHEDULES,
)


def _inputs(mocker, *answers):
    """Feed `answers` to successive input() calls."""
    return mocker.patch("builtins.input", side_effect=list(answers))


# ── prompts ──────────────────────────────────────────────────────────────────

class TestAsk:

    def test_returns_what_was_typed(self, mocker):
        _inputs(mocker, "  orders  ")
        assert _ask("Name") == "orders"

    def test_empty_answer_falls_back_to_the_default(self, mocker):
        _inputs(mocker, "   ")
        assert _ask("Name", default="extract") == "extract"

    def test_typed_answer_beats_the_default(self, mocker):
        _inputs(mocker, "custom")
        assert _ask("Name", default="extract") == "custom"

    def test_without_a_default_an_empty_answer_stays_empty(self, mocker):
        _inputs(mocker, "")
        assert _ask("Name") == ""


class TestAskChoice:

    def test_picks_the_numbered_option(self, mocker, capsys):
        _inputs(mocker, "2")
        assert _ask_choice("Schedule:", SCHEDULES) == "@hourly"
        assert "Once per hour" in capsys.readouterr().out

    def test_empty_answer_defaults_to_the_first_choice(self, mocker):
        _inputs(mocker, "")
        assert _ask_choice("How?", DEPLOY_METHODS) == "local"

    def test_invalid_answer_reprompts_until_valid(self, mocker, capsys):
        _inputs(mocker, "99", "x", "2")
        assert _ask_choice("How?", DEPLOY_METHODS) == "cfn"
        assert capsys.readouterr().out.count("Invalid choice") == 2


# ── wizard code generation ───────────────────────────────────────────────────

class TestBuildCustomPipeline:

    def test_emits_one_task_per_entry(self):
        code = _build_custom_pipeline(
            "orders", "@daily",
            [{"name": "extract"}, {"name": "load"}], "cfn",
        )
        assert "def extract()" in code and "def load()" in code

    def test_hyphenated_task_names_become_valid_identifiers(self):
        code = _build_custom_pipeline(
            "orders", "@daily", [{"name": "pull-raw"}], "cfn",
        )
        assert "def pull_raw()" in code
        assert "def pull-raw()" not in code

    def test_retries_are_emitted_only_when_set(self):
        with_retries = _build_custom_pipeline(
            "o", "@daily", [{"name": "a", "retries": 3}], "cfn")
        without = _build_custom_pipeline("o", "@daily", [{"name": "a"}], "cfn")
        assert "retries=3" in with_retries
        assert "retries=" not in without

    def test_default_trigger_rule_is_left_implicit(self):
        code = _build_custom_pipeline(
            "o", "@daily", [{"name": "a", "trigger_rule": "all_success"}], "cfn")
        assert "trigger_rule=" not in code

    def test_non_default_trigger_rule_is_emitted(self):
        code = _build_custom_pipeline(
            "o", "@daily", [{"name": "a", "trigger_rule": "all_done"}], "cfn")
        assert 'trigger_rule="all_done"' in code

    def test_the_generated_pipeline_is_importable_and_valid(self, tmp_path, monkeypatch):
        code = _build_custom_pipeline(
            "orders", "@daily",
            [{"name": "extract"}, {"name": "transform"}, {"name": "load"}], "cfn",
        )
        f = tmp_path / "dag.py"
        f.write_text(code)
        monkeypatch.chdir(tmp_path)
        # The wizard's own output must survive the tool that consumes it.
        from polyris.output import _load_dags
        dags, _ = _load_dags("dag.py")
        assert len(dags) == 1
        assert len(dags[0].tasks) == 3


# ── interactive_init ─────────────────────────────────────────────────────────

class TestInteractiveInit:

    def test_full_wizard_writes_a_valid_pipeline(self, tmp_path, mocker, capsys):
        _inputs(
            mocker,
            "2",          # deploy: cfn
            "1",          # schedule: @daily
            "extract", "0",
            "load", "2",
            "",           # finish task entry
            "y",          # chain sequentially (dependency step)
        )
        interactive_init("orders", base_dir=str(tmp_path))
        dag = tmp_path / "orders" / "dag.py"
        assert dag.exists()
        assert "def extract()" in dag.read_text()
        assert "retries=2" in dag.read_text()

    def test_a_single_task_pipeline_can_be_built(self, tmp_path, mocker):
        # Regression: the suggestion list {1: extract, 2: transform, 3: load}
        # meant Enter accepted a default instead of finishing, so nothing under
        # three tasks was reachable through the wizard.
        _inputs(
            mocker,
            "1",          # deploy: local
            "1",          # schedule
            "only", "0",  # one task
            "",           # Enter now finishes, as the prompt says
        )
        interactive_init("solo", base_dir=str(tmp_path))
        code = (tmp_path / "solo" / "dag.py").read_text()
        assert "def only()" in code
        assert "def transform()" not in code

    def test_the_first_task_still_gets_a_suggestion(self, tmp_path, mocker):
        _inputs(mocker, "1", "1", "", "0", "")
        interactive_init("scaffold", base_dir=str(tmp_path))
        assert "def extract()" in (tmp_path / "scaffold" / "dag.py").read_text()


# ── init_project ─────────────────────────────────────────────────────────────

class TestInitProject:

    def test_writes_config_py_with_the_given_namespace(self, tmp_path, mocker):
        _inputs(mocker, "acme")
        init_project(base_dir=str(tmp_path))
        cfg = (tmp_path / "config.py").read_text()
        assert "acme" in cfg

    def test_empty_namespace_falls_back_to_myorg(self, tmp_path, mocker):
        _inputs(mocker, "   ")
        init_project(base_dir=str(tmp_path))
        assert "myorg" in (tmp_path / "config.py").read_text()

    def test_existing_config_exits_1_rather_than_overwriting(self, tmp_path, capsys):
        (tmp_path / "config.py").write_text("# mine\n")
        with pytest.raises(SystemExit) as e:
            init_project(base_dir=str(tmp_path))
        assert e.value.code == 1
        assert "already exists" in capsys.readouterr().out
        assert (tmp_path / "config.py").read_text() == "# mine\n"


# ── init_pipeline guard ──────────────────────────────────────────────────────

class TestInitPipelineGuard:

    def test_existing_directory_exits_1(self, tmp_path, capsys):
        (tmp_path / "taken").mkdir()
        with pytest.raises(SystemExit) as e:
            init_pipeline("taken", base_dir=str(tmp_path))
        assert e.value.code == 1
        assert "already exists" in capsys.readouterr().out

    def test_local_scaffold_points_at_the_deployable_variant(self, tmp_path, capsys):
        init_pipeline("demo", base_dir=str(tmp_path), deploy_method="local")
        out = capsys.readouterr().out
        # Regression: this section header used to print with nothing under it.
        assert "Ready to deploy?" in out
        assert "polyris-init demo" in out


# ── main() dispatch ──────────────────────────────────────────────────────────

def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["polyris-init", *argv])
    main()


class TestMainDispatch:

    def test_plain_name_scaffolds_a_cfn_pipeline(self, tmp_path, monkeypatch):
        _run(monkeypatch, "orders", "--dir", str(tmp_path))
        assert (tmp_path / "orders" / "dag.py").exists()

    def test_local_flag_is_passed_through(self, tmp_path, monkeypatch, capsys):
        _run(monkeypatch, "orders", "--local", "--dir", str(tmp_path))
        assert "no AWS needed" in capsys.readouterr().out

    def test_schedule_flag_reaches_the_template(self, tmp_path, monkeypatch):
        _run(monkeypatch, "orders", "--schedule", "@hourly", "--dir", str(tmp_path))
        assert "@hourly" in (tmp_path / "orders" / "dag.py").read_text()

    def test_project_flag_scaffolds_config_py(self, tmp_path, monkeypatch, mocker):
        _inputs(mocker, "acme")
        _run(monkeypatch, "--project", "--dir", str(tmp_path))
        assert (tmp_path / "config.py").exists()

    def test_missing_name_is_an_argparse_error(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit) as e:
            _run(monkeypatch, "--dir", str(tmp_path))
        assert e.value.code == 2

    def test_cfn_and_local_are_mutually_exclusive(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit) as e:
            _run(monkeypatch, "orders", "--cfn", "--local", "--dir", str(tmp_path))
        assert e.value.code == 2

    def test_interactive_without_a_name_prompts_for_one(self, tmp_path, monkeypatch, mocker):
        _inputs(mocker, "prompted", "1", "1", "a", "0", "")
        _run(monkeypatch, "-i", "--dir", str(tmp_path))
        assert (tmp_path / "prompted" / "dag.py").exists()

    def test_interactive_with_an_empty_name_exits_1(self, tmp_path, monkeypatch, mocker, capsys):
        _inputs(mocker, "   ")
        with pytest.raises(SystemExit) as e:
            _run(monkeypatch, "-i", "--dir", str(tmp_path))
        assert e.value.code == 1
        assert "name is required" in capsys.readouterr().out


# ── roles re-export ──────────────────────────────────────────────────────────

class TestRolesReExport:
    """polyris/roles.py is a two-line convenience re-export of config.roles.
    It sat at 0% with zero AWS references — nothing imported it in tests, so
    a broken re-export would have shipped silently."""

    def test_roles_mirrors_the_config_roles_mapping(self):
        from polyris.roles import roles
        from polyris.config import config
        # config.roles is a property returning a fresh view each access, so this
        # is equality, not identity.
        assert roles == config.roles

    def test_roles_is_reachable_from_the_documented_import_path(self):
        # docs/reference/CONFIGURATION.md documents `from polyris.roles import roles`.
        import importlib
        mod = importlib.import_module("polyris.roles")
        assert hasattr(mod, "roles")


# ── remaining wizard branches ────────────────────────────────────────────────

class TestExplicitDependencies:

    def test_depends_on_emits_the_edges(self):
        code = _build_custom_pipeline(
            "orders", "@daily",
            [{"name": "a"}, {"name": "b", "depends_on": ["a"]}],
            "cfn",
        )
        assert "a() >> b()" in code

    def test_hyphens_in_dependency_edges_become_identifiers(self):
        code = _build_custom_pipeline(
            "orders", "@daily",
            [{"name": "pull-raw"}, {"name": "push-out", "depends_on": ["pull-raw"]}],
            "cfn",
        )
        assert "pull_raw() >> push_out()" in code

    def test_remote_state_variant_reads_the_stage_from_the_env(self):
        code = _build_custom_pipeline(
            "orders", "@daily", [{"name": "a"}], "cfn", use_remote_state=True,
        )
        assert "POLYRIS_STAGE" in code
        assert "polyris-deploy --stage" in code

    def test_declining_the_chain_asks_per_task_dependencies(self, tmp_path, mocker):
        _inputs(
            mocker,
            "2",            # deploy: cfn
            "1",            # schedule
            "a", "0",
            "b", "0",
            "",             # finish
            "n",            # do NOT keep the sequential chain
            "a",            # b depends on a
        )
        interactive_init("custom-deps", base_dir=str(tmp_path))
        assert "a() >> b()" in (tmp_path / "custom-deps" / "dag.py").read_text()

    def test_answering_none_still_yields_a_sequential_chain(self, tmp_path, mocker):
        """Pins CURRENT behaviour, which contradicts the prompt.

        _build_custom_pipeline cannot distinguish "dependencies were never
        asked about" from "the user explicitly declined them": both arrive as
        an empty depends_on, and the `if not dep_lines and len(tasks) > 1`
        fallback chains them anyway. So answering 'none' to every task still
        produces a chain, and a fan-out of independent tasks is not reachable
        through the wizard. Changing that means giving the function a way to
        tell the two apart — a contract change, not a coverage fix.
        """
        _inputs(
            mocker,
            "2", "1",
            "a", "0",
            "b", "0",
            "",
            "n",            # define dependencies manually
            "none",         # b depends on nothing...
        )
        interactive_init("no-deps", base_dir=str(tmp_path))
        assert "a() >> b()" in (tmp_path / "no-deps" / "dag.py").read_text()

    def test_wizard_exits_1_when_the_directory_exists(self, tmp_path, mocker, capsys):
        (tmp_path / "taken").mkdir()
        _inputs(mocker, "1", "1", "a", "0", "")
        with pytest.raises(SystemExit) as e:
            interactive_init("taken", base_dir=str(tmp_path))
        assert e.value.code == 1
        assert "already exists" in capsys.readouterr().out
