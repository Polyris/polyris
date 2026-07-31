"""Assets — declaring what a task produces and consumes (EXPERIMENTAL).

⚠️  Assets are experimental (v0.93.0): the API may change, and the visual asset
    console is not in the open-source build yet (engine + CLI lineage only).

An **Asset** is a logical piece of data (a table, an S3 prefix, ...). A task
declares the assets it writes via `outlets=[...]` and the ones it reads via
`inlets=[...]`. Polyris uses these to track lineage and (optionally) to trigger
downstream pipelines when an asset updates.

Here one pipeline turns `raw/orders` into `clean/orders`:

    extract   --produces-->  raw/orders
    transform --reads-->     raw/orders  --produces-->  clean/orders

Run it locally (no AWS):  polyris-output --graph
"""
import warnings
import polyris
from polyris import DAG, task, Asset

# These examples intentionally construct Assets; silence the experimental notice.
warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

# Logical data assets (name + optional physical uri + optional UI group).
raw_orders = Asset("raw/orders", uri="s3://polyris-example/raw/orders/", group="raw")
clean_orders = Asset("clean/orders", uri="s3://polyris-example/clean/orders/", group="processed")

with DAG(
    dag_id="orders-clean",
    schedule="@daily",
    description="Extract raw orders, then clean them — with asset lineage.",
) as dag:

    @task.sfn(arn=ARN, outlets=[raw_orders])
    def extract():
        """Land raw orders. Declares it *produces* raw/orders."""
        pass

    @task.sfn(arn=ARN, inlets=[raw_orders], outlets=[clean_orders])
    def transform():
        """Read raw/orders, write clean/orders. Declares both sides of lineage."""
        pass

    extract() >> transform()
