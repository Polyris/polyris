"""The harness's event filtering (stream_filters.py) agrees with what AWS delivered in TR-0.

TR-0 (C1, C2) put the four filters of ADR #137 §7 on a live mapping and
wrote 25 changes to the table: AWS delivered exactly 11 of them, and none of
four deletions. The same filters and the same records must give the same
answer here, or every harness scenario built on the filters tells a story
AWS does not.

The patterns that the transition table generates
(`transitions.filter_patterns`) must pass the same records, and every
change of a task row that wakes the dispatcher.

The same matcher applies the template's rule on the end of executions
(`ExecutionEndRule`, ADR #137 §7) to events shaped as Step Functions documents
them.
"""

import json
from typing import Any, Dict, Optional

import pytest

from polyris.coordination import transitions
from stream_filters import matches, matches_any

FINAL = ["success", "skipped", "failed", "upstream_failed", "aborted", "stopped"]
FAILURES = ["failed", "upstream_failed", "aborted", "stopped"]
GENERATION = {"generation": {"N": [{"exists": True}]}}

#: The four filters TR-0 put on the live mapping (ADR #137 §7).
TR0_FILTERS = [
    {"dynamodb": {"NewImage": {**GENERATION, "status": {"S": FINAL + ["waiting_decision"]}},
                  "OldImage": {"status": {"S": [{"anything-but": FINAL}]}}}},
    {"dynamodb": {"NewImage": {**GENERATION, "status": {"S": ["waiting"]}},
                  "OldImage": {"status": {"S": FINAL + ["waiting_decision"]}}}},
    {"dynamodb": {"NewImage": {**GENERATION, "status": {"S": ["success", "skipped"]}},
                  "OldImage": {"status": {"S": FAILURES}}}},
    {"eventName": ["INSERT", "MODIFY"], "dynamodb": {"Keys": {"execution_name": {"S": [{"prefix": "_run_"}]}}}},
]


def image(key: str, status: Optional[str], generation: Optional[int]) -> Dict[str, Any]:
    item: Dict[str, Any] = {"execution_name": {"S": key}}
    if status is not None:
        item["status"] = {"S": status}
    if generation is not None:
        item["generation"] = {"N": str(generation)}
    return item


def record(key: str, event: str, old: Optional[str], new: Optional[str], gen: Optional[int] = 1) -> Dict[str, Any]:
    """A stream record as the mapping sees it; a task row carries `generation`, other rows do not."""
    body: Dict[str, Any] = {"Keys": {"execution_name": {"S": key}}}
    if event != "INSERT":
        body["OldImage"] = image(key, old, gen)
    if event != "REMOVE":
        body["NewImage"] = image(key, new, gen)
    return {"eventName": event, "dynamodb": body}


#: The 25 writes of TR-0 C1, in order, and whether AWS delivered each (2026-11-18).
TR0_WRITES = [
    (record("c2-task-a", "INSERT", None, "waiting"), False),
    (record("c2-task-a", "MODIFY", "waiting", "deps_ready"), False),
    (record("c2-task-a", "MODIFY", "deps_ready", "running"), False),
    (record("c2-task-a", "MODIFY", "running", "success"), True),
    (record("c2-task-a", "MODIFY", "success", "waiting", gen=2), True),        # a restart
    (record("c2-task-a", "MODIFY", "waiting", "failed", gen=2), True),
    (record("c2-task-a", "MODIFY", "failed", "success", gen=2), True),         # Mark Successful
    (record("c2-task-b", "INSERT", None, "failed"), False),
    (record("c2-task-b", "MODIFY", "failed", "stopped"), False),               # final to final
    (record("c2-task-c", "INSERT", None, "waiting_decision"), False),
    (record("c2-task-c", "MODIFY", "waiting_decision", "waiting_decision"), True),   # rewritten in a pause
    (record("output#c2#t#2026-11-18", "INSERT", None, "success", gen=None), False),
    (record("output#c2#t#2026-11-18", "MODIFY", "success", "success", gen=None), False),
    (record("_notify_warn_coordination_c2", "INSERT", None, "warn", gen=None), False),
    (record("c2-task-d", "INSERT", None, "waiting"), False),
    (record("c2-task-d", "MODIFY", "waiting", "waiting"), False),              # a TTL write
    (record("_run_c2", "INSERT", None, "creating", gen=None), True),
    (record("_run_c2", "MODIFY", "creating", "active", gen=None), True),
    (record("_run_c2", "MODIFY", "active", "active", gen=None), True),         # a summary write
    (record("_run_c2#input", "INSERT", None, None, gen=None), True),
    (record("c2-task-f", "INSERT", None, "waiting"), False),
    (record("c2-task-f", "MODIFY", "waiting", "skipped"), True),
    (record("c2-task-g", "INSERT", None, "deps_ready"), False),
    (record("c2-task-g", "MODIFY", "deps_ready", "failed"), True),
    (record("c2-task-ttl", "INSERT", None, "waiting"), False),
]

