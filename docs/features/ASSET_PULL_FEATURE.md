# Asset Pull Feature (wait_for)

> **⚠️ Experimental (v0.93.0).** Assets are an experimental feature — the API
> (`Asset`, `outlets`, `inlets`, `wait_for`, asset-triggered `schedule`) may change
> in a future release, and the open-source build has no visual asset console yet
> (inspect lineage with `polyris-output --graph`). Not recommended for production
> pipelines.
> Silence the runtime warning with
> `warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)`.
> <!-- EXPERIMENTAL-ASSETS: remove this banner when assets graduate to stable. -->


## Overview

This feature adds pull-based cross-pipeline asset dependencies via the `wait_for` parameter.

## DSL Usage

```python
from polyris import DAG, task, Asset

inventory = Asset("inventory", uri="s3://bucket/inventory/")
catalog = Asset("catalog")

# Producer pipeline
with DAG("producer", schedule="@daily") as pipeline:
    @task.sfn(arn="${extract_arn}", outlets=[inventory])
    def extract():
        pass

# Consumer pipeline - waits for inventory asset
with DAG("consumer", schedule="@hourly") as pipeline:
    @task.sfn(arn="${process_arn}", wait_for=[inventory])
    def process():
        pass
    
    # With freshness constraint
    @task.sfn(arn="${report_arn}", wait_for=[catalog.within(hours=24)])
    def report():
        pass
```

## Supported wait_for patterns

```python
# Latest asset (no freshness check)
wait_for=[asset_x]

# With freshness
wait_for=[asset_x.within(hours=6)]
wait_for=[asset_x.within(days=1)]
wait_for=[asset_x.within(weeks=2)]
wait_for=[asset_x.within(days=1, hours=12)]  # Combined

# Multiple assets
wait_for=[asset_a, asset_b]      # AND - wait for all
wait_for=[asset_a | asset_b]     # OR - wait for any

# Consecutive days (cross-pipeline weekly→daily)
wait_for=[daily_complete.consecutive(days=7)]  # Wait for 7 daily events

# Combined patterns
wait_for=[daily.consecutive(days=7), prices.within(hours=24)]  # AND (list)
wait_for=[daily.consecutive(days=7) | manual_override]          # OR
wait_for=[sales.consecutive(days=7) & inventory.consecutive(days=7)]  # AND (explicit)
```

## UI Display

### Lineage Graph

Tasks with `wait_for` appear as **consumers** on the Asset Lineage graph:

```
weekly-complete ──────▶ build_retailers_feed
        │
        ├─────────────▶ build_brands_feed  
        │
        └─────────────▶ build_analytics_feed
```

### Task Details

The Task Detail modal shows **Asset Dependencies** section:

```
Dependencies:        None (no task dependencies)
Asset Dependencies:  acme/weekly-complete (192h)
```

The freshness constraint (e.g., `192h` = 8 days) is shown as a badge.

## Infrastructure (SAM)

All infrastructure for the asset pull feature is defined in `sam/template.yaml`. The key resources:

**Lambdas** (defined as `AWS::Serverless::Function`):
- `CheckAssetsFunction` — checks asset freshness in `asset-events` table, saves subscriptions if not ready
- `NotifyAssetSubscribersFunction` — queries subscribers and sends `SendTaskSuccess` to waiting tasks

**SFN integration:**
- `RegistrationHelperSfn` — invokes `CheckAssetsFunction` in parallel with task dependency checks
- `RunTaskHelperSfn` — records asset events and invokes `NotifyAssetConsumersSfn` on task success
- `NotifyAssetConsumersSfn` (EXPRESS) — triggers asset-based pipelines

**DynamoDB tables used:**
- `asset-events` — stores asset materialization events (PK: `asset_name`, SK: `event_time`)
- `asset-subscriptions` — stores cross-pipeline asset triggers (PK: `asset_name`, SK: `pipeline_name`)

No manual infrastructure changes are needed — `sam build && sam deploy` handles everything.

## Architecture Flow

```
Consumer task starts
        │
        ▼
Registration helper
        │
        ├── Check task dependencies (existing)
        │
        └── Check asset dependencies (NEW)
            │
            ▼
        Check_Assets Lambda
            │
            ├── Query asset_events table
            │   └── Latest event for each asset
            │
            ├── Check freshness (if specified)
            │
            └── If not ready:
                └── Save subscription to asset-subscriptions
                    Key: asset:{asset_name}
        │
        ▼
If all ready → Signal wrapper → Continue
If not ready → Wait for signal (waitForTaskToken)

---

Producer task completes with outlets
        │
        ▼
Run_Task helper
        │
        ├── Emit EventBridge events (existing - for push)
        │
        ├── Record asset event in DynamoDB (NEW)
        │
        └── Notify_Asset_Subscribers Lambda (NEW)
            │
            ├── Query subscribers for each outlet
            │
            └── sendTaskSuccess to each waiting task
```

## Files Changed

### DSL
- `polyris/assets.py` - Added `AssetRef` class, `Asset.within()` method
- `polyris/task.py` - Added `wait_for` parameter
- `polyris/generators.py` - Added `_serialize_wait_for()` function

### SFN Templates
- `sam/sfn_templates/dependency_wrapper/sfn.tpl.json` - Pass wait_for to registration
- `sam/sfn_templates/helpers/registration/sfn.tpl.json` - Parallel check for task & asset deps
- `sam/sfn_templates/helpers/run_task/sfn.tpl.json` - Record asset events, notify subscribers

### Lambdas
- `sam/lambdas/check_assets/index.py` - NEW: Check asset freshness
- `sam/lambdas/notify_asset_subscribers/index.py` - NEW: Notify waiting tasks
