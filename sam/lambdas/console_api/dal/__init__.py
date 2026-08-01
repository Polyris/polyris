"""
Data Access Layer (DAL) for Console API.

Centralizes all DynamoDB operations into domain-specific repositories.
Import singletons from here:

    from dal import executions_repo, pipelines_repo, ...

Benefits:
- Schema changes: edit one file instead of 7+ route files
- Consistent patterns: ConsistentRead, conditional writes defined once
- Testability: mock repos instead of boto3.Table
- Discoverability: all DB operations in one place
"""

from dal.executions_repo import executions_repo
from dal.pipelines_repo import pipelines_repo
from dal.assets_repo import asset_events_repo, queued_events_repo
from dal.subscriptions_repo import dep_subscriptions_repo
from dal.task_events_repo import task_events_repo
from dal.circuit_breakers_repo import circuit_breakers_repo
from dal.backfills_repo import backfills_repo
from dal.api_tokens_repo import api_tokens_repo

__all__ = [
    'executions_repo',
    'pipelines_repo',
    'asset_events_repo',
    'queued_events_repo',
    'dep_subscriptions_repo',
    'task_events_repo',
    'circuit_breakers_repo',
    'backfills_repo',
    'api_tokens_repo',
]