#: The deletions of TR-0's last check: AWS delivered none of them.
TR0_DELETIONS = [record("_run_c2", "REMOVE", "active", None, gen=None), record("c2-task-a", "REMOVE", "success", None, gen=2),
                 record("c2-task-g", "REMOVE", "failed", None), record("output#c2#t#2026-11-18", "REMOVE", "success", None, gen=None)]


def test_the_filters_pass_exactly_what_aws_delivered_in_tr0():
    passed = [matches_any(TR0_FILTERS, r) for r, _ in TR0_WRITES]
    assert passed == [delivered for _, delivered in TR0_WRITES]
    assert sum(passed) == 11


def test_no_deletion_passes_as_in_tr0():
    assert not any(matches_any(TR0_FILTERS, r) for r in TR0_DELETIONS)


@pytest.mark.parametrize("rule,value,expected", [
    (["a", "b"], "b", True), (["a"], "c", False),
    ([{"prefix": "_run_"}], "_run_x", True), ([{"prefix": "_run_"}], "task", False),
    ([{"anything-but": ["a"]}], "b", True), ([{"anything-but": ["a"]}], "a", False),
    ([{"anything-but": "a"}], "b", True),
    ([{"exists": True}], "x", True),
])
def test_a_leaf_matches_a_present_value(rule, value, expected):
    assert matches({"f": rule}, {"f": value}) is expected


def test_a_missing_field_matches_only_exists_false():
    assert matches({"f": [{"exists": False}]}, {}) is True
    for rule in (["a"], [{"anything-but": ["a"]}], [{"prefix": "a"}], [{"exists": True}]):
        assert matches({"f": rule}, {}) is False


def test_a_missing_nested_object_matches_nothing():
    assert matches({"OldImage": {"status": {"S": [{"anything-but": ["x"]}]}}}, {"NewImage": {}}) is False


def test_an_operator_the_harness_does_not_model_is_refused():
    with pytest.raises(ValueError, match="not modelled"):
        matches({"f": [{"numeric": [">", 1]}]}, {"f": 2})


# ── the patterns the transition table generates (ADR #137 §7, invariant 7) ──


GENERATED = transitions.filter_patterns()
STATUSES = sorted({s.value for s in transitions.S})


def test_the_generated_filters_pass_what_aws_delivered_in_tr0():
    assert [matches_any(GENERATED, r) for r, _ in TR0_WRITES] == [delivered for _, delivered in TR0_WRITES]
    assert not any(matches_any(GENERATED, r) for r in TR0_DELETIONS)


@pytest.mark.parametrize("old", STATUSES)
def test_no_change_that_wakes_the_dispatcher_is_filtered_out(old):
    """Every pair of statuses, a rewrite of one included: a filter may pass more than the table
    wakes for, since the dispatcher drops the rest, but never less."""
    for new in STATUSES:
        woken = transitions.task_row_wakes({"status": old, "generation": 1}, {"status": new, "generation": 1})
        if woken:
            assert matches_any(GENERATED, record("t", "MODIFY", old, new)), (old, new)


def test_each_transition_of_the_table_passes_the_filters_exactly_when_it_wakes():
    for t in transitions.TASK_TRANSITIONS:
        for source in t.sources:
            event = "INSERT" if source is None else "MODIFY"
            assert matches_any(GENERATED, record("t", event, source, t.target)) is t.wakes, (t.name, source)


@pytest.mark.parametrize("item,passes", [
    (record("t", "INSERT", None, "waiting"), False),                        # an insert has no OldImage
    (record("output#p#t#2026-11-18", "MODIFY", "success", "success", gen=None), False),
    (record("_notify_warn_coordination_x", "INSERT", None, "warn", gen=None), False),
    (record("t", "MODIFY", "waiting", "waiting"), False),                    # a TTL or a due time
    (record("_run_p-run-d-h", "MODIFY", "active", "active", gen=None), True),  # a summary: through, then dropped
])
def test_the_fixture_records_of_section_7(item, passes):
    assert matches_any(GENERATED, item) is passes
    keys, body = item["dynamodb"]["Keys"]["execution_name"]["S"], item["dynamodb"]
    plain = {n: {k: next(iter(v.values())) for k, v in body[n].items()} if n in body else None
             for n in ("OldImage", "NewImage")}
    assert not transitions.wakes(keys, plain["OldImage"], plain["NewImage"])


