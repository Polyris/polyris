"""D129: 1.4 upgrades only from 1.3, and deletes what 1.3 keeps for the move from 1.2.

That is `polyris-up`'s move from 1.2 (`polyris/upgrade.py` with its checks, the stop of what 1.2 left running and
`--accept-stopped-runs`; the `_check_before_upgrade`, `_stop_leftovers` and `_finish_cleanup` steps of `up.py`; the
rules of `platform.py` for it), and the engine's handling of rows that a 1.2 run left (`ownership.LEGACY`,
`row_write.LEGACY_ROW_CONDITION`, `transitions.OLDER_NOT_FINAL`, the stop view of a row without readiness fields).
Rows of 1.2 expire within a year and 90 days, and whoever is still on 1.2 goes through 1.3 first. These guards fail
from version 1.4.0 until the code goes, so the deletion is not forgotten (the architect's review of TR-8, A-16).
"""

from pathlib import Path

import polyris

REPO = Path(__file__).resolve().parents[2]
UPGRADE = REPO / "polyris" / "upgrade.py"
ROW_WRITE = REPO / "polyris" / "coordination" / "row_write.py"
TRANSITIONS = REPO / "polyris" / "coordination" / "transitions.py"


def _minor(version: str) -> tuple:
    return tuple(int(part) for part in version.split(".")[:2])


def test_the_move_from_1_2_goes_in_1_4():
    if _minor(polyris.__version__) < (1, 4):
        return
    assert not UPGRADE.exists(), (
        "D129: 1.4 deletes polyris-up's move from 1.2 (polyris/upgrade.py, its steps in up.py and rules in "
        "platform.py, --accept-stopped-runs) and this guard")


def test_the_engine_s_handling_of_1_2_rows_goes_in_1_4():
    if _minor(polyris.__version__) < (1, 4):
        return
    assert "LEGACY_ROW_CONDITION" not in ROW_WRITE.read_text(), (
        "D129: 1.4 deletes the stop of rows a 1.2 run left (row_write.LEGACY_ROW_CONDITION, ownership.LEGACY, "
        "dispatch.stop_view's branch without readiness) and this guard")
    assert "OLDER_NOT_FINAL" not in TRANSITIONS.read_text(), (
        "D129: 1.4 deletes transitions.OLDER_NOT_FINAL, the status only 1.2's rows carry, and this guard")


def test_the_guards_read_the_files_that_hold_the_code_today():
    """So a move of the code cannot leave the guards checking nothing."""
    assert UPGRADE.exists()
    assert "LEGACY_ROW_CONDITION" in ROW_WRITE.read_text()
    assert "OLDER_NOT_FINAL" in TRANSITIONS.read_text()
