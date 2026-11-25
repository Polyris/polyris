# Asset-based orchestration

> **Experimental.** Assets are an experimental feature: the API (`Asset`, `outlets`, `inlets`, `wait_for`, asset-triggered `schedule`) may change. See [Known experimental limitations](#known-experimental-limitations). Not recommended for production pipelines. Silence the runtime warning with `warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)`.
> <!-- EXPERIMENTAL-ASSETS: remove this banner when assets graduate to stable. -->

## Overview

Assets link pipelines through the data they produce, without references from one pipeline to another. When a producer task succeeds, polyris records a [fact](../../GLOSSARY.md#fact) of each asset it produces, dated by its run's date; consumers start or continue on that fact.

**Key concepts:**
- **Asset**: a named piece of data (file, table, dataset)
- **Producer**: a task that creates an asset (`outlets=[...]`)
- **Consumer**: a pipeline that starts when an asset is ready (`schedule=[...]`), or a task that waits for it (`wait_for=[...]`)

`polyris-output --assets` lists the assets that the pipeline file in the current folder declares, with their producers and consumers within that file.

## Known experimental limitations

| Area | Current behavior |
|------|------------------|
| Partitions below a day | A scheduled run is dated by its day, so runs more often than daily share their day's partition, and an hourly asset's facts count for the whole day. |
| A late start | A waiting task normally starts within seconds of its last condition. If that signal is lost, polyris starts the task at its next check, at most about 40 minutes later, and the bell shows a `sweeper` warning. |

## Contents

- [Defining Assets](#defining-assets) — naming, [granularity](#granularity), schema declaration (schema is optional; skip on first read)
- [Producer Tasks (`outlets`)](#producer-tasks-outlets)
- [Consumer DAGs (`schedule`)](#consumer-dags-schedule) — AND / OR / mixed logic
- [Inlets](#inlets-documentation), [pull-based dependencies (`wait_for`)](#pull-based-dependencies-wait_for)
- [Asset events](#asset-events), [lineage](#asset-lineage)
- [Complete example](#complete-example), [best practices](#best-practices)

---

## Defining Assets

```python
from polyris import Asset

# Simple asset
processed = Asset(name="processed/acme")

# Asset with URI (for lineage tracking)
processed = Asset(
    name="processed/acme",
    uri="s3://my-bucket/processed/acme/"
)

# Asset with a group
processed = Asset(
    name="processed/acme",
    uri="s3://my-bucket/processed/acme/",
    group="acme"
)
```

### Asset Naming Conventions

```
{group}/{type}         # e.g., acme/raw, acme/processed
{domain}/{entity}      # e.g., catalog/products, orders/daily
{source}/{stage}       # e.g., api/raw, api/cleaned
```

### Granularity

`granularity` says how often the asset gets a new partition: `"hourly"`, `"daily"` (the default), `"weekly"` or `"monthly"`.

```python
from polyris import Asset

prices = Asset("pricing/prices", granularity="weekly")
```

Each successful producer run records a fact of the asset, dated by the run's date. A consumer run for date D, through `wait_for` or `schedule`, accepts a fact dated F by the asset's granularity:

| Granularity | A fact dated F serves the runs for |
|---|---|
| `hourly`, `daily` | D = F |
| `weekly` | D from F to the end of the week after F's week |
| `monthly` | D from F to the end of the month after F's month |

A daily consumer of a weekly asset therefore uses the latest version from this week or the last one, and a run for a past date waits for a fact of its own date, never today's.

A run's date is a day, such as `2026-01-12`, or the key of an hourly, weekly or monthly partition (`2026-01-12T06`, `2026-W03`, `2026-01`), which counts as its first day. A `wait_for` task in a run dated otherwise, for example `20260112`, fails with the cause.

### Schema Declaration

Declare a column schema directly in the asset definition. The schema is stored with the asset in the pipeline's task specs. A schema, like the Glue table and the owner below, describes the asset: nothing reads it while a pipeline runs, and `polyris-validate --all` compares the schemas that different pipelines declare.

**Recommended form: `Column` + `types`.**

```python
from polyris import Asset, Column, types as t

orders = Asset(
    name="retail/orders",
    uri="s3://bucket/orders/",
    owner="data-team",
    schema=[
        Column("order_id",    t.bigint(),       primary_key=True, nullable=False,
                                                description="Unique order identifier"),
        Column("customer_id", t.bigint(),       nullable=False, description="FK to customers"),
        Column("event_date",  t.date(),         partition_key=True),
        Column("amount",      t.decimal(10, 2), description="Total order amount"),
        Column("status",      t.string(),       description="pending/confirmed/shipped/delivered"),
        Column("created_at",  t.timestamp()),
    ],
)
```

**Available types** (factory functions in `polyris.types`, 21 in total):

| Group         | Factory                                                                |
|---------------|------------------------------------------------------------------------|
| Integers      | `tinyint()`, `smallint()`, `integer()`, `bigint()`                     |
| Floating      | `float_()`, `double()`                                                 |
| Decimal       | `decimal(precision, scale)`                                            |
| Boolean       | `boolean()`                                                            |
| Strings       | `string()`, `varchar(length)`, `char(length)`                          |
| Binary        | `binary()`, `fixed_binary(length)`                                     |
| Date / time   | `date()`, `time()`, `timestamp(tz_aware=True)`, `timestamp_ntz()`      |
| Identifiers   | `uuid()`                                                               |
| Semi-struct   | `json_()`                                                              |
| Nested        | `array(inner)`, `struct(field=t.<...>, ...)`, `map_(key, value)`       |

`integer`, `float_`, `json_`, `map_` use trailing/different names to avoid
shadowing Python builtins or stdlib modules.

**Column constraints:**

| Field            | Default | Meaning                                                   |
|------------------|---------|-----------------------------------------------------------|
| `nullable`       | `True`  | Whether the column may contain `NULL`.                    |
| `primary_key`    | `False` | Column is part of the primary key.                        |
| `partition_key`  | `False` | Column partitions the asset.                              |
| `unique`         | `False` | Values must be unique.                                    |
| `description`    | `""`    | Human-readable description.                               |
| `default`        | `None`  | JSON-serializable default value.                          |

**Legacy formats (still supported, no deprecation warning):**

```python
# Tuple form
Asset(name="retail/orders", schema=[
    ("order_id", "bigint", "Unique order identifier"),
    ("amount",   "decimal(10,2)"),
])

# Dict form
Asset(name="retail/orders", schema=[
    {"name": "order_id", "type": "bigint", "primary_key": True},
    {"name": "amount",   "type": "decimal(10,2)"},
])
```

All three forms are normalized to `List[Column]` internally. You can mix them
in a single declaration if you are migrating gradually.

**Schema conflict detection:** the same asset can be declared in multiple pipelines with different schemas — say a producer declares 8 columns and a consumer declares 3. `polyris-validate --all` warns about each such asset: a column declared with two types, or a different number of columns. Reconcile the divergent declaration based on the warning.

### Glue Catalog Reference

Link an asset to a table in **AWS Glue Data Catalog**. The Glue Data Catalog
has a three-level structure that maps to Polyris fields as follows:

| AWS Glue concept              | Maps to                          |
|-------------------------------|----------------------------------|
| **Region**                    | `glue_region`                    |
| **Glue Data Catalog ID**      | `glue_catalog`                   |
| **Database**                  | first half of `glue_table`       |
| **Table**                     | second half of `glue_table`      |

The fields are stored with the asset in the pipeline's task specs.

A Glue Data Catalog ID **is** the 12-digit AWS account ID — there is one
Glue Data Catalog per AWS account per region. The full ARN looks like:

```
arn:aws:glue:<region>:<catalog-id>:table/<database>/<table>
arn:aws:glue:eu-west-1:222222222222:table/analytics/orders
                ↑          ↑               ↑          ↑
            glue_region  glue_catalog ←─ glue_table ─→
```

```python
# Same-account, same-region (the common case)
orders = Asset(
    name="retail/orders",
    glue_table="analytics.orders",          # database=analytics, table=orders
)

# Cross-account (the catalog belongs to another AWS account)
orders = Asset(
    name="retail/orders",
    glue_table="analytics.orders",
    glue_catalog="222222222222",            # AWS account ID of the catalog owner
)

# Cross-region (catalog lives in a different AWS region)
orders = Asset(
    name="retail/orders",
    glue_table="analytics.orders",
    glue_region="eu-west-1",
)
```

#### Note on Amazon Athena's data sources

Athena's query editor shows a four-level hierarchy (Data source → Catalog
→ Database → Table). "Data source" and the inner "Catalog" levels are
**Athena-side aliases**, not AWS Glue concepts. By default Athena's data
source `AwsDataCatalog` maps directly to your AWS account's Glue Data
Catalog — for tables visible there, the three SDK fields above describe
the full addressing.

What is **not** covered:

- **Athena Federated Catalogs** (Lambda-backed connectors to Hive,
  MySQL, Snowflake, etc.) — Polyris targets the Glue Data Catalog
  directly, not the Athena Federated Query API.
- **Athena DataSources registered as cross-account aliases** — these
  are Athena UI conveniences. The underlying catalog is still a Glue
  Data Catalog identifiable by its AWS account ID; use `glue_catalog`
  with that account ID instead of going through the Athena alias.

### Ownership

Record the team that owns an asset.

```python
orders = Asset(
    name="retail/orders",
    owner="data-team",
)
```

### Constructing Assets from Existing Schema Sources

When the schema already lives in another system (a Parquet file, a
pydantic model, a Glue Catalog table) you can skip the manual `Column`
list and use a `from_*` constructor.

**From a pyarrow.Schema (Iceberg, Parquet, BigQuery, Polars, Pandas, DuckDB):**

```python
import pyarrow.parquet as pq
from polyris import Asset

sample = pq.read_metadata("s3://bucket/orders/sample.parquet")
orders = Asset.from_pyarrow(
    sample.schema.to_arrow_schema(),
    name="retail/orders",
    glue_table="analytics.orders",
)
```

Requires the `pyarrow` extra:
`pip install "polyris[pyarrow] @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"`.
The bridge to all six formats above goes through pyarrow as a hub — one
optional dependency, many integrations.

**Shortcut: from a Parquet file directly.**

```python
from polyris import Asset

# Local path
orders = Asset.from_parquet("samples/orders.parquet", name="retail/orders")

# S3 path — uses pyarrow's built-in S3 filesystem; needs credentials
# in the standard chain (env vars, ~/.aws, IAM role)
orders = Asset.from_parquet(
    "s3://bucket/orders/sample.parquet",
    name="retail/orders",
    glue_table="analytics.orders",
)
```

Convenience wrapper over `from_pyarrow` that reads only the file
footer (no row data is fetched). Same `pyarrow` extra requirement.

**From a pydantic v2 model:**

```python
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field
from polyris import Asset

class Order(BaseModel):
    order_id: int = Field(description="Unique order identifier")
    amount: Decimal
    created_at: datetime
    tags: list[str] = []

orders = Asset.from_pydantic(Order, name="retail/orders")
```

Requires the `pydantic` extra:
`pip install "polyris[pydantic] @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"`.
Pydantic field descriptions, defaults, and `Optional[...]` markers all
carry over.

**Naming note.** If `name=` is omitted, `from_pydantic` falls back to
the model's class name (`Order` → `name="Order"`). This is convenient
for quick prototypes but produces a CamelCase asset name with no group
— in production you almost always want an explicit `name="domain/asset"`.

**From an existing Glue Catalog table (deploy-time fetch):**

```python
from polyris import Asset

orders = Asset.from_glue_table(
    "analytics.orders",
    name="retail/orders",
    owner="data-team",
)
```

The constructor sets `glue_table` on the resulting asset. It runs on your machine at deploy time, as a one-shot schema import, and reads Glue with your local credentials. Cross-account: pass `catalog_id="<account-id>"`.

---

## Producer Tasks (outlets)

A task with `outlets` emits an asset event when it completes successfully:

```python
with DAG("etl", schedule="@daily") as dag:
    
    @task.sfn(arn=..., outlets=[processed])
    def process_data():
        """When this completes, 'processed' asset event is emitted."""
        pass
```

**What happens:** when the task succeeds, polyris records a fact of each outlet, dated by the run's date, in the same write as the success. Within seconds, the tasks that wait for the asset are checked again, and the pipelines scheduled on it start if their schedule is met.

---

## Consumer DAGs (schedule)

A DAG can be triggered by asset events instead of time:

```python
from polyris import Asset

# Reference the same asset
processed = Asset(name="processed/acme")

with DAG(
    "feeds",
    schedule=[processed],  # Trigger when 'processed' is ready
) as dag:
    
    @task.sfn(arn=...)
    def build_feeds():
        pass
```

**What happens:** a producer task succeeds and records its fact for its run's date. polyris evaluates the consumer's whole `schedule` for that date, with the same rule as `wait_for` (`.within(...)` is judged as of the fact's time), and if it is met, starts the consumer for that date.

A run's name is derived from the consumer, the date and the kind of trigger, so a repeated event never starts a second run. A failure to start one consumer never affects the producer or other consumers: a transient one is retried, anything else shows up as a warning.

A consumer reacts only to facts recorded after its first deploy. Deploying a new consumer does not start it for facts that already exist; its first run starts with the next fact.

Remove an asset from a consumer's `schedule` and redeploy, and that asset no longer starts it: the old subscription is deleted the next time the asset records a fact. The same holds for a consumer you move to a cron schedule. `polyris-destroy` deletes a consumer's subscriptions with its stack.

---

## AND Logic (All Assets Required)

Use `&` operator when ALL assets must be ready:

```python
asset_a = Asset("data/asset-a")
asset_b = Asset("data/asset-b")
asset_c = Asset("data/asset-c")

with DAG(
    "combined",
    schedule=[asset_a & asset_b & asset_c],  # ALL required
) as dag:
    ...
```

**How it works:**
1. Every fact re-evaluates the schedule from the recorded facts for that fact's date — nothing is counted, so a lost or repeated event changes nothing
2. The DAG starts once every asset has a fact that serves the date
3. One run per DAG and date: a later event for the same date starts nothing, as long as the first run's history is kept (a year and 90 days). To run a date again, after its upstream data was recomputed, press Run for it

**Example timeline:**
```
10:00 - asset_a arrives → b and c missing for the date, wait
10:15 - asset_b arrives → c missing, wait
10:30 - asset_c arrives → all present → START the DAG for the date
10:45 - asset_c arrives again → the date's run exists, nothing happens
```

Facts of different granularities combine: with `schedule=[daily_sales, weekly_prices]`, each day's sales fact starts that day's run once the current or previous week's prices fact exists.

---

## OR Logic (Any Asset Triggers)

Use `|` operator when ANY asset triggers the DAG:

```python
asset_a = Asset("data/asset-a")
asset_b = Asset("data/asset-b")

with DAG(
    "processor",
    schedule=[asset_a | asset_b],  # ANY triggers
) as dag:
    ...
```

**Note:** Each asset event triggers a separate DAG run.

---

## Mixed Logic

Combine AND and OR:

```python
# Triggers when: (A AND B) OR C
with DAG(
    "mixed",
    schedule=[(asset_a & asset_b) | asset_c],
) as dag:
    ...
```

---

## Inlets (Documentation)

Use `inlets` to document which assets a task consumes:

```python
@task.sfn(
    arn=...,
    inlets=[raw_data],      # Consumes raw_data
    outlets=[processed]      # Produces processed
)
def transform():
    pass
```

**Note:** Inlets don't affect execution - they're for lineage tracking.

---

## Pull-based dependencies (`wait_for`)

`wait_for` makes a task pause until an upstream `Asset` is available (and
optionally fresh). Unlike push-based schedules (`schedule=[asset]`),
pull-based `wait_for` runs on the consumer's own cadence and blocks the
individual task until the asset condition holds.

### `wait_for` patterns

| Pattern | Meaning |
|---------|---------|
| `wait_for=[asset_x]` | A fact of `asset_x` that serves the run's date ([granularity](#granularity)) |
| `wait_for=[asset_x.within(hours=6)]` | The newest fact of the latest partition up to the run's date is at most 6 hours old |
| `wait_for=[asset_x.within(days=1)]` | … at most 1 day old |
| `wait_for=[asset_x.within(weeks=2)]` | … at most 2 weeks old |
| `wait_for=[asset_x.within(days=1, hours=12)]` | … at most 36 hours old (units combine) |
| `wait_for=[asset_x.consecutive(days=7)]` | A fact for each of the 7 days that end on the run's date |
| `wait_for=[a, b]` | AND: both `a` and `b` |
| `wait_for=[a \| b]` | OR: either `a` or `b` |
| `wait_for=[a & b]` | AND, the explicit form of the list |
| `wait_for=[a.consecutive(days=7), b.within(hours=24)]` | AND of a 7-day run and a fresh fact |
| `wait_for=[a.consecutive(days=7) \| manual_override]` | a 7-day run, or the override |

A task checks its conditions when its own dependencies are met and again whenever a fact of one of its assets arrives, so it starts as soon as the last condition holds.

### Full example — producer + consumer

```python
from polyris import DAG, task, Asset

inventory = Asset("inventory", uri="s3://bucket/inventory/")
catalog = Asset("catalog")

# Producer pipeline — publishes the inventory asset when extract completes.
with DAG("producer", schedule="@daily") as pipeline:
    @task.sfn(arn="${extract_arn}", outlets=[inventory])
    def extract():
        pass

# Consumer pipeline — runs hourly, but individual tasks block on the asset.
with DAG("consumer", schedule="@hourly") as pipeline:
    # The inventory fact for this run's date.
    @task.sfn(arn="${process_arn}", wait_for=[inventory])
    def process():
        pass

    # The latest catalog partition up to this run's date, at most 24 hours old.
    @task.sfn(arn="${report_arn}", wait_for=[catalog.within(hours=24)])
    def report():
        pass
```

### Timeout for cross-pipeline dependencies

A task waits for its `wait_for` assets 7 days from when its own dependencies are met, then fails. `orchestration_timeout` replaces that limit: it caps all of the task's waiting, for its upstreams and its assets, from when the wait began.

```python
from datetime import timedelta

@task.sfn(
    arn=...,
    wait_for=[weekly_report],
    orchestration_timeout=timedelta(days=10),  # wait up to 10 days, upstreams and assets
    execution_timeout=timedelta(hours=2),      # each attempt of the work: up to 2 hours
)
def depends_on_weekly():
    pass
```

`orchestration_timeout` never falls back to `execution_timeout`. `polyris-deploy` warns when it is shorter than the 7 days of the wait.

### In the Console

The Task Detail modal shows a task's `wait_for` assets as a labelled
section, with the freshness constraint as a badge:

```
Asset Dependencies:  acme/weekly-complete  8d
```

The badge shows the window declared in `.within(...)`: in days when it is a whole number of days, otherwise in hours. A `.consecutive(days=7)` condition shows `7d` with a calendar icon.

### How it works

How polyris wakes a waiting task and starts a consumer: [ARCHITECTURE.md § Flow 4](../architecture/ARCHITECTURE.md#flow-4-asset-based-cross-pipeline-triggers).

---

## Asset events

A producer task records one fact per outlet when it succeeds, in the `asset-events` table:

| Field | Example | Meaning |
|---|---|---|
| `asset_name` | `processed/acme` | the asset |
| `event_time` | `2026-01-12T06:41:07.123Z` | when the fact was recorded |
| `partition_key` | `2026-01-12` | the producer run's date |
| `partition_date` | `2026-01-12` | the first day of that partition |
| `granularity` | `daily` | the asset's declared [granularity](#granularity) |
| `source_dag`, `source_task` | `acme-daily`, `process_data` | the producer |

Marking a producer task successful in the console records its outlets' facts in the same write, dated by the task's run, not by the day you mark it. When the facts cannot be recorded, nothing is: the console refuses the action and names the cause. A task that succeeds on its own also records its status, its output and its facts in one write. When that write fails, the task waits for your decision (`waiting_decision`) with the cause: restart it once the cause is fixed.

A fact that no reader can place on a partition (for example, one recorded by an older version for a run date such as `latest`) counts for no task, and the bell shows a warning for it.

---

## Asset lineage

`polyris-output --assets` prints, as JSON, the assets that the pipeline file in the current folder declares, with their producers and consumers within that file. A producer or consumer in another pipeline's file does not show:

```bash
cd pipelines/acme && polyris-output --assets
```

| Declaration | Lineage |
|--------------|---------|
| `outlets` | the task produces the asset |
| `inlets` | the task consumes the asset (documentation only, no runtime effect) |
| `wait_for` | the task consumes the asset and waits for it |
| `schedule=[asset]` | the pipeline is triggered by the asset |

---

## Complete Example

### Pipeline 1: Producer

```python
# pipelines/acme/dag.py

from polyris import DAG, task, Asset

# Define assets
raw = Asset("acme/raw", uri="s3://bucket/acme/raw/")
processed = Asset("acme/processed", uri="s3://bucket/acme/processed/")

with DAG(
    "acme-etl",
    schedule="@daily",
) as dag:
    
    @task.sfn(arn=..., outlets=[raw])
    def scrape():
        pass
    
    @task.sfn(arn=..., inlets=[raw], outlets=[processed])
    def process():
        pass
    
    s = scrape()
    process(s)
```

### Pipeline 2: Consumer

```python
# pipelines/feeds/dag.py

from polyris import DAG, task, Asset

# Reference same asset
processed = Asset("acme/processed")
feed = Asset("feeds/acme", uri="s3://bucket/feeds/acme/")

with DAG(
    "acme-feeds",
    schedule=[processed],  # Triggered when processed is ready
) as dag:
    
    @task.sfn(arn=..., outlets=[feed])
    def build_feeds():
        pass
    
    build_feeds()
```

### Flow

```
1. @daily trigger
       │
       ▼
┌──────────────┐
│ acme-etl  │
│              │
│  scrape ────▶ raw asset
│    │
│  process ───▶ processed asset ─────┐
│              │                      │
└──────────────┘                      │
                                      │
                      asset trigger   │
                                      │
                                      ▼
                              ┌───────────────┐
                              │ acme-feeds │
                              │               │
                              │  build_feeds  │
                              │               │
                              └───────────────┘
```

---

## Best Practices

1. **Use consistent naming**: `{group}/{type}` or `{domain}/{entity}`
2. **Add URIs**: Helps with lineage tracking and debugging
3. **Group related assets** with the `group` parameter
4. **Document inlets**: Even if not used for triggering
5. **Use date-based materialization**: a fact belongs to the partition of the run that produced it

---
