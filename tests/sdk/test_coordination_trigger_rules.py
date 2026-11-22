"""The nine trigger rules (ADR #138): the truth table of §2, each clause of evaluate and of enabled_at."""

import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

from polyris.constants import TriggerRule, TriggerRuleLiteral
from polyris.coordination import trigger_rules as tr

TABLE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "trigger_rules_truth_table.json").read_text())
VERDICT = {"run": tr.READY, "skip": tr.SKIP, "upstream_failed": tr.UPSTREAM_FAILED, "wait": tr.WAIT,
           "wait>skip": tr.WAIT, "wait>upstream_failed": tr.WAIT, "fail": tr.FAIL}


def combinations(column):
    """Every list of (status, skip_origin) that the letters of ``column`` stand for."""
    letters = [] if column == "none" else column.split(",")
    return itertools.product(*(TABLE["letters"][letter] for letter in letters))


def cells(section):
    for rule, symbols in TABLE[section]["rules"].items():
        for column, symbol in zip(TABLE[section]["columns"], symbols):
            yield pytest.param(rule, column, symbol, id=f"{rule}-{column}")


def check(rule, column, symbol):
    for upstreams in combinations(column):
        statuses = [status for status, _ in upstreams]
        origins = [origin for _, origin in upstreams]
        assert tr.evaluate(rule, statuses, origins).verdict == VERDICT[symbol], upstreams


# ── the truth table (§2, §6) ─────────────────────────────────────────

def test_the_table_covers_the_nine_rules_and_every_status():
    assert set(TABLE["two_upstreams"]["rules"]) == set(TABLE["three_upstreams"]["rules"]) == tr.KNOWN_RULES
    stored = {status for values in TABLE["letters"].values() for status, _ in values}
    assert stored == {s for s in tr.FINAL | {"waiting", "waiting_paused", "deps_ready", "running",
                                            "waiting_decision", "waiting_delay", "pending"}}


@pytest.mark.parametrize("rule,column,symbol", cells("two_upstreams"))
def test_two_upstreams(rule, column, symbol):
    check(rule, column, symbol)


@pytest.mark.parametrize("rule,column,symbol", cells("three_upstreams"))
def test_three_upstreams_and_none(rule, column, symbol):
    check(rule, column, symbol)


@pytest.mark.parametrize("column,symbol", zip(TABLE["rule_skip"]["columns"], TABLE["rule_skip"]["verdicts"]))
def test_a_skip_by_a_rule_cascades_under_all_success(column, symbol):
    check(TABLE["rule_skip"]["rule"], column, symbol)


@pytest.mark.parametrize("rule", sorted(tr.KNOWN_RULES - {"all_success"}))
def test_every_other_rule_reads_a_skip_by_a_rule_like_any_skip(rule):
    for column in TABLE["two_upstreams"]["columns"] + ["S,K,F", "S,K,K"]:
        if "K" in column:
            symbol = TABLE["two_upstreams"]["rules"][rule][TABLE["two_upstreams"]["columns"].index(column)] \
                if column in TABLE["two_upstreams"]["columns"] else \
                TABLE["three_upstreams"]["rules"][rule][TABLE["three_upstreams"]["columns"].index(column)]
            check(rule, column.replace("K", "R"), symbol)


@pytest.mark.parametrize("column", TABLE["two_upstreams"]["columns"] + TABLE["three_upstreams"]["columns"])
def test_an_unknown_rule_fails_its_task_in_every_column(column):
    check("none_failed_or_skipped", column, TABLE["unknown_rule"]["verdict"])


ARROWS = [pytest.param(rule, column, symbol.split(">")[1], id=f"{rule}-{column}")
          for section in ("two_upstreams", "three_upstreams")
          for rule, symbols in TABLE[section]["rules"].items()
          for column, symbol in zip(TABLE[section]["columns"], symbols) if ">" in symbol]


@pytest.mark.parametrize("rule,column,outcome", ARROWS)
def test_an_arrow_cell_gives_its_outcome_whatever_final_status_its_pending_upstream_takes(rule, column, outcome):
    """⏳→⏭ and ⏳→⛔ wait although the task can no longer run (§2): once P is final, the outcome is fixed."""
    finals = TABLE["letters"]["S"] + TABLE["letters"]["K"] + TABLE["letters"]["F"]
    for upstreams in combinations(column.replace("P", "S")):
        for status, origin in finals:
            resolved = [(status, origin) if column.split(",")[i] == "P" else u for i, u in enumerate(upstreams)]
            assert tr.evaluate(rule, [s for s, _ in resolved], [o for _, o in resolved]).verdict == VERDICT[outcome]


# ── names (§1, §6) ───────────────────────────────────────────────────

def test_the_engine_the_literal_and_the_enum_name_the_same_nine_rules():
    names = {v for k, v in vars(TriggerRule).items() if not k.startswith("_")}
    assert names == tr.KNOWN_RULES == set(get_args(TriggerRuleLiteral))
    assert len(names) == 9


