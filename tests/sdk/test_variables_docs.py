"""The task variables users read about are the ones run_task gives (polyris/variables.py).

The registry is the one source: the codegen writes it into run_task, and the
console reads its descriptions. DSL.md and the console's help list the
variables by hand, and both had drifted from it (the review of TR-5): DSL.md
lists every one, and the help shows a subset of real names.
"""

import re
from pathlib import Path

from polyris.variables import VARIABLES

REPO_ROOT = Path(__file__).resolve().parents[2]


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def test_dsl_md_lists_every_variable_run_task_gives():
    dsl = _section((REPO_ROOT / "docs/features/DSL.md").read_text(), "### Date variables", "## Complete Example")
    missing = [name for name in VARIABLES if f"`{name}`" not in dsl]
    assert missing == [], f"DSL.md's Date variables do not list {missing}"


def test_the_consoles_help_shows_only_variables_run_task_gives():
    modal = (REPO_ROOT / "ui/src/components/HelpModal.tsx").read_text()
    block = _section(modal, "<h4>Task variables", "</div>\n            </div>")
    shown = {name for code in re.findall(r'<code className="hm-api-method get">([^<]+)</code>', block)
             for name in re.split(r"\s*/\s*", code)}
    assert shown, "the help's list of task variables is gone"
    assert shown <= set(VARIABLES), f"the help shows variables run_task does not give: {sorted(shown - set(VARIABLES))}"
