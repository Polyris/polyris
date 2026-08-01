"""polyris CLI — command index / help dispatch.

This is the bare ``polyris`` entry point. Its sole job is to print the list of
available commands; it performs no operations itself. Each real command is its
own console script (``polyris-init``, ``polyris-deploy``, …) wired in
``pyproject.toml``.
"""

from __future__ import annotations

import sys
from typing import Optional


HELP_TEXT = """\
polyris - Serverless Data Pipeline Orchestration
═════════════════════════════════════════════════

Available commands:
  polyris-init         Create a new pipeline or project config
  polyris-deploy       Deploy pipeline via CloudFormation
  polyris-validate     Validate pipeline(s) for errors
  polyris-output       Generate pipeline artifacts (JSON, Mermaid, graph)
  polyris-register     Register pipeline in DynamoDB

Each command has its own --help. Run e.g. `polyris-deploy --help`.

Documentation:
  https://github.com/Polyris/polyris
"""


def main(argv: Optional[list] = None) -> int:
    """Print the command index. Always succeeds — this entry point performs
    no work itself; the real commands are the separate ``polyris-*`` scripts."""
    print(HELP_TEXT)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
