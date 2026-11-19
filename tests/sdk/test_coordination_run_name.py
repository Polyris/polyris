"""Run names, the keys of their task rows and run_task's names stay unique (ADR #130 §6.6, defect B-X1;
ADR #137 invariant 4)."""

import pytest

from polyris.coordination.run_name import (
    KIND_AND,
    KIND_BACKFILL,
    KIND_EVENT,
    MAX_NAME,
    RUN_TASK_HASH,
    run_name,
    run_task_name,
)
from polyris.coordination import task_row


class TestRunName:
    def test_deterministic(self):
        assert run_name("sales", "2026-11-12", KIND_AND) == run_name("sales", "2026-11-12", KIND_AND)

    def test_two_consumers_never_share_task_rows(self):
        # Before: both runs ended in "-asset-and", so their `load` tasks collided.
        first = run_name("finance", "2026-11-12", KIND_AND)
        second = run_name("marketing", "2026-11-12", KIND_AND)
        assert task_row.key("load", "2026-11-12", first) != task_row.key("load", "2026-11-12", second)

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


class TestRunTaskName:
    """A run_task execution's name (ADR #137 invariant 4)."""

    KEY = "load-2026-11-12-run-2026-11-12-abcdef12"
    START = "2026-11-12T06:00:00.123Z"

    def test_it_is_a_prefix_of_the_row_key_and_a_hash(self):
        name = run_task_name(self.KEY, self.START, 1)
        assert name.startswith(self.KEY + "-") and len(name) == len(self.KEY) + 1 + RUN_TASK_HASH
        assert name == run_task_name(self.KEY, self.START, 1)              # a start repeated finds the first

    @pytest.mark.parametrize("change", [{"run_start": "2027-11-12T06:00:00.123Z"}, {"generation": 2},
                                        {"key": KEY + "x"}])
    def test_another_run_generation_or_row_gets_another_name(self, change):
        args = {"key": self.KEY, "run_start": self.START, "generation": 1}
        assert run_task_name(**{**args, **change}) != run_task_name(**args)

    def test_a_long_key_keeps_its_hash_within_80_characters(self):
        long_key = "group." + "very_long_task_name_" * 8 + "-2026-11-12-run"
        first, second = run_task_name(long_key + "a", self.START, 1), run_task_name(long_key + "b", self.START, 1)
        assert len(first) == MAX_NAME and first != second
        assert set(first) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

    def test_a_key_of_characters_step_functions_rejects_is_named_by_its_hash(self):
        assert len(run_task_name("...", self.START, 1)) == RUN_TASK_HASH
