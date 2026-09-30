"""Tests for polyris.config project-config loading.

Covers the contract that a *broken* config.py is surfaced (ADR #38: no silent
swallow) rather than producing an empty config with no feedback, while a missing
config stays quiet and a valid one loads.
"""
from __future__ import annotations

from polyris.config import _load_project_config, _find_project_config, _has_environments_assignment


def test_broken_config_is_surfaced_not_silently_swallowed(tmp_path, monkeypatch, capsys):
    # A config.py that exists but fails to import must not silently fall back
    # to an empty config — the user gets a visible warning naming the file.
    (tmp_path / "config.py").write_text(
        "ENVIRONMENTS = {\n"          # deliberately broken: unterminated dict
        "    'dev': {'stage': 'dev'\n"
    )
    monkeypatch.chdir(tmp_path)

    result = _load_project_config()

    assert result == {}                      # resilient: CLI doesn't crash
    err = capsys.readouterr().out
    assert "config.py" in err                # but the failure is visible
    assert "Failed to load" in err or "Skipping" in err


def test_valid_config_loads(tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text(
        "ENVIRONMENTS = {'dev': {'stage': 'dev'}, 'prod': {'stage': 'prod'}}\n"
        "DEFAULT_STAGE = 'prod'\n"
    )
    monkeypatch.chdir(tmp_path)

    result = _load_project_config()

    assert result["environments"] == {"dev": {"stage": "dev"}, "prod": {"stage": "prod"}}
    assert result["default_stage"] == "prod"


def test_missing_config_returns_empty_quietly(tmp_path, monkeypatch, capsys):
    # No config.py anywhere up the tree is a normal case (e.g. running outside a
    # project) — it must stay silent, not warn.
    (tmp_path / "pyproject.toml").write_text("[project]\n")  # boundary so we don't walk past tmp
    monkeypatch.chdir(tmp_path)

    result = _load_project_config()

    assert result == {}
    assert capsys.readouterr().out == ""


def test_arbitrary_config_py_not_executed_during_discovery(tmp_path, monkeypatch):
    # A config.py without ENVIRONMENTS must never be executed during discovery —
    # only ast.parse'd.  We detect execution by writing a file that errors on import.
    (tmp_path / "config.py").write_text("raise RuntimeError('should not be executed')\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    monkeypatch.chdir(tmp_path)

    result = _find_project_config()  # must not raise

    assert result is None


def test_walk_stops_at_git_boundary(tmp_path, monkeypatch):
    # A config.py above the .git boundary must be ignored.
    outer = tmp_path / "outer"
    inner = tmp_path / "outer" / "project"
    inner.mkdir(parents=True)
    (outer / "config.py").write_text("ENVIRONMENTS = {'prod': {}}\n")
    (inner / ".git").mkdir()
    monkeypatch.chdir(inner)

    result = _find_project_config()

    assert result is None


def test_walk_stops_at_pyproject_boundary(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    inner = tmp_path / "outer" / "project"
    inner.mkdir(parents=True)
    (outer / "config.py").write_text("ENVIRONMENTS = {'prod': {}}\n")
    (inner / "pyproject.toml").write_text("[project]\n")
    monkeypatch.chdir(inner)

    result = _find_project_config()

    assert result is None


def test_polyris_config_env_var_used_directly(tmp_path, monkeypatch):
    cfg = tmp_path / "my_config.py"
    cfg.write_text("ENVIRONMENTS = {'dev': {}}\nDEFAULT_STAGE = 'dev'\n")
    monkeypatch.setenv("POLYRIS_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path / "..")  # cwd has no config.py

    result = _find_project_config()

    assert result == cfg


def test_has_environments_assignment_detects_assignment(tmp_path):
    f = tmp_path / "config.py"
    f.write_text("ENVIRONMENTS = {'dev': {}}\n")
    assert _has_environments_assignment(f) is True


def test_has_environments_assignment_detects_annotated_assignment(tmp_path):
    # ENVIRONMENTS: dict = {...} is ast.AnnAssign, not ast.Assign
    f = tmp_path / "config.py"
    f.write_text("ENVIRONMENTS: dict = {'dev': {}}\n")
    assert _has_environments_assignment(f) is True


def test_has_environments_assignment_detects_tuple_unpacking(tmp_path):
    # X, ENVIRONMENTS = ... — ENVIRONMENTS is inside an ast.Tuple target
    f = tmp_path / "config.py"
    f.write_text("_UNUSED, ENVIRONMENTS = None, {'dev': {}}\n")
    assert _has_environments_assignment(f) is True


def test_has_environments_assignment_ignores_non_environments(tmp_path):
    f = tmp_path / "config.py"
    f.write_text("OTHER = {'dev': {}}\n")
    assert _has_environments_assignment(f) is False


def test_has_environments_assignment_ignores_attribute_target(tmp_path):
    # some_mod.ENVIRONMENTS = {} — ast.Attribute target, not ast.Name
    f = tmp_path / "config.py"
    f.write_text("some_mod.ENVIRONMENTS = {'dev': {}}\n")
    assert _has_environments_assignment(f) is False


def test_polyris_config_env_var_nonexistent_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYRIS_CONFIG", str(tmp_path / "does_not_exist.py"))
    assert _find_project_config() is None


def test_polyris_config_env_var_syntax_error_surfaced(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "bad_config.py"
    cfg.write_text("ENVIRONMENTS = {\n")  # unterminated
    monkeypatch.setenv("POLYRIS_CONFIG", str(cfg))

    result = _find_project_config()

    assert result is None
    assert "Skipping" in capsys.readouterr().out


def test_syntax_error_config_is_surfaced(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.py").write_text("ENVIRONMENTS = {\n")  # unterminated
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    monkeypatch.chdir(tmp_path)

    result = _find_project_config()

    assert result is None
    assert "Skipping" in capsys.readouterr().out


def test_polyris_config_env_var_without_environments_is_skipped(tmp_path, monkeypatch, capsys):
    # POLYRIS_CONFIG pointing to a file with no ENVIRONMENTS must not be executed
    cfg = tmp_path / "not_a_config.py"
    cfg.write_text("raise RuntimeError('should not be executed')\n")
    monkeypatch.setenv("POLYRIS_CONFIG", str(cfg))

    result = _find_project_config()

    assert result is None
    assert "POLYRIS_CONFIG" in capsys.readouterr().out


def test_reload_sets_loaded_flag(tmp_path, monkeypatch):
    from polyris.config import PolyrisConfig
    monkeypatch.chdir(tmp_path)
    c = PolyrisConfig()
    c.reset()  # _loaded = False
    c.reload()
    assert PolyrisConfig._loaded is True
