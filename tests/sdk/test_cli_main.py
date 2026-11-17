"""CLI entry-point tests for the bare ``polyris`` command index (ADR #104).

``polyris.cli.main`` performs no work — it prints the list of available
commands, or the version for ``--version``, and exits 0.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import polyris
from polyris import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestCliMain:
    def test_prints_command_index_and_returns_zero(self, capsys):
        rc = cli.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Available commands:" in out
        # Every command this package installs is listed, and nothing else.
        scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
        listed = {name for name in scripts if name != "polyris"}
        assert {name for name in listed if f"  {name} " in out} == listed
        assert "polyris-backfill" not in out

    def test_version_prints_the_installed_version_and_nothing_else(self, capsys):
        rc = cli.main(["--version"])
        assert rc == 0
        assert capsys.readouterr().out == f"polyris {polyris.__version__}\n"

    def test_the_index_names_the_version_and_how_to_print_it(self, capsys):
        cli.main([])
        out = capsys.readouterr().out
        assert polyris.__version__ in out.splitlines()[0]
        assert "polyris --version" in out

    def test_ignores_arguments_and_still_prints_index(self, capsys):
        # The bare command takes no subcommands; any argv just prints the index.
        rc = cli.main(["anything", "--here"])
        assert rc == 0
        assert "Available commands:" in capsys.readouterr().out

    def test_a_malformed_version_flag_still_prints_the_index(self, capsys):
        assert cli.main(["--version=1"]) == 0
        assert "Available commands:" in capsys.readouterr().out
