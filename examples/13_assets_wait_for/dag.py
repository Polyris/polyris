"""Assets — waiting on a cross-pipeline asset, pull model (EXPERIMENTAL).

⚠️  Assets are experimental (v0.93.0): the API may change, and the visual asset
    console is not in the open-source build yet (engine + CLI lineage only).

Two ways a pipeline can depend on another pipeline's asset — pick whichever
fits the case:

  PUSH (schedule=[asset], see 12_assets_schedule_trigger)
    The whole pipeline is *asset-triggered*: no timer, it starts a new run
    the moment the asset updates. Good default when the consumer should
    react immediately and has nothing useful to do otherwise.

  PULL (wait_for=[asset], this example)
    The pipeline keeps its OWN schedule (here: manual/on-demand, could just
    as well be `@daily`) and a specific TASK inside it pauses at the
    dependency-check step until the asset is fresh enough, then continues.
    Good when the consumer has other, asset-independent work to do first
    (so it shouldn't wait from the very start of the run), or when you want
    a freshness *window* rather than "fires on every single update."

This pipeline reads `analytics/orders_daily` — the asset produced by
`12_assets_schedule_trigger`'s `orders-analytics` pipeline — and waits for it to be
no older than 24h before building a summary report:

    (orders-analytics)  aggregate --produces--> analytics/orders_daily
                                                      |
    (orders-report)                    wait_for <----+ (within 24h)
                                       build_summary

Run it locally (no AWS):  polyris-output --graph
"""
import warnings
import polyris
from polyris import DAG, task, Asset

warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

# Same asset name as produced in 12_assets_schedule_trigger — this is what links the lineage.
orders_daily = Asset("analytics/orders_daily", uri="s3://polyris-example/analytics/orders_daily/", group="aggregated")

with DAG(
    dag_id="orders-report",
    schedule="@daily",  # own timer — unlike 12_assets_schedule_trigger, NOT asset-triggered
    description="Build a summary report once analytics/orders_daily is fresh (pull model).",
) as dag:

    @task.sfn(arn=ARN, wait_for=[orders_daily.within(hours=24)])
    def build_summary():
        """Pauses at registration until analytics/orders_daily has updated in
        the last 24h, then reads it and builds the summary. If it's already
        fresh when this task registers, it proceeds immediately — no wait."""
        pass

    build_summary()
