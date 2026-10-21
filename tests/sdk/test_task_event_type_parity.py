"""Parity test for the SFN template ↔ console_api `EventType` coupling (ADR #125,
Principle #28).

Any string literal used as `"event_type"` in a `task-events` putItem across the
SFN templates must have a matching member on `EventType`. The reverse is less
strict — `EventType` may list members not yet emitted (e.g. `RETRY_SCHEDULED`
is reserved for a future Wait-phase event) — but every TEMPLATE emission must
be a declared type, so a typo in a `.tpl.json` string literal fails the test
immediately.

`TASK_RETRY_STARTED` (ADR #125) is the member this test pins first: it must
exist on `EventType` AND be emitted somewhere in `run_task/sfn.tpl.json`.
"""
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SFN_TEMPLATE_DIR = REPO_ROOT / "sam" / "sfn_templates"
CONSOLE_API_CONSTANTS = REPO_ROOT / "sam" / "lambdas" / "console_api" / "constants.py"


@pytest.fixture(scope="module")
def event_type_members() -> set[str]:
    """Load the hand-maintained `EventType` class from console_api/constants.py
    without importing the full console_api package (which would require AWS SDK
    stubs). Parses the class body text to extract `NAME = 'VALUE'` pairs.
    """
    text = CONSOLE_API_CONSTANTS.read_text()
    class_match = re.search(r"class EventType:(.*?)(?=\nclass |\Z)", text, re.DOTALL)
    assert class_match is not None, "Could not find class EventType in console_api/constants.py"
    body = class_match.group(1)
    return set(re.findall(r"'([A-Z_]+)'", body))


@pytest.fixture(scope="module")
def sfn_event_type_literals() -> set[str]:
    """Scan every `.tpl.json` under sam/sfn_templates/ for `"event_type"` keys
    whose immediate value is a quoted literal (not a JSONata expression).
    """
    literals: set[str] = set()
    pattern = re.compile(
        r'"event_type"\s*:\s*\{\s*"S"\s*:\s*"([A-Z_]+)"\s*\}'
    )
    for path in SFN_TEMPLATE_DIR.rglob("*.tpl.json"):
        for match in pattern.finditer(path.read_text()):
            literals.add(match.group(1))
    return literals


def test_every_sfn_event_type_literal_is_declared_in_event_type(
    sfn_event_type_literals: set[str],
    event_type_members: set[str],
) -> None:
    missing = sfn_event_type_literals - event_type_members
    assert not missing, (
        f"SFN templates emit event_type literals missing from EventType: {sorted(missing)}. "
        f"Add them to console_api/constants.py:EventType."
    )


def test_task_retry_started_is_declared(event_type_members: set[str]) -> None:
    """ADR #125 — the new event type must be on `EventType`."""
    assert "TASK_RETRY_STARTED" in event_type_members, (
        "EventType.TASK_RETRY_STARTED missing (ADR #125). "
        "Add `TASK_RETRY_STARTED = 'TASK_RETRY_STARTED'` to console_api/constants.py:EventType."
    )


def test_task_retry_started_is_emitted_by_run_task(sfn_event_type_literals: set[str]) -> None:
    """ADR #125 — `run_task` must emit TASK_RETRY_STARTED on the in-SFN retry loop.

    Deliberately NOT a sweep of every template — only `run_task` owns the retry
    loop; other templates have no business emitting this event.
    """
    run_task_text = (SFN_TEMPLATE_DIR / "helpers" / "run_task" / "sfn.tpl.json").read_text()
    assert '"TASK_RETRY_STARTED"' in run_task_text, (
        "run_task/sfn.tpl.json does not emit TASK_RETRY_STARTED (ADR #125). "
        "Add an Emit_Task_Retry_Started state after Save_Retry_Attempt."
    )
