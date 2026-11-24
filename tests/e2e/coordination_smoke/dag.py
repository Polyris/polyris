"""Pipelines for the live coordination smoke test (ADR #130; tests/e2e/test_coordination_smoke.py).

Deploy them once to a dev platform, then run the test against its console API:

    export POLYRIS_SMOKE_SFN_PREFIX=arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris
    cd tests/e2e/coordination_smoke && polyris-deploy --stage dev --profile <profile>

The tasks run the platform's own test state machines (`<prefix>-test-quick`,
`-test-success`, `-test-failure`), so the smoke test needs no workload of its
own. The test picks a fresh run date for every scenario, so facts that earlier
runs recorded never decide a new one.
"""

import os
import warnings
from datetime import timedelta

import polyris
from polyris import DAG, Asset, task

warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)

_PREFIX = os.environ.get("POLYRIS_SMOKE_SFN_PREFIX", "").rstrip("-")
if not _PREFIX:
    raise SystemExit("Set POLYRIS_SMOKE_SFN_PREFIX to the platform's state machine ARN prefix, for example "
                     "arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris")

QUICK = f"{_PREFIX}-test-quick"        # succeeds within seconds
SLOW = f"{_PREFIX}-test-success"       # succeeds after 20 seconds
FAILS = f"{_PREFIX}-test-failure"      # fails after 20 seconds

A = Asset("smoke/a", uri="s3://polyris-smoke/a/", group="smoke")
B = Asset("smoke/b", uri="s3://polyris-smoke/b/", group="smoke")

#: Short, so a wake-up that never comes fails the run instead of holding it for a day.
WAIT = timedelta(minutes=20)

with DAG("smoke-produce-a", schedule=None, description="Records smoke/a for its run's date") as produce_a:
    @task.sfn(arn=QUICK, outlets=[A])
    def make_a():
        """Records smoke/a when it succeeds."""
    make_a()

with DAG("smoke-produce-b", schedule=None, description="Records smoke/b for its run's date") as produce_b:
    @task.sfn(arn=QUICK, outlets=[B])
    def make_b():
        """Records smoke/b when it succeeds."""
    make_b()

with DAG("smoke-produce-a-by-hand", schedule=None,
         description="Fails; Mark Successful records smoke/a for the task's date") as produce_by_hand:
    @task.sfn(arn=FAILS, outlets=[A])
    def make_a_by_hand():
        """Fails on its own; marking it successful records smoke/a."""
    make_a_by_hand()

with DAG("smoke-wait-both", schedule=None, description="wait_for=[a, b]") as wait_both:
    @task.sfn(arn=QUICK, wait_for=[A, B], orchestration_timeout=WAIT)
    def use_both():
        """Runs once smoke/a and smoke/b both have a fact for the run's date."""
    use_both()

with DAG("smoke-wait-a", schedule=None, description="wait_for=[a]") as wait_a:
    @task.sfn(arn=QUICK, wait_for=[A], orchestration_timeout=WAIT)
    def use_a():
        """Runs once smoke/a has a fact for the run's date."""
    use_a()

with DAG("smoke-wait-either-after-upstream", schedule=None,
         description="wait_for=[a | b] behind a 20-second upstream task") as wait_either:
    @task.sfn(arn=SLOW)
    def upstream():
        """Takes 20 seconds, so a fact can arrive before it finishes."""

    @task.sfn(arn=QUICK, wait_for=[A | B], orchestration_timeout=WAIT)
    def use_either():
        """Runs once its upstream succeeded and either asset has a fact."""
    upstream() >> use_either()

with DAG("smoke-push-both", schedule=[A & B], description="Starts once a and b both have a fact for a date") as push_both:
    @task.sfn(arn=QUICK)
    def report():
        """The pushed run's work."""
    report()

with DAG("smoke-push-twin-1", schedule=[A], description="Pushed by smoke/a; same task id as twin 2") as twin_1:
    @task.sfn(arn=QUICK)
    def report():  # noqa: F811 — the same task id in another pipeline is the point (B-X1)
        """The pushed run's work."""
    report()

with DAG("smoke-push-twin-2", schedule=[A], description="Pushed by smoke/a; same task id as twin 1") as twin_2:
    @task.sfn(arn=QUICK)
    def report():  # noqa: F811 — the same task id in another pipeline is the point (B-X1)
        """The pushed run's work."""
    report()


# ── Tasks (ADR #137 §3, §4, §5; ADR #138) ─────────────────────────────────────

with DAG("smoke-chain", schedule=None, description="Three tasks in a row") as chain:
    @task.sfn(arn=QUICK)
    def first():
        """Starts the chain."""

    @task.sfn(arn=QUICK)
    def second():
        """Starts once first succeeded."""

    @task.sfn(arn=QUICK)
    def third():
        """Starts once second succeeded."""
    first() >> second() >> third()

with DAG("smoke-rules", schedule=None, description="A final failure decides each dependant by its rule") as rules:
    @task.sfn(arn=FAILS)
    def breaks():
        """Fails after 20 seconds and waits for a decision; the test fails it for good."""

    @task.sfn(arn=QUICK)
    def other():
        """Succeeds: the other upstream of needs_it."""

    @task.sfn(arn=QUICK)
    def needs_it():
        """all_success over breaks and other: upstream_failed once breaks fails for good."""

    @task.sfn(arn=QUICK, trigger_rule="all_done")
    def cleanup():
        """all_done: runs once breaks is final, whatever its outcome."""

    @task.sfn(arn=QUICK, trigger_rule="one_failed")
    def on_failure():
        """one_failed: runs once breaks failed for good."""
    broken = breaks()
    [broken, other()] >> needs_it()
    broken >> cleanup()
    broken >> on_failure()

with DAG("smoke-retry", schedule=None, description="Retries, then waits for a decision; a Skip lets its dependant run") as retry:
    @task.sfn(arn=FAILS, retries=1, retry_delay=timedelta(seconds=5))
    def flaky():
        """Fails, fails again on its retry, then waits for a decision."""

    @task.sfn(arn=QUICK)
    def after():
        """all_success: an operator's Skip of flaky counts as done (ADR #138)."""
    flaky() >> after()

with DAG("smoke-stop-task", schedule=None, description="Stop of a running task, then Restart with dependants") as stop_task:
    @task.sfn(arn=SLOW)
    def long():
        """Runs 20 seconds: the test stops it while it runs."""

    @task.sfn(arn=QUICK)
    def next_one():
        """upstream_failed after the stop; the Restart with dependants runs it again."""
    long() >> next_one()

with DAG("smoke-pause", schedule=None, description="A paused run starts no task until resumed") as pause:
    @task.sfn(arn=SLOW)
    def before_pause():
        """Runs 20 seconds; the test pauses the run meanwhile."""

    @task.sfn(arn=QUICK)
    def after_resume():
        """Starts only once the run is resumed."""
    before_pause() >> after_resume()

with DAG("smoke-stop-run", schedule=None, description="A Stop of the run stops its tasks") as stop_run:
    @task.sfn(arn=SLOW)
    def working():
        """Runs 20 seconds; the test stops the run meanwhile."""

    @task.sfn(arn=QUICK)
    def never():
        """Waits for working; the stop of the run stops it."""
    working() >> never()

with DAG("smoke-countdown", schedule=None, description="wait_before: the task starts after its countdown") as countdown:
    @task.sfn(arn=QUICK, wait_before=timedelta(seconds=30))
    def delayed():
        """Counts down 30 seconds; the sweeper's check ends the countdown, up to 5 minutes late."""
    delayed()
