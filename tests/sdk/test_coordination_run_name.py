"""Run and wrapper names stay unique (ADR #130 §6.6, defect B-X1)."""

import pytest

from polyris.coordination.run_name import (
    KIND_AND,
    KIND_BACKFILL,
    KIND_EVENT,
    MAX_NAME,
    fit_task_id,
    run_name,
)


def wrapper_name(task_id, date, run):
    """How a run's task executions are named (generators._make_execution_name_expr)."""
    return f"{task_id}-{date}-{run[-8:]}"


class TestRunName:
    def test_deterministic(self):
        assert run_name("sales", "2026-11-12", KIND_AND) == run_name("sales", "2026-11-12", KIND_AND)

    def test_two_consumers_never_share_task_wrapper_names(self):
        # Before: both runs ended in "-asset-and", so their `load` tasks collided.
        first = run_name("finance", "2026-11-12", KIND_AND)
        second = run_name("marketing", "2026-11-12", KIND_AND)
        assert wrapper_name("load", "2026-11-12", first) != wrapper_name("load", "2026-11-12", second)

    @pytest.mark.parametrize("args", [
        ("p", "2026-11-12", KIND_EVENT, "ns/a@2026-11-12T06:00:00.000Z"),
        ("p", "2026-11-12", KIND_EVENT, "ns/a@2026-11-12T06:00:00.001Z"),
        ("p", "2026-11-12", KIND_BACKFILL, "bf-1/tier-0"),
        ("p", "2026-11-12", KIND_BACKFILL, "bf-1/tier-1"),
        ("p", "2026-11-13", KIND_AND, ""),
    ])
    def test_suffix_differs_for_every_distinct_run(self, args):
        assert run_name(*args)[-20:] != run_name("p", "2026-11-12", KIND_AND)[-20:]

    def test_long_pipeline_is_truncated_but_tail_is_kept(self):
        name = run_name("p" * 200, "2026-11-12", KIND_AND)
        assert len(name) == MAX_NAME
        assert name.endswith("-2026-11-12-and-" + name[-8:])

    def test_characters_step_functions_rejects_are_replaced(self):
        assert run_name("my.pipe/line", "2026-11-12", KIND_AND).startswith("my-pipe-line-2026-11-12-and-")

    @pytest.mark.parametrize("args", [
        ("p", "2026-11-12", "or"),
        ("", "2026-11-12", KIND_AND),
        ("p", " ", KIND_AND),
        ("///", "2026-11-12", KIND_AND),
    ])
    def test_invalid_arguments_raise(self, args):
        with pytest.raises(ValueError):
            run_name(*args)


class TestFitTaskId:
    def test_short_ids_are_unchanged(self):
        assert fit_task_id("load", 60) == "load"

    def test_long_ids_keep_a_prefix_and_a_hash(self):
        long_id = "group." + "x" * 80
        fitted = fit_task_id(long_id, 60)
        assert len(fitted) == 60
        assert fitted.startswith("group.")

    def test_long_ids_differing_only_at_the_end_stay_distinct(self):
        base = "reporting_group." + "very_long_task_name_" * 4
        assert fit_task_id(base + "a", 40) != fit_task_id(base + "b", 40)

    def test_budget_too_small(self):
        with pytest.raises(ValueError):
            fit_task_id("anything", 7)
