"""sync_variables — generate the run_task ``$dateVars`` block from the registry.

Canonical source: ``polyris/variables.py`` (``VARIABLES``). This reads it via a
normal import (NOT regex) and regenerates the ``Output`` expression of the
``Prepare_Task_Input`` state in::

    sam/sfn_templates/helpers/run_task/sfn.tpl.json

The replacement is **surgical** — only that one JSON string value is rewritten; the
rest of the template's formatting is untouched (unlike the enum codegen, this target
is a hand-maintained file with a single generated field).

Usage::

  python -m polyris.codegen.sync_variables          # write
  python -m polyris.codegen.sync_variables --check   # exit 1 if it would change
"""
import argparse
import re
import sys
from pathlib import Path

from polyris.variables import VARIABLES

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "sam" / "sfn_templates" / "helpers" / "run_task" / "sfn.tpl.json"

# The template is not pure JSON (it contains ``${...}`` deploy-time placeholders), so
# we locate the generated Output by text, not by json parsing. The ``$dateVars``
# expression is the single JSON string value containing ``$dateVars``; the JSONata
# inside uses only single quotes, so a double-quoted ``"[^"]*"`` span bounds it safely.
_OUTPUT_RE = re.compile(r'"(\{%[^"]*\$dateVars[^"]*%\})"')

# Fixed JSONata scaffolding around the generated object. ``$cd`` / ``$dt`` are the
# logical-date locals the registry expressions reference; the suffix merges the
# computed vars with any user-supplied ``variables`` and preserves ``upstream``.
_PREFIX = ("{% ( $cd := $states.input.current_date; "
           "$dt := $toMillis($cd & 'T00:00:00Z'); $dateVars := { ")
_SUFFIX = (" }; $vars := $exists($states.input.variables) ? $states.input.variables : {}; "
           "$merged := $merge([$dateVars, $vars]); "
           "$states.input ~> |$| {'variables': $merged, "
           "'upstream': $exists($states.input.upstream) ? $states.input.upstream : {}} | ) %}")


def render_output() -> str:
    """Build the full ``Output`` JSONata expression from the registry."""
    fields = ", ".join(f"'{name}': {spec['expr']}" for name, spec in VARIABLES.items())
    return _PREFIX + fields + _SUFFIX


def _find_current(text: str) -> "re.Match[str]":
    matches = _OUTPUT_RE.findall(text)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one $dateVars Output in {TEMPLATE_PATH.name}, "
            f"found {len(matches)} — aborting."
        )
    match = _OUTPUT_RE.search(text)
    assert match is not None  # findall above guarantees exactly one match
    return match


def _current_output() -> str:
    return _find_current(TEMPLATE_PATH.read_text()).group(1)


def is_in_sync() -> bool:
    return _current_output() == render_output()


def write() -> bool:
    """Rewrite the generated Output in place. Returns True if the file changed."""
    text = TEMPLATE_PATH.read_text()
    match = _find_current(text)
    new = render_output()
    if match.group(1) == new:
        return False
    TEMPLATE_PATH.write_text(text[:match.start()] + '"' + new + '"' + text[match.end():])
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 if the template would change; don't write.")
    args = parser.parse_args()

    rel = TEMPLATE_PATH.relative_to(REPO_ROOT)
    if args.check:
        if is_in_sync():
            print("✅ run_task $dateVars in sync with polyris/variables.py")
            return 0
        sys.stderr.write(
            f"❌ {rel} is out of sync with polyris/variables.py\n"
            "Run: python -m polyris.codegen.sync_variables\n"
        )
        return 1

    if write():
        print(f"✅ Wrote {rel}")
    else:
        print("✅ Already in sync, no changes written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
