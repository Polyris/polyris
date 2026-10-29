"""ANSI terminal color helpers for CLI output.

All color output must go through `_c()` so that piped/non-TTY consumers
never receive raw escape codes.
"""
import sys

BOLD       = "\033[1m"
GREEN      = "\033[32m"
BOLD_GREEN = "\033[1;32m"
CYAN       = "\033[36m"
RED        = "\033[31m"
YELLOW     = "\033[33m"
RESET      = "\033[0m"


def c(text: str, *codes: str) -> str:
    """Wrap *text* in ANSI *codes* when stdout is a real terminal."""
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + RESET
