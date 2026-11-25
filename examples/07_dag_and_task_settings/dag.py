"""Operational knobs — retries, backoff and timeouts.

The settings that matter once a pipeline is in production:

  - retry policy with exponential backoff, jitter, and a cap
  - per-task execution timeout and per-task orchestration timeout

Run it locally (no AWS):  polyris-validate -v
"""
from datetime import timedelta

from polyris import DAG, task

# NOTE: ARN hardcoded to the testing-infra CloudFormation stack.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="operational-demo",
    group="features",
    schedule="@daily",
    description="Production knobs: retries and timeouts.",
    tags=["example", "operations"],
) as dag:

    @task.sfn(arn=ARN)
    def scrape_live_source():
        pass

    # Flaky external call: retry with exponential backoff + jitter, capped delay.
    @task.sfn(
        arn=ARN,
        retries=5,
        retry_delay=timedelta(seconds=10),
        retry_exponential_backoff=True,     # 10s, 20s, 40s, ...
        retry_jitter=True,                  # spread retries to avoid thundering herd
        max_retry_delay=timedelta(minutes=5),
    )
    def call_flaky_api():
        pass

    # Bounded work: fail fast if a single execution runs too long.
    @task.sfn(
        arn=ARN,
        execution_timeout=timedelta(minutes=30),    # the task's own runtime cap
        orchestration_timeout=timedelta(hours=2),   # cap on the orchestration wait
    )
    def transform():
        pass

    @task.sfn(arn=ARN)
    def load():
        pass

    raw = scrape_live_source()
    enriched = call_flaky_api(raw)
    transform(enriched) >> load()
