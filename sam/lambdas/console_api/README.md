# Console API

Pipeline management API for polyris orchestration system.

## Structure

```
console_api/
├── main.py              # Lambda entry point, request routing
├── config.py            # AWS clients, environment variables
├── utils.py             # Shared utility functions
├── response.py          # CORS, HTML response helpers
├── dal/                 # Data Access Layer (DynamoDB repositories)
│   ├── __init__.py      # Exports 8 singleton repo instances
│   ├── executions_repo.py   # pipeline-executions table
│   ├── pipelines_repo.py    # pipeline-registry table
│   ├── assets_repo.py       # asset-registry, asset-events, queued-asset-events
│   ├── subscriptions_repo.py # dep-subscriptions, asset-subscriptions
│   └── task_events_repo.py  # task-events table
├── routes/              # Domain-specific route handlers
│   ├── __init__.py      # Route exports
│   ├── pipelines_list.py    # Pipeline listing & status
│   ├── pipelines_actions.py # Pipeline mutations (run, register, pause, restart)
│   ├── pipelines_info.py    # Pipeline observability (metrics, DAG, logs)
│   ├── tasks.py         # Task operations
│   ├── assets.py        # Asset management
│   ├── backfill.py      # Backfill operations
│   ├── executions.py    # Execution control
│   ├── slack.py         # Slack actions
│   └── notifications.py # Notifications
└── tests/               # Unit tests
    ├── conftest.py      # Shared fixtures
    └── test_utils.py    # Utils tests
```

## API Endpoints (49 total)

See [API.md](../../../docs/operations/API.md) for the complete endpoint reference with request/response examples.

## Data Access Layer (DAL)

All DynamoDB operations are centralized in `dal/` repositories. Routes import singleton instances:

```python
from dal import executions_repo, pipelines_repo

# Single-item ops
item = executions_repo.get(execution_name)
pipelines_repo.put(item)
executions_repo.update(execution_name, 'SET #s = :status', expr_values={':status': 'done'}, expr_names={'#s': 'status'})

# Query ops
items = executions_repo.query_by_date(date, max_items=100, filter_expr=Attr('status').eq('failed'))
items = executions_repo.query_by_pipeline_execution(pipeline_execution)

# Raw query (when you need boto3 response with LastEvaluatedKey, Count, etc.)
response = executions_repo.query_by_date_raw(**query_params)

# Scan
items = executions_repo.scan(max_items=500)
all_pipelines = pipelines_repo.list_all()
```

8 repos cover 8 DynamoDB tables. Schema changes only need edits in one repo file.

**Important:** Never bypass repos with `repo.table.get_item(...)` — all 55 direct table calls were migrated to repo methods in v69.3.

## Running Tests

```bash
cd sam/lambdas/console_api
pip install pytest pytest-mock pytest-env
pytest tests/ -v
```

## SAM Configuration

The Lambda is defined in `sam/template.yaml` as `ConsoleApiFunction` (`AWS::Serverless::Function`). All environment variables (table names, SFN ARNs) are injected via CloudFormation references.

## History

Refactored from monolithic `handlers.py` (3,836 lines) to modular structure.
See `HANDLERS_REFACTORING_PLAN.md` for full migration details.

DAL layer added to centralize ~65 scattered `dynamodb.Table()` calls into 8 repository classes.
All 55 route-level DDB bypasses migrated to repo methods in v69.3 — routes no longer access `boto3.Table` directly.

`pipelines.py` (1,246 lines) split into three focused modules:
`pipelines_list.py`, `pipelines_actions.py`, `pipelines_info.py`.
