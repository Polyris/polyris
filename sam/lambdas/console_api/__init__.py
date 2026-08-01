"""
Console API Package

Pipeline Console API for managing data pipelines:
- List/monitor pipelines
- Retry/Skip/Fail tasks
- Configure task settings
- Asset management
- Execution control

Refactored structure:
- main.py: Entry point with handler() and routing
- config.py: AWS clients and environment variables
- utils.py: Shared utility functions
- response.py: CORS and HTML response helpers
- routes/: Domain-specific route handlers
"""

from .config import (
    dynamodb, sfn, logs, cloudwatch, s3,
    TABLE_NAME, PIPELINES_TABLE, SUBSCRIPTIONS_TABLE,
    ASSET_EVENTS_TABLE, 
    QUEUED_EVENTS_TABLE, TASK_EVENTS_TABLE, RESULTS_BUCKET,
    EXECUTION_NAME_PATTERN
)

from .utils import (
    is_execution_name, retrieve_result, record_manual_decision,
    parse_wait_before, safe_int, safe_param_int,
    scan_all, query_all, stop_task_executions
)

from .response import cors_response, html_response

# Main Lambda handler - now in main.py
from .main import handler

__all__ = [
    # Config
    'dynamodb', 'sfn', 'logs', 'cloudwatch', 's3',
    'TABLE_NAME', 'PIPELINES_TABLE', 'SUBSCRIPTIONS_TABLE',
    'ASSET_EVENTS_TABLE',
    'QUEUED_EVENTS_TABLE', 'TASK_EVENTS_TABLE', 'RESULTS_BUCKET',
    'EXECUTION_NAME_PATTERN',
    # Utils
    'is_execution_name', 'retrieve_result', 'record_manual_decision',
    'parse_wait_before', 'safe_int', 'safe_param_int',
    'scan_all', 'query_all', 'stop_task_executions',
    # Response
    'cors_response', 'html_response',
    # Handler
    'handler'
]
