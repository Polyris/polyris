"""Operational knobs — retries, backoff, timeouts, and backfill behavior.

The settings that matter once a pipeline is in production:

  - retry policy with exponential backoff, jitter, and a cap
  - per-task execution timeout and per-task orchestration timeout
  - `skip_on_backfill` — don't re-run a live scraper when backfilling history
  - `catchup=False` — only run going forward, don't fill the gap since start_date
  - `max_active_tasks` / `max_active_runs` — concurrency limits
  - a bounded active window with `start_date` / `end_date`

Run it locally (no AWS):  polyris-validate -v
"""
from datetime import datetime, timedelta

from polyris import DAG, task

# NOTE: ARN hardcoded to the testing-infra CloudFormation stack.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="operational-demo",
    schedule="@daily",
    description="Production knobs: retries, timeouts, backfill, concurrency.",
    tags=["example", "operations"],
    catchup=False,                         # don't backfill the gap since start_date
    max_active_tasks=4,                    # at most 4 tasks running at once
    max_active_runs=1,                     # one DAG run at a time
    start_date=datetime(2026, 1, 1),
    end_date=datetime(2027, 1, 1),
) as dag:

    # A live scraper — during a backfill of old dates this should NOT re-run.
    @task.sfn(arn=ARN, skip_on_backfill=True)
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
