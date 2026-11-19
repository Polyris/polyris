"""
polyris-local's trigger rules: the engine's own function decides (ADR #138 §6).

The rules themselves are tested cell by cell in
tests/sdk/test_coordination_trigger_rules.py. These tests pin what the mock
runner adds: every upstream already has a final result, every skip in it
comes from a rule, a step it does not execute counts as pending, and a run
records ``skipped``, ``upstream_failed`` or ``failed`` where the engine does.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from polyris.coordination import trigger_rules as tr
from polyris.local import TaskResult, _evaluate_trigger_rule


def _task(trigger_rule, dep_ids):
    return SimpleNamespace(trigger_rule=trigger_rule, dependencies=[SimpleNamespace(node_id=d) for d in dep_ids])


def _result(task_id, status):
    now = datetime.now(timezone.utc)
    return TaskResult(task_id=task_id, status=status, start_time=now, end_time=now)


@pytest.mark.parametrize("rule,results,verdict", [
    ("all_success", [], tr.READY),
    ("all_success", [("a", "success"), ("b", "success")], tr.READY),
    ("all_success", [("a", "success"), ("b", "failed")], tr.UPSTREAM_FAILED),
    ("all_success", [("a", "success"), ("b", "skipped")], tr.SKIP),    # a rule's skip cascades
    ("all_done", [("a", "failed"), ("b", "skipped")], tr.READY),
    ("one_failed", [("a", "success"), ("b", "failed")], tr.READY),
    ("one_failed", [("a", "success"), ("b", "success")], tr.SKIP),
    ("none_failed", [("a", "upstream_failed")], tr.UPSTREAM_FAILED),
    ("always", [("a", "success")], tr.FAIL),                           # a name ADR #138 turned down
])
def test_the_verdict_is_the_engines(rule, results, verdict):
    task = _task(rule, [task_id for task_id, _ in results])
    assert _evaluate_trigger_rule(task, [_result(t, s) for t, s in results])[0] == verdict


def test_an_upstream_this_runner_did_not_execute_counts_as_pending():
    verdict, reason = _evaluate_trigger_rule(_task("all_success", ["a", "wait_step"]), [_result("a", "success")])
    assert (verdict, reason) == (tr.WAIT, "waiting for its upstreams")


def test_a_mock_run_records_what_the_engine_would():
    from polyris import DAG, task
    from polyris.local import run

    arn = "arn:aws:states:us-east-1:000000000000:stateMachine:x"
    with DAG("local-rules") as dag:
        @task.sfn(arn=arn)
        def extract():
            raise RuntimeError("the source is down")

        @task.sfn(arn=arn)
        def load():
            return {}

        @task.sfn(arn=arn, trigger_rule="all_done")
        def cleanup():
            return {}

        @task.sfn(arn=arn, trigger_rule="one_failed")
        def compensate():
            return {}

        @task.sfn(arn=arn, trigger_rule="bogus")
        def typo():
            return {}

        e = extract()
        e >> load()
        e >> cleanup()
        e >> compensate()
        e >> typo()

    result = run(dag, mock=True)
    statuses = {r.task_id: r.status for r in result.task_results}
    assert statuses == {"extract": "failed", "load": "upstream_failed", "cleanup": "success",
                        "compensate": "success", "typo": "failed"}
    assert "⛔ 1 upstream_failed" in result.summary()
    assert "unknown trigger_rule 'bogus'" in next(r.error for r in result.task_results if r.task_id == "typo")
