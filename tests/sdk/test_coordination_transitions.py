"""The transition table of a task row and of a run row (ADR #137 §3, §6, §7; invariant 7)."""

import pytest

from polyris.constants import SkipOrigin, TaskStatus
from polyris.coordination import attention, ownership, run_row
from polyris.coordination import transitions as t

S = TaskStatus
STATUSES = frozenset(s.value for s in S)
#: The statuses 1.3 writes; ``succeeded``, ``aborted``, ``waiting_delay`` and ``pending`` occur only on older rows.
WRITTEN = frozenset({"waiting", "waiting_paused", "deps_ready", "running", "waiting_decision",
                     "success", "skipped", "failed", "upstream_failed", "stopped"})


def task(status, generation=1):
    return {"status": status, ownership.FIELD: generation}


# ── the task row's table ─────────────────────────────────────────────

def test_the_table_names_each_transition_once_with_known_writers_and_statuses():
    names = [x.name for x in t.TASK_TRANSITIONS]
    assert len(names) == len(set(names))
    for x in t.TASK_TRANSITIONS:
        assert x.writers and x.writers <= t.WRITERS, x.name
        assert x.sources and x.sources <= STATUSES | {t.NO_ROW}, x.name
        assert x.target in WRITTEN, x.name


def test_1_3_writes_every_status_of_its_own_and_none_of_the_older_ones():
    """Nothing writes `aborted` any more: every Stop writes `stopped` (§3)."""
    assert {x.target for x in t.TASK_TRANSITIONS} == WRITTEN


def test_every_skip_names_its_origin_and_only_a_skip_does():
    """ADR #138 §3: the writers' census fails a write of `skipped` without an origin."""
    origins = {x.name: x.skip_origin for x in t.TASK_TRANSITIONS if x.target == S.SKIPPED.value}
    assert origins == {"create_skipped": SkipOrigin.RUN, "skip_by_rule": SkipOrigin.RULE, "skip": SkipOrigin.MANUAL}
    assert all(x.skip_origin is None for x in t.TASK_TRANSITIONS if x.target != S.SKIPPED.value)


def test_the_wakes_column_is_what_section_7_says_wakes_the_dispatcher():
    """A task row that became final or waits for a decision, a final failure that became an outcome
    that lets dependants run, or a restart (§7). Two encodings of one rule must agree."""
    for x in t.TASK_TRANSITIONS:
        for source in x.sources:
            became = x.target in t.FINAL | {"waiting_decision"} and source not in t.FINAL
            resolved = source in t.FINAL_FAILURES and x.target in ("success", "skipped")
            restarted = x.target == "waiting" and source in t.FINAL | {"waiting_decision"}
            assert x.wakes == (source is not None and (became or resolved or restarted)), (x.name, source)


def test_no_pair_of_statuses_both_wakes_and_does_not():
    quiet = {(s, x.target) for x in t.TASK_TRANSITIONS if not x.wakes for s in x.sources}
    assert quiet & t.WAKING == set()


def test_only_the_dispatcher_moves_a_row_out_of_waiting_apart_from_an_operator():
    """Invariant 2."""
    for x in t.TASK_TRANSITIONS:
        if x.sources & {"waiting", "waiting_paused"}:
            assert x.writers <= {t.Writer.DISPATCHER, t.Writer.CONSOLE}, x.name


def test_run_task_writes_only_running_success_and_a_request_for_a_decision():
    assert {x.target for x in t.TASK_TRANSITIONS if t.Writer.RUN_TASK in x.writers} == {
        "running", "success", "waiting_decision"}


