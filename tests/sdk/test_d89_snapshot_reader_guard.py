"""D89: 1.3 reads a 1.2 run's DAG snapshot (``dag_snapshot::``) only for a run with no run row; 1.4 deletes it.

A run of 1.2 lives at most a year, so by 1.4 no such run is left to draw. This guard fails from version 1.4.0
until the reader goes, so the deletion is not forgotten (ADR #137, *Migration*).
"""

from pathlib import Path

import polyris

READER = Path(__file__).resolve().parents[2] / "sam" / "lambdas" / "console_api" / "routes" / "pipelines_info.py"


def _minor(version: str) -> tuple:
    return tuple(int(part) for part in version.split(".")[:2])


def test_the_1_2_snapshot_reader_goes_in_1_4():
    if _minor(polyris.__version__) < (1, 4):
        return
    assert "DAG_SNAPSHOT_PREFIX" not in READER.read_text(), (
        "D89: 1.4 deletes the console's reader of 1.2 DAG snapshots (routes/pipelines_info.py) and this guard")


def test_the_guard_reads_the_file_that_holds_the_reader_today():
    """So a move of the reader cannot leave the guard checking nothing."""
    assert "DAG_SNAPSHOT_PREFIX" in READER.read_text()


FEED = Path(__file__).resolve().parents[2] / "sam" / "lambdas" / "console_api" / "feed.py"


def test_the_1_2_stale_display_goes_in_1_4():
    """D123: a run from before 1.3 left unfinished shows ``stale`` until its rows expire; 1.4 deletes that display
    (``feed.is_stale``, ``ExecutionStatus.STALE``) with the snapshot reader."""
    if _minor(polyris.__version__) < (1, 4):
        return
    assert "def is_stale" not in FEED.read_text(), (
        "D123: 1.4 deletes the stale display of 1.2 runs (feed.is_stale, ExecutionStatus.STALE) and this guard")


def test_the_stale_guard_reads_the_file_that_holds_the_display_today():
    assert "def is_stale" in FEED.read_text()
