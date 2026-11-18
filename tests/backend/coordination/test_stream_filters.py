"""The harness's event filtering (stream_filters.py) agrees with what AWS delivered in TR-0.

TR-0 (C1, C2) put the four filters of ADR #137 §7 on a live mapping and
wrote 25 changes to the table: AWS delivered exactly 11 of them, and none of
four deletions. The same filters and the same records must give the same
answer here, or every harness scenario built on the filters tells a story
AWS does not.
"""

from typing import Any, Dict, Optional

import pytest

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