def test_a_rejected_name_fails_with_what_to_use_instead():
    result = tr.evaluate("always", ["success"])
    assert result.verdict == tr.FAIL
    assert "remove the dependency" in result.reason
    assert set(tr.REJECTED) == {"one_done", "all_done_min_one_success", "always", "all_done_setup_success"}
    assert not set(tr.REJECTED) & tr.KNOWN_RULES


def test_an_unknown_name_lists_the_known_rules():
    assert "known rules: ['all_done'," in tr.evaluate("bogus", []).reason


# ── what evaluate reports ────────────────────────────────────────────

def test_counts_read_every_category_and_the_origin_of_each_skip():
    c = tr.count(["success", "succeeded", "failed", "stopped", "skipped", "skipped", "waiting_decision"],
                 [None, None, None, None, "rule", "manual", None])
    assert (c.total, c.success, c.failed, c.skipped, c.cascading, c.pending) == (7, 2, 2, 2, 1, 1)


@pytest.mark.parametrize("rule,statuses,origins,reason", [
    ("all_success", [], None, "no upstreams"),
    ("all_success", ["success"], None, "all_success is met"),
    ("all_success", ["success", "running"], None, "waiting for its upstreams"),     # no count: no rewrite
    ("all_success", ["failed", "stopped"], None, "2 upstreams failed"),
    ("all_success", ["skipped"], ["rule"], "1 upstreams were skipped by their own rule"),
    ("one_success", ["skipped"], None, "no upstream succeeded"),
    ("all_skipped", ["success", "skipped"], None, "not every upstream was skipped (1 of 2)"),
    ("none_skipped", ["skipped"], None, "1 upstreams were skipped"),
    ("one_failed", ["success"], None, "no upstream failed"),
    ("all_failed", ["success", "failed"], None, "not every upstream failed (1 of 2)"),
    ("none_failed_min_one_success", ["skipped"], None, "no upstream succeeded"),
])
def test_each_outcome_says_why(rule, statuses, origins, reason):
    assert tr.evaluate(rule, statuses, origins).reason == reason


def test_skip_origins_must_align_with_statuses():
    with pytest.raises(ValueError, match="skip_origins has 1 entries for 2 upstreams"):
        tr.evaluate("all_success", ["success", "skipped"], ["rule"])


# ── the enabling time (ADR #130 §9, ADR #138 §2) ─────────────────────

T1, T2, T3 = (datetime(2026, 11, 18, h, tzinfo=timezone.utc) for h in (1, 2, 3))


def test_one_success_dates_from_its_earliest_success():
    assert tr.enabled_at("one_success", ["success", "failed", "success"], [T2, T1, T3]) == T2


def test_one_failed_dates_from_its_earliest_final_failure():
    """At the decision or its timeout, which the caller gives as the failure's time."""
    assert tr.enabled_at("one_failed", ["success", "stopped", "failed", "running"], [T1, T3, T2, None]) == T2


@pytest.mark.parametrize("rule", ["all_success", "all_done", "none_skipped", "none_failed"])
def test_other_rules_date_from_the_latest_upstream(rule):
    assert tr.enabled_at(rule, ["success", "success"], [T3, T1]) == T3


def test_an_outcome_that_can_never_be_met_dates_from_the_latest_upstream():
    assert tr.enabled_at("one_success", ["failed", "skipped"], [T1, T2]) == T2


@pytest.mark.parametrize("rule,statuses,times", [
    ("all_success", ["success", "running"], [T1, None]),     # it still waits
    ("bogus", ["success"], [T1]),                             # an unknown rule fails at once
])
def test_no_time_while_waiting_or_for_an_unknown_rule(rule, statuses, times):
    assert tr.enabled_at(rule, statuses, times) is None


def test_unknown_times_are_left_out():
    assert tr.enabled_at("all_success", ["success", "success"], [None, T1]) == T1
    assert tr.enabled_at("all_success", ["success"], [None]) is None
    assert tr.enabled_at("all_success", [], []) is None


def test_times_must_align_with_statuses():
    with pytest.raises(ValueError, match="finished_at has 1 entries for 2 upstreams"):
        tr.enabled_at("all_success", ["success", "success"], [T1])


@pytest.mark.parametrize("status, origin, category", [
    ("success", None, "success"), ("failed", None, "failed"), ("upstream_failed", None, "failed"),
    ("stopped", None, "failed"), ("skipped", "rule", "skipped_by_rule"), ("skipped", "manual", "skipped"),
    ("skipped", None, "skipped"), ("waiting", None, "pending"), ("running", None, "pending"),
    ("waiting_decision", None, "pending")])
def test_each_upstream_counts_as_what_every_rule_counts_it(status, origin, category):
    """The console shows it beside the rule's verdict; it agrees with ``count`` (ADR #137 §1)."""
    assert tr.counts_as(status, origin) == category
    counts = tr.count([status], [origin])
    assert {"success": counts.success, "failed": counts.failed, "pending": counts.pending,
            "skipped_by_rule": counts.cascading, "skipped": counts.skipped - counts.cascading}[category] == 1
