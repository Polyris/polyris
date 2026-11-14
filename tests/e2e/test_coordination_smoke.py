"""Live smoke of the coordination layer (ADR #130): a release gate (CLAUDE.md #15).

It runs against a deployed platform whose console API is ``POLYRIS_API_URL``,
with the pipelines of ``tests/e2e/coordination_smoke/dag.py`` deployed (see that
file). Every scenario picks a run date no other run uses, so facts that earlier
runs recorded never decide it. Covered: the ``wait_for`` rows of PLAN §2.6,
Mark Successful, a restart, the push AND rule and two consumers with one task
id. The upgrade rehearsal, schedules and fault injection are manual checks.

    POLYRIS_API_URL=https://... POLYRIS_ID_TOKEN=plrs_... pytest tests/e2e/test_coordination_smoke.py -v

The run takes about 15 minutes.
"""

import random
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.write]

#: Long enough for a fact to reach a waiting task (stream, asset-fanout, reconcile, wrapper):
#: a task that has not moved after this long would not have moved on that fact.
SETTLE_SECONDS = 45
#: How long a scenario waits for a run to get where it should.
TIMEOUT_SECONDS = 300
POLL_SECONDS = 5

PRODUCE_A = ("smoke-produce-a", "make_a")
PRODUCE_B = ("smoke-produce-b", "make_b")


def _fresh_date() -> str:
    """A run date in the 1900s: one that no real run, and almost surely no earlier smoke run, used."""
    return (date(1900, 1, 1) + timedelta(days=random.randrange(36_000))).isoformat()


