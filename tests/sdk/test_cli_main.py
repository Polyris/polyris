"""CLI entry-point tests for the bare ``polyris`` command index (ADR #104).

``polyris.cli.main`` performs no work — it prints the list of available
commands and exits 0. Backfill moved to ``polyris-backfill`` in the Team
edition, so the former HTTP / dispatch / KeyboardInterrupt paths now live in
the proprietary ``polyris-ee`` package's tests.
"""
from __future__ import annotations

from polyris import cli


class TestCliMain:
    def test_prints_command_index_and_returns_zero(self, capsys):
        rc = cli.main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Available commands:" in out
        # The real OSS commands are listed. polyris-backfill ships separately in
        # polyris-ee and is not advertised in the open-source CLI index.
        assert "polyris-deploy" in out
        assert "polyris-backfill" not in out

    def test_ignores_arguments_and_still_prints_index(self, capsys):
        # The bare command takes no subcommands; any argv just prints the index.
        rc = cli.main(["anything", "--here"])
        assert rc == 0
        assert "Available commands:" in capsys.readouterr().out
