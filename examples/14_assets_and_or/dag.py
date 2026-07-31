"""Assets — AND vs OR trigger logic with multiple assets (EXPERIMENTAL).

⚠️  Assets are experimental (v0.93.0): the API may change, and the visual asset
    console is not in the open-source build yet (engine + CLI lineage only).

`12_assets_schedule_trigger` and `13_assets_wait_for` each depend on a single asset, so
AND and OR behave identically there (nothing to distinguish them with only
one input). This example has TWO independent upstream sources, so the
difference actually matters:

    sales_daily = Asset("sales/daily")
    inventory_daily = Asset("inventory/daily")

  AND  schedule=[sales_daily, inventory_daily]
       Waits for BOTH to update before running. If sales lands at 6am and
       inventory at 6:15am, the AND-triggered pipeline runs once, at 6:15am
       — not twice. Good for a report that's wrong unless every input is in.

  OR   schedule=[sales_daily | inventory_daily]
       Runs on EITHER update — twice in the scenario above, once per source.
       Good for a change-log / audit trail that should record every update
       to any watched asset, independently.

Same two rules as the DSL.md syntax table:

    schedule=[a, b]    or   schedule=[a & b]     → AND (both required)
    schedule=[a | b]                             → OR  (either fires)

Flow:

    (produce_sales)      --produces--> sales/daily      --\\
                                                            +--> (combined_report, AND)
    (produce_inventory)  --produces--> inventory/daily  --/  \\--> (change_log, OR)

Run it locally (no AWS):  polyris-output --graph
"""
import warnings
import polyris
from polyris import DAG, task, Asset

warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

sales_daily = Asset("sales/daily", uri="s3://polyris-example/sales/daily/", group="raw")
inventory_daily = Asset("inventory/daily", uri="s3://polyris-example/inventory/daily/", group="raw")

# --- Two independent upstream sources ---

with DAG(
    dag_id="produce-sales",
    schedule="@daily",
    description="Produces sales/daily (upstream source #1 for the AND/OR examples).",
) as sales_dag:

    @task.sfn(arn=ARN, outlets=[sales_daily])
    def load_sales():
        pass

    load_sales()

with DAG(
    dag_id="produce-inventory",
    schedule="@daily",
    description="Produces inventory/daily (upstream source #2 for the AND/OR examples).",
) as inventory_dag:

    @task.sfn(arn=ARN, outlets=[inventory_daily])
    def load_inventory():
        pass

    load_inventory()

# --- AND consumer: needs BOTH sales and inventory ---

with DAG(
    dag_id="combined-report",
    schedule=[sales_daily, inventory_daily],  # AND — waits for both
    description="Runs only once BOTH sales/daily and inventory/daily are ready.",
) as and_dag:

    @task.sfn(arn=ARN, inlets=[sales_daily, inventory_daily])
    def build_combined_report():
        """A report that's meaningless with only one of the two inputs — so
        this waits for both, however far apart they land."""
        pass

    build_combined_report()

# --- OR consumer: reacts to EITHER sales or inventory updating ---

with DAG(
    dag_id="change-log",
    schedule=[sales_daily | inventory_daily],  # OR — fires on either
    description="Fires once per update to EITHER asset (audit-trail style).",
) as or_dag:

    @task.sfn(arn=ARN, inlets=[sales_daily, inventory_daily])
    def record_change():
        """Runs independently for each source's update — twice a day in the
        normal case (once when sales lands, once when inventory lands),
        unlike combined_report which runs once after both are in."""
        pass

    record_change()