def _day_before(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


class Console:
    """The console API calls the scenarios need."""

    def __init__(self, api: Any):
        self.api = api

    def start(self, pipeline: str, day: str) -> str:
        """Start a run of `pipeline` for `day`; returns the run's execution name."""
        response = self.api.post("/api/pipeline-run", body={"input": {"current_date": day}}, params={"name": pipeline})
        assert response["status"] == 200, f"starting {pipeline} for {day}: {response}"
        return str(response["body"]["execution_arn"]).rsplit(":", 1)[1]

    def task(self, pipeline: str, day: str, name: str, run: Optional[str] = None) -> Optional[Dict[str, Any]]:
        response = self.api.get("/api/tasks", params={"pipeline": pipeline, "date": day})
        assert response["status"] == 200, response
        rows = [t for t in response["body"].get("tasks", [])
                if t.get("task_name") == name and (run is None or t.get("pipeline_execution") == run)]
        return rows[0] if rows else None

    def status(self, pipeline: str, day: str, name: str, run: Optional[str] = None) -> Optional[str]:
        row = self.task(pipeline, day, name, run)
        return None if row is None else str(row.get("status"))

    def runs(self, pipeline: str, day: str) -> List[Dict[str, Any]]:
        response = self.api.get("/api/runs", params={"pipeline": pipeline, "date": day})
        assert response["status"] == 200, response
        return list(response["body"].get("runs", []))

    def act(self, action: str, name: str, day: str, run: str) -> None:
        """An operator's action on a task: success, fail, restart."""
        response = self.api.post(f"/api/task-{action}", body={"date": day, "pipeline_execution": run},
                                 params={"name": name})
        assert response["status"] in (200, 202), f"{action} on {name}: {response}"

    def produce(self, producer: Tuple[str, str], day: str) -> str:
        """Run a producer for `day` and wait until its task succeeded, which records its fact."""
        pipeline, name = producer
        run = self.start(pipeline, day)
        eventually(lambda: self.status(pipeline, day, name, run) == "success", f"{pipeline} for {day} to succeed")
        return run


def eventually(check: Callable[[], Any], what: str, timeout: float = TIMEOUT_SECONDS) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(POLL_SECONDS)
    pytest.fail(f"after {timeout:.0f} s still waiting for {what}; last seen: {last!r}")


def stays(check: Callable[[], Any], what: str, seconds: float = SETTLE_SECONDS) -> None:
    """`check` holds for `seconds`: the other half of a wake-up, the task does not start too early."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert check(), f"{what} stopped holding"
        time.sleep(POLL_SECONDS)


@pytest.fixture
def console(api: Any) -> Console:
    return Console(api)


# ── wait_for (PLAN §2.6) ──────────────────────────────────────────────────


@pytest.mark.parametrize("first,second", [(PRODUCE_A, PRODUCE_B), (PRODUCE_B, PRODUCE_A)], ids=["a-then-b", "b-then-a"])
def test_a_task_waiting_for_two_assets_starts_only_once_both_have_a_fact(console, first, second):
    """Rows 1 and 2: it waits after the first fact and starts after the second, in either order."""
    day = _fresh_date()
    run = console.start("smoke-wait-both", day)
    waiting = lambda: console.status("smoke-wait-both", day, "use_both", run) == "waiting"  # noqa: E731
    eventually(waiting, "use_both to wait")
    console.produce(first, day)
    stays(waiting, "use_both waiting with one asset")
    console.produce(second, day)
    eventually(lambda: console.status("smoke-wait-both", day, "use_both", run) == "success", "use_both to run")


def test_a_fact_recorded_before_the_run_counts(console):
    """Row 3: smoke/a exists before the run starts; the task starts once smoke/b arrives."""
    day = _fresh_date()
    console.produce(PRODUCE_A, day)
    run = console.start("smoke-wait-both", day)
    waiting = lambda: console.status("smoke-wait-both", day, "use_both", run) == "waiting"  # noqa: E731
    eventually(waiting, "use_both to wait")
    stays(waiting, "use_both waiting for smoke/b")
    console.produce(PRODUCE_B, day)
    eventually(lambda: console.status("smoke-wait-both", day, "use_both", run) == "success", "use_both to run")


def test_an_asset_that_arrives_before_the_upstream_finishes_waits_for_the_upstream(console):
    """Row 4: wait_for=[a | b] behind a 20-second upstream task; the fact usually arrives first."""
    pipeline, day = "smoke-wait-either-after-upstream", _fresh_date()
    run = console.start(pipeline, day)
    console.start(PRODUCE_A[0], day)

    def upstream_done() -> bool:
        upstream, use = console.status(pipeline, day, "upstream", run), console.status(pipeline, day, "use_either", run)
        assert upstream == "success" or use not in ("running", "success"), f"use_either is {use} before its upstream"
        return upstream == "success"

    eventually(upstream_done, "the upstream to finish")
    eventually(lambda: console.status(pipeline, day, "use_either", run) == "success", "use_either to run")


def test_a_fact_for_another_date_does_not_start_the_task(console):
    """Row 5 (D8): a fact serves only its own date."""
    day = _fresh_date()
    run = console.start("smoke-wait-a", day)
    waiting = lambda: console.status("smoke-wait-a", day, "use_a", run) == "waiting"  # noqa: E731
    eventually(waiting, "use_a to wait")
    console.produce(PRODUCE_A, _day_before(day))
    stays(waiting, "use_a waiting after a fact for the day before")
    console.produce(PRODUCE_A, day)
    eventually(lambda: console.status("smoke-wait-a", day, "use_a", run) == "success", "use_a to run")


def test_marking_a_producer_successful_records_its_fact(console):
    """Row 6: Mark Successful records the fact for the marked task's date, and the stream wakes the waiter."""
    day = _fresh_date()
    run = console.start("smoke-wait-a", day)
    eventually(lambda: console.status("smoke-wait-a", day, "use_a", run) == "waiting", "use_a to wait")
    producer = console.start("smoke-produce-a-by-hand", day)
    # A failed task waits for a decision (waiting_decision) until the decision timeout, then fails.
    eventually(lambda: console.status("smoke-produce-a-by-hand", day, "make_a_by_hand", producer)
               in ("waiting_decision", "failed"), "the producer to fail")
    console.act("success", "make_a_by_hand", day, producer)
    eventually(lambda: console.status("smoke-wait-a", day, "use_a", run) == "success", "use_a to run")


def test_a_restarted_task_waits_for_its_assets_again_and_runs_once(console):
    """Row 7: failed by the operator, restarted, it registers again, waits, and runs when the fact comes."""
    day = _fresh_date()
    run = console.start("smoke-wait-a", day)
    waiting = lambda: console.status("smoke-wait-a", day, "use_a", run) == "waiting"  # noqa: E731
    eventually(waiting, "use_a to wait")
    console.act("fail", "use_a", day, run)
    eventually(lambda: console.status("smoke-wait-a", day, "use_a", run) == "failed", "use_a to fail")
    console.act("restart", "use_a", day, run)
    eventually(waiting, "use_a to wait again")
    stays(waiting, "use_a waiting after its restart")
    console.produce(PRODUCE_A, day)
    eventually(lambda: console.status("smoke-wait-a", day, "use_a", run) == "success", "use_a to run")
    row = console.task("smoke-wait-a", day, "use_a", run)
    assert row is not None and row.get("attempt") == 2, row


# ── asset-scheduled pipelines ──────────────────────────────────────────────


def test_an_and_schedule_starts_one_run_per_date(console):
    """schedule=[a & b]: no run on one asset, one run on both, none more on a repeated event."""
    day = _fresh_date()
    console.produce(PRODUCE_A, day)
    stays(lambda: not console.runs("smoke-push-both", day), "no pushed run with one asset")
    console.produce(PRODUCE_B, day)
    eventually(lambda: len(console.runs("smoke-push-both", day)) == 1, "one pushed run")
    console.produce(PRODUCE_B, day)                         # the same event again
    stays(lambda: len(console.runs("smoke-push-both", day)) == 1, "still one pushed run")
    eventually(lambda: console.status("smoke-push-both", day, "report") == "success", "the pushed run's task")


def test_two_consumers_with_the_same_task_id_both_run(console):
    """B-X1: each pushed run's task is its own row."""
    day = _fresh_date()
    console.produce(PRODUCE_A, day)
    for twin in ("smoke-push-twin-1", "smoke-push-twin-2"):
        eventually(lambda twin=twin: console.status(twin, day, "report") == "success", f"{twin}'s task to run")