# ── the rule on the end of executions (ADR #137 §7, ExecutionEndRule) ──────────

def test_wildcard_matches_any_run_of_characters_and_refuses_two_in_a_row():
    pattern = {"arn": [{"wildcard": "arn:aws:states:*:stateMachine:*-polyris-*"}]}
    assert matches(pattern, {"arn": "arn:aws:states:us-east-1:1:stateMachine:acme-dev-polyris-orders"})
    assert not matches(pattern, {"arn": "arn:aws:states:us-east-1:1:stateMachine:acme-dev-orders"})
    assert not matches(pattern, {})                                          # a missing field matches nothing
    escaped = {"f": [{"wildcard": "a\\*b"}]}
    assert matches(escaped, {"f": "a*b"}) and not matches(escaped, {"f": "axb"})
    with pytest.raises(ValueError):
        matches({"f": [{"wildcard": "a**b"}]}, {"f": "ab"})


def test_anything_but_needs_the_field_and_a_value_and_null_matches_null_alone():
    """The docs leave anything-but on null unstated: the stand takes the strict reading (stream_filters.py)."""
    pattern = {"error": [{"anything-but": "Polyris.Stopped"}]}
    assert matches(pattern, {"error": "States.Timeout"})
    assert not matches(pattern, {"error": "Polyris.Stopped"})
    assert not matches(pattern, {"error": None}) and not matches(pattern, {})
    assert matches({"error": [None]}, {"error": None}) and not matches({"error": [None]}, {})
    assert not matches({"error": [None]}, {"error": ""})                     # null and "" are not the same


def _rule_pattern() -> Dict[str, Any]:
    """The template's rule as CloudFormation resolves it on the harness's stack (acme, dev, us-east-1)."""
    from coordination_testkit import template
    from platform_stack import PARAMETERS, Stack

    stack = Stack.__new__(Stack)                    # names only: no tables or machines
    loaded = template()
    stack.resources = loaded["Resources"]
    stack.parameters = {**{k: v.get("Default") for k, v in loaded["Parameters"].items()}, **PARAMETERS}
    return json.loads(stack._sub(stack.resources["ExecutionEndRule"]["Properties"]["EventPattern"]))


def _end(machine: str, *, status: str = "ABORTED", error: Any = None, account: str = "123456789012") -> Dict[str, Any]:
    arn = f"arn:aws:states:us-east-1:{account}:stateMachine:{machine}"
    return {"source": "aws.states", "detail-type": "Step Functions Execution Status Change",
            "detail": {"stateMachineArn": arn, "executionArn": arn.replace(":stateMachine:", ":execution:") + ":x",
                       "name": "x", "status": status, "error": error, "cause": None}}


@pytest.mark.parametrize("event,passes", [
    (_end("acme-dev-polyris-dep-run-task-helper"), True),                             # stopped by hand: null
    (_end("acme-dev-polyris-dep-run-task-helper", status="FAILED", error="ClaimFailed"), True),
    (_end("acme-dev-polyris-orders", status="TIMED_OUT", error="States.Timeout"), True),
    (_end("ex-qa-polyris-orders", error="Console"), True),                             # a pipeline of its own config
    (_end("acme-dev-polyris-orders", status="FAILED", error="Polyris.RunFailed"), True),  # answered: one read
    (_end("acme-dev-polyris-dep-run-task-helper", error="Polyris.Stopped"), False),    # the engine's own stop
    (_end("acme-dev-polyris-orders", error="Polyris.Stopped"), False),
    (_end("acme-dev-polyris-orders", status="SUCCEEDED"), False),
    (_end("acme-dev-polyris-orders", status="RUNNING"), False),
    (_end("someone-elses-machine"), False),
    (_end("acme-dev-polyris-orders", account="210987654321"), False),
])
def test_the_rule_on_executions_ends_lets_through_the_abnormal_ends_of_polyris_machines(event, passes):
    assert matches(_rule_pattern(), event) is passes


def test_an_end_without_an_error_field_matches_no_branch_of_the_rule():
    """Step Functions always sends `error`, null when there is none (its docs' examples): an event without the
    field is not let through, as `[null]` and `anything-but` both need the field."""
    event = _end("acme-dev-polyris-orders")
    del event["detail"]["error"]
    assert not matches(_rule_pattern(), event)
