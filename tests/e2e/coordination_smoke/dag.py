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