@pytest.mark.parametrize("source,target,writer,expected", [
    (None, "waiting", t.Writer.START_RUN, True),
    ("waiting", "deps_ready", t.Writer.DISPATCHER, True),
    ("waiting", "deps_ready", t.Writer.CONSOLE, False),           # only the dispatcher decides
    ("failed", "skipped", t.Writer.CONSOLE, True),                 # an operator skips a failure
    ("failed", "skipped", t.Writer.DISPATCHER, False),             # a rule skips only a waiting row
    ("success", "skipped", t.Writer.CONSOLE, False),
    ("succeeded", "success", t.Writer.CONSOLE, False),             # a success under its older name
    ("failed", "stopped", t.Writer.CONSOLE, False),                # a final status stays final
    ("stopped", "waiting", t.Writer.CONSOLE, True),                # Restart after a Stop
    ("upstream_failed", "waiting", t.Writer.DISPATCHER, True),     # a reset of Restart with dependants
    ("waiting_decision", "failed", t.Writer.DISPATCHER, True),     # the decision timeout
    ("waiting_decision", "failed", t.Writer.CONSOLE, True),        # Fail
    ("running", "failed", t.Writer.RUN_TASK, False),               # a failed attempt asks for a decision
    ("deps_ready", "running", t.Writer.RUN_TASK, True),
])
def test_allowed(source, target, writer, expected):
    assert t.allowed(source, target, writer) is expected


def test_a_transition_is_found_by_its_name():
    assert t.task_transition("ready").sources == {"waiting", "waiting_paused"}
    assert t.run_transition("activate").wakes
    with pytest.raises(ValueError, match="unknown task-row transition 'claim'"):
        t.task_transition("claim")
    with pytest.raises(ValueError, match="unknown run-row transition 'claim'"):
        t.run_transition("claim")


# ── the attention index ──────────────────────────────────────────────

@pytest.mark.parametrize("status,kind", [
    ("waiting", attention.WAITING), ("deps_ready", attention.WAITING), ("running", attention.WAITING),
    ("waiting_decision", attention.DECISION), ("failed", attention.FAILED),
    ("waiting_paused", None), ("success", None), ("skipped", None), ("upstream_failed", None), ("stopped", None),
])
def test_a_task_rows_kind_in_the_attention_index_follows_its_status(status, kind):
    assert t.attention_kind(status) == kind


def test_the_new_kinds_are_attention_kinds():
    assert attention.attention(attention.DECISION, "k").startswith("decision#")
    assert attention.attention(attention.FAILED, "k").startswith("failed#")


# ── what wakes the dispatcher: task rows ─────────────────────────────

def test_each_waking_transition_wakes_and_each_other_does_not():
    for x in t.TASK_TRANSITIONS:
        for source in x.sources - {None}:
            assert t.task_row_wakes(task(source), task(x.target, 2)) is x.wakes, (x.name, source)


@pytest.mark.parametrize("old,new", [
    (None, task("waiting")),                                     # Start_Run's insert
    (task("success"), None),                                     # a deletion, by TTL or by hand
    (task("waiting_decision"), task("waiting_decision")),        # rewritten in a pause
    (task("waiting"), {**task("waiting"), "ttl": 1}),            # a TTL or a due time
    ({"status": "running"}, {"status": "success"}),              # output# or a warning: no generation
])
def test_writes_outside_the_table_wake_nothing(old, new):
    assert not t.task_row_wakes(old, new)


# ── the run row's table and what wakes the dispatcher ───────────────

def run(status="active", **fields):
    return {run_row.STATUS: status, **fields}


