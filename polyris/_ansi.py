"""ANSI terminal color helpers for CLI output.

All color output must go through `_c()` so that piped/non-TTY consumers
never receive raw escape codes.
"""
import re
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


# ---- argparse --help colorizer ----------------------------------------------

# Section headers argparse emits (plus a few we add via ``epilog=``). Must be
# the whole line prefix — ``usage:`` is also matched when it leads the output.
_HELP_SECTION_RE = re.compile(
    r'^(usage|options|positional arguments|Examples|Commands|Documentation):',
    re.MULTILINE,
)
# Option flags in the help body. argparse formats them as ``  -h, --help`` or
# ``  --all, -a`` or ``  --file FILE, -f FILE`` — the long/short order depends
# on definition order. This matches leading indent + a comma-separated chain of
# short or long flags, each optionally followed by an UPPER_ARG placeholder,
# up to the 2-space gap that separates flags from the description. The outer
# lookahead guards against matching positional-arg lines and epilog examples.
_HELP_OPTION_RE = re.compile(
    r'(^\s{2,})((?:--?[\w-]+(?:\s+[A-Z_]+)?)(?:,\s+--?[\w-]+(?:\s+[A-Z_]+)?)*)(?=\s{2,}|$)',
    re.MULTILINE,
)


def colorize_argparse_help(text: str) -> str:
    """Add BOLD to section headers and CYAN to option flags in a help string.

    No-op when stdout isn't a real terminal — piping ``polyris-deploy --help``
    to ``less`` or a file must never produce raw escape codes.

    Applied via :func:`colorize_parser`, which replaces ``parser.format_help``.
    """
    if not sys.stdout.isatty():
        return text
    text = _HELP_SECTION_RE.sub(lambda m: f"{BOLD}{m.group(1)}:{RESET}", text)
    text = _HELP_OPTION_RE.sub(
        lambda m: f"{m.group(1)}{CYAN}{m.group(2)}{RESET}", text
    )
    return text


def colorize_parser(parser: "object") -> "object":
    """Install :func:`colorize_argparse_help` on an :class:`argparse.ArgumentParser`.

    Replaces ``parser.format_help`` so both ``parser.print_help()`` and the
    ``--help`` flag route through the colorizer. Returns the same parser for
    easy chaining.
    """
    original = parser.format_help  # type: ignore[attr-defined]

    def colored_format_help() -> str:
        return colorize_argparse_help(original())

    parser.format_help = colored_format_help  # type: ignore[attr-defined]
    return parser
