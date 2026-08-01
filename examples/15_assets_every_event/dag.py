"""Assets — trigger on every event, not once per day (EXPERIMENTAL).

⚠️  Assets are experimental (v0.93.0): the API may change, and the visual asset
    console is not in the open-source build yet (engine + CLI lineage only).

Companion to `12_assets_schedule_trigger` — same trigger asset (`clean/orders`, produced
by `11_assets_outlets_inlets`'s `orders-clean`), different subscription shape, so you
can compare the two side by side on the same producer:

  12_assets_schedule_trigger:  schedule=[clean_orders]              — AND-of-one
  15 (this file):     schedule=AssetAny([clean_orders])    — OR-of-one

Both are satisfied by a single required asset, but they behave differently
when `orders-clean` runs MORE than once on the same calendar day:

  - `orders-analytics` (12): triggers on the FIRST run only. The second
    materialization that day hits the same day-scoped dedup key and is
    silently treated as a duplicate — see
    docs/reference/SPIKE_ASSET_TRIGGER_GRANULARITY.md.
  - `orders-live-feed` (this DAG): triggers on EVERY run, including the
    second, third, etc. — AssetAny has no day-scoped dedup at all.

To see the difference live: deploy this alongside `11_assets_outlets_inlets` and
`12_assets_schedule_trigger`, run `orders-clean` twice in a row (same day), and
compare execution counts — `orders-analytics` should show 1 new run,
`orders-live-feed` should show 2.

Run it locally (no AWS):  polyris-output --graph
"""
import warnings
import polyris
from polyris import DAG, task, Asset
from polyris.assets import AssetAny

warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

# Same asset name as produced in 11_assets_outlets_inlets — this is what links the lineage.
clean_orders = Asset("clean/orders", uri="s3://polyris-example/clean/orders/", group="processed")

with DAG(
    dag_id="orders-live-feed",
    schedule=AssetAny([clean_orders]),  # fires on EVERY clean/orders update, no day dedup
    description="Reacts to every clean/orders update, unlike orders-analytics (once/day).",
) as dag:

    @task.sfn(arn=ARN, inlets=[clean_orders])
    def publish_live_update():
        """Runs once per clean/orders materialization, however many times
        that happens in a single day — e.g. pushing a live feed / webhook /
        notification that should reflect every update, not a once-daily
        summary (that's what orders-analytics, example 12, is for)."""
        pass

    publish_live_update()