#: A change that each run-row transition makes, as the stream shows it.
RUN_CHANGES = {
    "create": (None, run("creating", run_token="t1")),
    "mark_step_failure": (run(), run(step_failure={"step": "notify"})),
    "activate": (run("creating"), run("active")),
    "new_token": (run(run_token="t1", answered_token="t1"), run(run_token="t2")),
    "stop": (run(), run(stop={"by": "ops", "reason": "bad input"})),
    "engine_stop": (run("creating"), run("creating", stop={"by": "engine"})),
    "request": (run(), run(requests={"renew_ttl": {"at": "2026-11-18T10:00:00Z"}})),
    "task_action": (run(), run(attention="waiting#a", attention_at="2026-11-18T10:00:00Z", run_version=2)),
    "reset": (run(run_version=2), run(run_version=3)),
    "pause": (run(pauses=[]), run(pauses=[{"from": "2026-11-18T10:00:00Z"}])),
    "resume": (run(pauses=[{"from": "2026-11-18T10:00:00Z"}]),
               run(pauses=[{"from": "2026-11-18T10:00:00Z", "to": "2026-11-18T11:00:00Z"}])),
    "clear_request": (run(requests={"renew_ttl": {"at": "t"}}), run(requests={})),
    "answer": (run(run_token="t1"), run(run_token="t1", answered_token="t1", finished_at="2026-11-18T12:00:00Z")),
    "stop_done": (run(stop={"by": "ops"}), run(stop={"by": "ops"}, stop_done="2026-11-18T12:00:00Z")),
}


def test_every_run_row_transition_has_its_change():
    assert set(RUN_CHANGES) == {x.name for x in t.RUN_TRANSITIONS}


@pytest.mark.parametrize("name", sorted(RUN_CHANGES))
def test_a_run_row_change_wakes_as_its_transition_says(name):
    old, new = RUN_CHANGES[name]
    assert t.run_row_wakes(old, new) is t.run_transition(name).wakes


def test_every_run_row_write_of_the_table_raises_run_version_but_a_steps_mark():
    """§3: the mark of a failed direct step keeps it; nothing outside the table raises it."""
    assert {x.name for x in t.RUN_TRANSITIONS if not x.raises_version} == {"mark_step_failure"}


def test_no_run_row_write_leads_back_to_creating():
    assert [x.name for x in t.RUN_TRANSITIONS if x.target == "creating"] == ["create"]
    assert all(x.sources == {None} for x in t.RUN_TRANSITIONS if x.target == "creating")


@pytest.mark.parametrize("old,new,woken", [
    (run(), run(left_at="2026-11-18T10:00:00Z"), True),                                  # a pass left rows
    (run(left_at="a"), run(left_at="b"), True),                                          # and again
    (run(left_at="2026-11-18T10:00:00Z"), run(), False),                                 # the sweeper clears it
    (run(summary={"running": 1}), run(summary={"success": 1}), False),                   # a summary
    (run(requests={"renew_ttl": {"at": "a"}}), run(requests={"renew_ttl": {"at": "b"}}), True),  # a request anew
    (run(), None, False),                                                                # a deletion
])
def test_left_at_set_wakes_and_the_writes_outside_the_table_do_not(old, new, woken):
    assert t.run_row_wakes(old, new) is woken


def test_wakes_tells_a_run_row_from_its_input_and_from_a_task_row():
    assert t.wakes(run_row.key("orders-run-2026-11-18-abcd1234"), run("creating"), run("active"))
    assert not t.wakes(run_row.input_key("orders-run-2026-11-18-abcd1234"), {}, {"variables": {}})
    assert t.wakes("extract-2026-11-18-abcd1234", task("running"), task("success"))
    assert not t.wakes("_notify_warn_coordination_x", {"status": "warn"}, {"status": "warned"})


def test_the_run_rows_keys():
    assert run_row.key("p-run-d-h") == "_run_p-run-d-h"
    assert run_row.input_key("p-run-d-h") == "_run_p-run-d-h#input"
    assert run_row.is_run_row("_run_p-run-d-h") and not run_row.is_run_row("_run_p-run-d-h#input")
    assert not run_row.is_run_row("extract-d-h")


# ── the stream's filters ─────────────────────────────────────────────

def test_four_filters_three_for_task_rows_that_carry_a_generation():
    patterns = t.filter_patterns()
    assert len(patterns) == 4                                   # of the five Lambda allows by default
    for pattern in patterns[:3]:
        assert pattern["dynamodb"]["NewImage"][ownership.FIELD] == {"N": [{"exists": True}]}
    assert patterns[3]["dynamodb"]["Keys"] == {run_row.TABLE_KEY: {"S": [{"prefix": "_run_"}]}}
