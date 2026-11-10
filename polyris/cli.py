"""polyris CLI — command index / help dispatch.

This is the bare ``polyris`` entry point. Its sole job is to print the list of
available commands; it performs no operations itself. Each real command is its
own console script (``polyris-init``, ``polyris-deploy``, …) wired in
``pyproject.toml``.
"""

from __future__ import annotations

import sys
from typing import Optional

from ._ansi import c as _c, BOLD, CYAN


def _col(name: str, width: int) -> str:
    """Return ``name`` wrapped in CYAN and padded to a VISIBLE ``width``.

    Python's ``:<30`` format spec counts invisible ANSI escape bytes toward the
    column count, so ``f"{_c('polyris-init', CYAN):<30}"`` under-pads in TTY
    mode (string is 21 raw chars but only 12 visible) and over-pads in plain
    mode (string is 12 raw chars). Compute padding from the visible name length
    so both modes align identically.
    """
    return _c(name, CYAN) + " " * max(0, width - len(name))


def _build_help_text() -> str:
    """Build the ``polyris`` command-index help, colored when stdout is a TTY.

    Constructed at call time (not module import) so that ``_c()``'s isatty
    check reflects the actual stdout state of the running invocation —
    important for tests that patch isatty and for pipes that redirect output.
    """
    rule = "═════════════════════════════════════════════════"
    return (
        f"{_c('polyris', BOLD)} - Serverless Data Pipeline Orchestration\n"
        f"{rule}\n"
        "\n"
        f"{_c('Available commands:', BOLD)}\n"
        f"  {_col('polyris-init', 20)} Create a new pipeline or project config\n"
        f"  {_col('polyris-deploy', 20)} Deploy pipeline via CloudFormation\n"
        f"  {_col('polyris-validate', 20)} Validate pipeline(s) for errors\n"
        f"  {_col('polyris-output', 20)} Generate pipeline artifacts (JSON, Mermaid, graph)\n"
        f"  {_col('polyris-register', 20)} Register pipeline in DynamoDB\n"
        f"  {_col('polyris-build-lambda', 20)} Stamp XCom helpers into a Lambda handler directory\n"
        "\n"
        "Each command has its own --help. Run e.g. `polyris-deploy --help`.\n"
        "\n"
        f"{_c('Documentation:', BOLD)}\n"
        "  https://github.com/Polyris/polyris\n"
    )


def main(argv: Optional[list] = None) -> int:
    """Print the command index. Always succeeds — this entry point performs
    no work itself; the real commands are the separate ``polyris-*`` scripts."""
    print(_build_help_text())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
