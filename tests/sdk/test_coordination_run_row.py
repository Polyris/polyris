"""The run row's contract beyond its keys (ADR #137 §3, §9)."""

import re

import pytest

from polyris.constants import BackfillCascade, WarningKind
from polyris.coordination import run_row, statuses
from polyris.xcom import ENV_UPSTREAM_STATUSES


@pytest.mark.parametrize("suppress,cascade_all,expected", [
    (False, False, BackfillCascade.AUTO),
    (False, True, BackfillCascade.ALL),
    (True, False, BackfillCascade.NONE),
    (True, True, BackfillCascade.NONE),          # no fact reaches a consumer: none wins
])
def test_the_runs_cascade_from_its_starters_flags(suppress, cascade_all, expected):
    assert run_row.cascade(suppress_asset_event=suppress, cascade_all=cascade_all) == expected


def test_a_failed_runs_answer_has_an_error_name_of_its_own():
    """States.* names are Step Functions' own, and a Retry on States.ALL would match it."""
    assert run_row.FAILED_RUN_ERROR.startswith("Polyris.")
    assert not run_row.FAILED_RUN_ERROR.startswith("States.")


def test_the_input_item_keeps_the_runs_three_fields():
    assert {run_row.VARIABLES, run_row.SKIP_TASKS, run_row.CASCADE} == {"variables", "skip_tasks", "cascade"}


def test_a_failed_direct_step_has_a_warning_kind_of_its_own():
    assert WarningKind.STEP == "step" and "step" in statuses.WARNING_KINDS


def test_the_upstream_statuses_reach_a_task_under_an_environment_name():
    assert re.fullmatch(r"POLYRIS_[A-Z_]+", ENV_UPSTREAM_STATUSES)
