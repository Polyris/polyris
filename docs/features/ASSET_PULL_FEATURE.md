# `wait_for` — pull-based cross-pipeline asset dependencies

> **⚠️ Experimental.** Assets (`Asset`, `outlets`, `inlets`, `wait_for`,
> asset-triggered `schedule`) are experimental — the API may change. Not
> recommended for production yet. Silence the runtime warning with
> `warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)`.
> <!-- EXPERIMENTAL-ASSETS: remove when assets graduate to stable. -->

`wait_for` makes a task pause until an upstream `Asset` is available (and
optionally fresh). Unlike push-based schedules (`schedule=[asset]`), pull-based
`wait_for` runs on the consumer's own cadence and blocks the individual task
until the asset condition holds.

## `wait_for` patterns

| Pattern                                             | Meaning                                                     |
| --------------------------------------------------- | ----------------------------------------------------------- |
| `wait_for=[asset_x]`                                | Latest materialization — no freshness check                 |
| `wait_for=[asset_x.within(hours=6)]`                | Latest event must be ≤ 6 hours old                          |
| `wait_for=[asset_x.within(days=1)]`                 | ≤ 1 day old                                                 |
| `wait_for=[asset_x.within(weeks=2)]`                | ≤ 2 weeks old                                               |
| `wait_for=[asset_x.within(days=1, hours=12)]`       | ≤ 36 hours old (combined units)                             |
| `wait_for=[asset_x.consecutive(days=7)]`            | 7 consecutive daily events required                         |
| `wait_for=[a, b]`                                   | AND — wait for both `a` and `b`                             |
| `wait_for=[a \| b]`                                 | OR — wait for either `a` or `b`                             |
| `wait_for=[a & b]`                                  | AND (explicit form; equivalent to the list form)            |
| `wait_for=[a.consecutive(days=7), b.within(hours=24)]` | Mixed: AND across a 7-day run and a fresh event          |
| `wait_for=[a.consecutive(days=7) \| manual_override]` | 7-day run OR manual override                              |

## Full example — producer + consumer

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
    # Latest inventory event, no freshness check.
    @task.sfn(arn="${process_arn}", wait_for=[inventory])
    def process():
        pass

    # Latest catalog event must be within 24 hours.
    @task.sfn(arn="${report_arn}", wait_for=[catalog.within(hours=24)])
    def report():
        pass
```

## In the Console

**Lineage view.** A task with `wait_for` appears as a consumer of the asset:

```
weekly-complete ──────▶ build_retailers_feed
        │
        ├─────────────▶ build_brands_feed
        │
        └─────────────▶ build_analytics_feed
```

**Task detail modal.** Asset dependencies show up as a labelled section, with
the freshness constraint as a badge:

```
Dependencies:        None (no task dependencies)
Asset Dependencies:  acme/weekly-complete (192h)
```

`192h` = 8 days (whatever was declared in `.within(...)`).

## How it works

Consumer side (the task with `wait_for`):

```
Consumer task starts
        │
        ▼
Registration helper
        │
        ├── Check task dependencies (existing)
        └── Check asset dependencies
            │
            ▼
        CheckAssets Lambda
            │
            ├── Query asset-events table (latest event per asset)
            ├── Check freshness (if `.within(...)` was specified)
            └── If not ready → save subscription to asset-subscriptions
        │
        ▼
If ready → signal wrapper → task continues
If not  → wait for signal (waitForTaskToken)
```

Producer side (a task with `outlets=[...]`):

```
Producer task completes with outlets
        │
        ▼
RunTask helper
        │
        ├── Emit EventBridge events (existing push path)
        ├── Record asset event in asset-events table
        └── NotifyAssetSubscribers Lambda
            │
            ├── Query subscribers for each outlet
            └── sendTaskSuccess to each waiting task
```

The two DynamoDB tables involved:

| Table                 | Purpose                                                    |
| --------------------- | ---------------------------------------------------------- |
| `asset-events`        | Every asset materialization (PK: `asset_name`, SK: `event_time`) |
| `asset-subscriptions` | Cross-pipeline triggers waiting on an asset (PK: `asset_name`, SK: `pipeline_name`) |

Everything provisions with `sam build && sam deploy` — no manual steps.
