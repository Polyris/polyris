"""Tests for polyris.config project-config loading.

Covers the contract that a *broken* config.py is surfaced (ADR #38: no silent
swallow) rather than producing an empty config with no feedback, while a missing
config stays quiet and a valid one loads.
"""
from __future__ import annotations

from polyris.config import _load_project_config


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
    monkeypatch.chdir(tmp_path)

    result = _load_project_config()

    assert result == {}
    assert capsys.readouterr().out == ""
