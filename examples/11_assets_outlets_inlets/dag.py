"""Assets — declaring what a task produces and consumes (EXPERIMENTAL).

⚠️  Assets are experimental: the API may change. List each asset's producers and consumers with `polyris-output --assets`.

An **Asset** is a logical piece of data (a table, an S3 prefix, ...). A task
declares the assets it writes via `outlets=[...]` and the ones it reads via
`inlets=[...]`. When the task succeeds, polyris records its `outlets`, which can trigger
downstream pipelines; `inlets` only document what the task reads.

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
    group="orders",
    schedule="@daily",
    description="Extract raw orders, then clean them, declaring the assets each task reads and writes.",
) as dag:

    @task.sfn(arn=ARN, outlets=[raw_orders])
    def extract():
        """Land raw orders. Declares it *produces* raw/orders."""
        pass

    @task.sfn(arn=ARN, inlets=[raw_orders], outlets=[clean_orders])
    def transform():
        """Read raw/orders, write clean/orders. Declares both the asset it reads and the one it writes."""
        pass

    extract() >> transform()
