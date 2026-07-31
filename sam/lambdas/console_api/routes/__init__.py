"""
Console API Routes

Domain-specific route handlers. Each module handles one domain:
- pipelines_list: Pipeline listing and status (list, status, executions)
- pipelines_actions: Pipeline mutations (register, run, pause, restart)
- pipelines_info: Pipeline observability (metrics, DAG, logs)
- tasks: Task operations (retry, skip, fail, mark_success, stop, config)
- executions: Execution control (stop, pause, resume, extend)
- health: Health checks and metrics

(Team-tier route modules — backfill, notifications, slack, matrix, drift —
live under ee/team/ and are registered separately; see ADR #98.)

Each route module exports handler functions that:
- Take API Gateway event dict as input
- Return API Gateway response dict
- Use shared utilities from utils.py
- Use response helpers from response.py
"""

# Route modules - Phase 2 complete
from .executions import (
    get_all_runs, get_execution_children, get_execution_parent,
    stop_execution, pause_execution, resume_execution, extend_pause
)
from .tasks import (
    get_all_tasks, get_task_config, get_task_events,
    retry_task, skip_task, fail_task, mark_success, stop_task, restart_task
)
from .pipelines_list import (
    list_pipelines, get_pipeline_status, get_pipeline_executions
)
from .pipelines_actions import (
    run_pipeline, register_pipeline
)
from .pipelines_info import get_pipeline_dag
from .health import health_check, health_check_simple, get_metrics

__all__ = [
    # executions
    'get_all_runs',
    'get_execution_children',
    'get_execution_parent',
    'stop_execution',
    'pause_execution',
    'resume_execution',
    'extend_pause',
    # tasks
    'get_all_tasks',
    'get_task_config',
    'get_task_events',
    'retry_task',
    'skip_task',
    'fail_task',
    'mark_success',
    'stop_task',
    'restart_task',
    # pipelines
    'list_pipelines',
    'get_pipeline_status',
    'get_pipeline_executions',
    'run_pipeline',
    'register_pipeline',
    'get_pipeline_dag',
    # health
    'health_check',
    'health_check_simple',
    'get_metrics',
]
