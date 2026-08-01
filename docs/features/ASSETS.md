# Asset-Based Orchestration

> **⚠️ Experimental (v0.93.0).** Assets are an experimental feature — the API
> (`Asset`, `outlets`, `inlets`, `wait_for`, asset-triggered `schedule`) may change
> in a future release, and the open-source build has no visual asset console yet
> (inspect lineage with `polyris-output --graph`). Not recommended for production
> pipelines.
> Silence the runtime warning with
> `warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)`.
> <!-- EXPERIMENTAL-ASSETS: remove this banner when assets graduate to stable. -->


## Overview

polyris supports asset-based orchestration, enabling cross-pipeline dependencies without hardcoded references.

**Key concepts:**
- **Asset**: A data artifact (file, table, dataset)
- **Producer**: Task that creates an asset (`outlets`)
- **Consumer**: DAG that triggers when asset is ready (`schedule`)

> **Open-core note (ADR #105):** the asset *engine* described here — declaring
> assets, producers, and consumers in pipelines — is part of the open-source
> build. The asset *console* (the `/assets` UI and its read API) is not included
> in this build.

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

# Asset with group (for UI organization)
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

### Schema Declaration

Declare a column schema directly in the asset definition. The schema is
displayed on the Asset Detail Page (Schema tab) and is the source of truth
for downstream features (drift detection, Glue sync, DDL generation —
on the roadmap).

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
| `partition_key`  | `False` | Column partitions the asset (shown on Partitions tab).    |
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

**Schema conflict detection:** when the same asset is declared in multiple
pipelines with different schemas (e.g. a producer pipeline declares 8 columns,
a consumer references the asset and declares 3), the backend keeps the richer
schema (more columns) and emits a warning to CloudWatch Logs so the divergent
declaration can be reconciled.

### Glue Catalog Reference

Link an asset to a table in **AWS Glue Data Catalog**. The Glue Data Catalog
has a three-level structure that maps to Polyris fields as follows:

| AWS Glue concept              | Maps to                          | Default                    |
|-------------------------------|----------------------------------|----------------------------|
| **Region**                    | `glue_region`                    | Lambda's own region        |
| **Glue Data Catalog ID**      | `glue_catalog`                   | Lambda's own AWS account   |
| **Database**                  | first half of `glue_table`       | (no default — be explicit) |
| **Table**                     | second half of `glue_table`      | (no default — be explicit) |

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

# Cross-account (Lambda's account ≠ catalog account)
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

If both `glue_table` and `schema` are set, the Schema tab compares them
and shows drift; see "Glue Catalog Sync" below.

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

Assign an owner to an asset for visibility in the UI.

```python
orders = Asset(
    name="retail/orders",
    owner="data-team",
)
```

### Glue Catalog Sync (on-demand)

When an asset declares both `schema=[...]` and `glue_table="db.table"`, the
Asset Detail Page Schema tab fetches the actual schema from AWS Glue
Catalog and shows a side-by-side diff against the declared schema.

The fetch is **on-demand**: it fires when you open the Schema tab, not on
a schedule. There is no cron, no separate Lambda, no extra DDB table.
Result is cached in the browser for 5 minutes; a refresh button forces a
fresh call.

The diff surfaces three categories:

| Category | Meaning |
|---|---|
| Declared but missing from Glue | Column declared in code; Glue table does not have it. Either the table needs `ALTER TABLE ADD COLUMN`, or the declaration is stale. |
| Present in Glue but not declared | Column exists in Glue (added by another tool, manual `ALTER`, or a different ETL); not declared in code. |
| Type mismatches | Same column name on both sides, but different types (e.g. declared `decimal(10,2)`, Glue `decimal(12,4)`). |

If Glue is unreachable (IAM, VPC, throttling, table-not-found) the panel
shows a structured error with the Glue error code and message; the declared
schema is still rendered alongside.

**Constraint fields** (`nullable`, `primary_key`, `partition_key`, `unique`)
are polyris-only metadata and are not included in the diff — Glue Catalog
does not represent them, so they cannot drift.

**Cross-account Glue access — IAM checklist:**

The default SAM template grants `glue:GetTable` on `Resource: "*"`, which
covers same-account access in any region. **Cross-account requires an
explicit ARN expansion** in the Console API Lambda's IAM policy:

```yaml
# sam/template.yaml — extend for cross-account
- Effect: Allow
  Action: ["glue:GetTable", "glue:GetDatabase"]
  Resource:
    - !Sub "arn:aws:glue:*:${AWS::AccountId}:catalog"           # local
    - !Sub "arn:aws:glue:*:${AWS::AccountId}:database/*"
    - !Sub "arn:aws:glue:*:${AWS::AccountId}:table/*/*"
    - "arn:aws:glue:*:222222222222:catalog"                     # peer account
    - "arn:aws:glue:*:222222222222:database/*"
    - "arn:aws:glue:*:222222222222:table/*/*"
```

The catalog account must **also** grant access — either via a Glue
resource policy (IAM-only) or via Lake Formation (if the catalog is
managed by LF). See [Granting cross-account access — AWS Glue docs](https://docs.aws.amazon.com/glue/latest/dg/cross-account-access.html).

**Lake Formation interaction:** If the target catalog is managed by Lake
Formation, IAM permissions alone are insufficient — Lake Formation's
GRANT/REVOKE permissions model gates access independently. Without an
LF GRANT to the Console API Lambda's role, drift detection returns
`AccessDeniedException` even when IAM is correct. Check by running:

```bash
aws glue get-table --database-name <db> --name <table> \
  --query 'Table.IsRegisteredWithLakeFormation'
```

If `true` — coordinate with the catalog owner to add an LF data permission.

**Cross-region:** `glue_region` is independent of `glue_catalog` — the
two fields combine to address any (region, account) tuple. AWS Glue Data
Catalog itself is per-region; there is no automatic cross-region
replication. Polyris does not provide multi-region drift comparison —
each asset points at exactly one (region, account, database, table).

ADR #43 covers the on-demand fetch design. ADR #45 covers the
cross-account / cross-region field design.

---

### Constructing Assets from Existing Schema Sources

When the schema already lives in another system (a Parquet file, a
pydantic model, a Glue Catalog table) you can skip the manual `Column`
list and use a `from_*` constructor. ADR #44 describes the design.

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

Requires `pip install 'polyris[pyarrow]'`. The bridge to all six
formats above goes through pyarrow as a hub — one optional dependency,
many integrations.

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

Requires `pip install 'polyris[pydantic]'`. Pydantic field
descriptions, defaults, and `Optional[...]` markers all carry over.

**Naming note.** If `name=` is omitted, `from_pydantic` falls back to
the model's class name (`Order` → `name="Order"`). This is convenient
for quick prototypes but produces a CamelCase asset name with no group
— in production you almost always want an explicit `name="domain/asset"`
so the asset shows up grouped (e.g. under `retail/`) on the assets page.

**From an existing Glue Catalog table (deploy-time fetch):**

```python
from polyris import Asset

orders = Asset.from_glue_table(
    "analytics.orders",
    name="retail/orders",
    owner="data-team",
)
```

The Glue `glue_table` field is automatically set on the resulting
asset, so the runtime drift check (Phase 2, ADR #43) works on it
without further configuration. Cross-account: pass
`catalog_id="<account-id>"`.

This is different from the runtime drift detection in the UI: this
constructor runs on your local machine at deploy time as a one-shot
schema import. The two share the same parser, but the runtime path
hits the API; this path hits Glue directly via your local credentials.

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

**What happens:**
1. Task completes successfully
2. `run_task_helper` records asset event in DynamoDB
3. `notify_asset_consumers` SFN is invoked to trigger subscribed pipelines

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

**What happens:**
1. Task with `outlets` completes successfully
2. `run_task_helper` invokes `notify_asset_consumers` SFN (async)
3. SFN checks if all required assets are ready
4. If ready → starts the consumer DAG (via StartExecution)

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
1. Each asset event is recorded in DynamoDB with atomic counter
2. `notify_asset_consumers` SFN checks: "Are all required assets present?"
3. Only triggers DAG when ALL assets are ready for the same date
4. Uses distributed lock to prevent duplicate triggers

**Example timeline:**
```
10:00 - asset_a arrives → counter=1, wait
10:15 - asset_b arrives → counter=2, wait
10:30 - asset_c arrives → counter=3 → all present → TRIGGER DAG
```

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

## Asset Events

### Automatic (from outlets)

When a task with outlets completes:
```json
{
  "source": "pipeline.assets",
  "detail-type": "Asset.Materialized",
  "detail": {
    "asset_name": "processed/acme",
    "asset_uri": "s3://bucket/processed/acme/",
    "date": "2026-01-12",
    "source_pipeline": "acme-daily",
    "source_task": "process_data",
    "metadata": {}
  }
}
```

### Manual Trigger

Via API:
```bash
POST /api/asset/processed%2Facme/trigger
Content-Type: application/json

{
  "date": "2026-01-12",
  "metadata": {"source": "manual", "reason": "backfill"}
}
```

Via UI:
1. Go to Assets view
2. Find asset
3. Click "Trigger"
4. Enter date

---

## Queue Management

### View Queued Assets

Assets waiting for AND conditions:

```bash
GET /api/assets/queued
```

### Skip Asset in Queue

Skip waiting for a specific asset:

```bash
POST /api/assets/skip-in-queue
{
  "dag_id": "feeds",
  "asset_name": "processed/acme",
  "date": "2026-01-12"
}
```

### Clear Queue

Clear all queued assets for a DAG (today's queue):

```bash
POST /api/assets/clear-queue
{
  "dag_id": "feeds",
  "date": "2026-01-12"
}
```

---

## Asset Lineage Graph

The UI shows a visual lineage graph:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Task A    │────▶│  Asset X    │────▶│   DAG B     │
│  (producer) │     │             │     │ (consumer)  │
└─────────────┘     └─────────────┘     └─────────────┘
```

Access via: **📦 Assets** tab → **Lineage** view

### Relationships Shown

| Source | Relationship | Shows on Lineage |
|--------|--------------|------------------|
| `outlets` | Task produces asset | ✅ Task → Asset |
| `inlets` | Task consumes asset | ✅ Asset → Task |
| `wait_for` | Task waits for asset freshness | ✅ Asset → Task |
| `schedule=[asset]` | DAG triggered by asset | ✅ Asset → DAG |

**Note:** Both `inlets` and `wait_for` show the task as a consumer of the asset on the lineage graph. The difference is:
- `inlets` — documentation only (no runtime effect)
- `wait_for` — runtime dependency (task waits for fresh asset)

### Timeout for Cross-Pipeline Dependencies

When a task waits for assets from another pipeline, you may want a longer timeout than the default 24h:

```python
from datetime import timedelta

@task.sfn(
    arn=...,
    wait_for=[weekly_report],
    orchestration_timeout=timedelta(days=3),  # Wait up to 3 days for deps
    execution_timeout=timedelta(hours=2),      # Task itself runs in 2h
)
def depends_on_weekly():
    pass
```

If `orchestration_timeout` is not set, it defaults to `execution_timeout` (24h).

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
3. **Group related assets**: Use `group` parameter for UI organization
4. **Document inlets**: Even if not used for triggering
5. **Monitor queues**: Check queued assets in UI
6. **Use date-based materialization**: Assets are keyed by date

---

## Matrix view (v0.76.0)

The Matrix tab in the Assets view shows assets × dates as a 2D grid —
useful for answering *"what's broken, and when did it start?"* at a
glance across the whole platform.

**How to read it.** Rows are assets (grouped by `group`); columns
are dates ending with today. Each cell carries the asset's status on
that date:

  - 🟢 Materialized — producer task succeeded that day
  - 🔴 Failed — producer task failed (hover for the error message)
  - 🟡 Running — producer task is currently running for that date
  - 🟠 Queued — no producer record yet, but at least one consumer DAG awaits this asset
  - ⚪ Missing — no producer record and no consumer waiting

Hover any cell for a tooltip showing the timestamp, source task and DAG,
and — for failed cells — the actual error message. No need to click
through to task detail to see *why* something failed.

The 🟠 Queued cell is the one to scan for during incidents — it shows
which assets are blocking downstream pipelines right now. Operators
can click queued (or missing, or failed) cells to backfill that
(asset, date) directly.

**Click-to-backfill.** Missing, failed, and queued cells are
actionable — clicking opens the Backfill modal pre-filled with that
asset and date. Keyboard equivalent: focus the cell and press Enter
or Space.

**Date range.** Default range adapts to viewport: 14 days on desktop,
7 days on tablet (≤768 px), 5 days on mobile (≤480 px). Adjust via the
Range dropdown — supported values are 5, 7, 14, 30, and 60 days.
Backend caps requests at 60 days.

**Partition granularity (v0.77, ADR #50).** Assets can partition at
different cadences. Each asset declares one granularity (daily by
default); Glue-backed assets get an advisory auto-detected suggestion
from the catalog's partition keys, and a drift badge flags when the
declared granularity disagrees with what Glue reports. The matrix
shows one granularity at a time (a filter), rather than mixing
cadences in a single grid — multi-dimensional and dynamic partitions,
and a unified multi-granularity view, are explicit non-goals of ADR
#50. Backfill is granularity-aware: the inferred/declared granularity
determines how the requested range expands into partitions.

**Refresh cadence.** The matrix polls every 30 seconds (same cadence
as the Catalog tab). The pause-when-tab-hidden behavior is automatic
via React Query; nothing fetches when you switch tabs.

**Regenerate semantics.** When a date is backfilled, `pipeline-tokens`
ends up with multiple records for the same (task, date). The matrix
picks the latest reality: a currently-running backfill wins; otherwise
the record with the newest `finished_at`. So an originally-successful
day that fails on backfill correctly turns 🔴 — the cell reflects the
latest attempt, not the first.

**Where the data comes from.** The matrix is a *derived view* — nothing
is stored specifically for it. Cell colors are computed at read time
from:

  - `pipeline-tokens.date-pipeline-index` for task status by date
  - `pipeline_registry` for task→outlet mapping and consumer
    `asset_schedule`

This is intentional: see ADR #49 for the design rationale (short
version: task status is already canonical in `pipeline-tokens`, so
storing it again as asset events would duplicate state and cost real
money at scale).

**View assets and static tables.** Athena views (`extra={"type":
"athena_view"}`) and static tables are excluded from the matrix by
default because they don't materialize. They remain visible in the
Catalog tab. A future release may add a toggle to surface them with a
fixed "external" indicator.

**What you won't see.** Staleness is a "now" concept — the matrix
shows historical truth at each date, so an asset materialized two days
ago appears 🟢 on that day even if today's freshness check would mark
it stale. Use the Catalog tab for current staleness.
