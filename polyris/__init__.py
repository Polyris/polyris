"""
polyris: Serverless data pipeline orchestration on AWS Step Functions

Define pipelines in Python, deploy to AWS Step Functions.

Example:
    from polyris import DAG, task, Asset, config
    from datetime import timedelta
    
    with DAG(
        dag_id="my-etl",
        schedule="@daily",
        default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
    ) as dag:
        
        @task.sfn(arn="arn:aws:states:us-east-1:111111111111:stateMachine:myorg-dev-extract")
        def extract():
            pass
        
        @task.sfn(arn="arn:aws:states:us-east-1:111111111111:stateMachine:myorg-dev-transform")
        def transform():
            pass
        
        extract() >> transform()

Local Testing:
    
    from polyris.local import dry_run, validate, run
    
    # Validate DAG structure
    validate(dag)
    
    # Dry run - show what would happen
    dry_run(dag)
    
    # Mock execution - simulate locally
    run(dag, mock=True)
    
    # LocalStack - run on AWS emulator
    run(dag, localstack=True)

Deploy Options:

    # polyris-deploy
    # Then: polyris-deploy
"""

__version__ = "0.93.0"

# Core classes
from .config import config
from .dag import DAG, Pipeline
from .task import Task, TaskInstance, TaskDecorator, task
from .task_group import TaskGroup, task_group
from .xcom import XComArg

# Assets
from .assets import Asset, AssetAll, AssetAny, AssetAlias, Metadata, Watcher, generate_watchers_config, ExperimentalWarning

# Schema (typed column system) — `types` module exposes type factories:
#   from polyris import types as t
#   t.bigint(), t.decimal(10, 2), t.array(t.string()), ...
from .schema import Column, Schema, PolyrisType
from . import schema as types  # noqa: F401  (re-export under shorter name)

# Steps
from .steps import (
    Step,
    Wait,
    Pass,
    Choice,
    Condition,
    HttpTask,
    Map,
    Sensor,
    ShortCircuit,
    LambdaTask,
    Succeed,
    DynamoDBTask,
    SNSTask,
    SQSTask,
    S3Task,
    GlueTask,
    AthenaTask,
    ECSTask,
    EventBridgeTask,
    BedrockTask,
)

# Constants
from .constants import (
    TriggerRuleLiteral,
    TaskTypeLiteral,
    SCHEDULE_PRESETS,
    TriggerRule,
    TaskStatus,
    TERMINAL_STATUSES,
    SUCCESS_STATUSES,
    FAILURE_STATUSES,
    ACTIVE_STATUSES,
    WAITING_STATUSES,
    COUNTDOWN_STATUSES,
    STOPPABLE_STATUSES,
)

# Helpers
from .helpers import (
    chain,
    cross_downstream,
    Label,
    dag,
)

# Generators
from .generators import (
    generate_step_function_json,
    generate_dag_json,
    generate_mermaid,
    generate_eventbridge_schedule,
    generate_assets_json,
    generate_all_assets,
)


# Resolver
from .resolver import (
    ARNResolver,
    set_resolver,
    get_resolver,
)

__all__ = [
    # Core
    'config',
    'DAG',
    'Pipeline',
    'Task',
    'task',  # TaskDecorator instance with .sfn(), .lambda_(), .glue(), etc.
    'TaskDecorator',
    'TaskGroup',
    'task_group',
    'TaskInstance',
    'XComArg',
    
    # Assets
    'Asset',
    'AssetAll',
    'AssetAny',
    'AssetAlias',
    'Metadata',
    'Watcher',
    'generate_watchers_config',

    # Schema (Column class + types factory module)
    'Column',
    'Schema',
    'PolyrisType',
    'types',

    # Steps
    'Step',
    'Wait',
    'Pass',
    'Choice',
    'Condition',
    'HttpTask',
    'Map',
    'Sensor',
    'ShortCircuit',
    'LambdaTask',
    'Succeed',
    'DynamoDBTask',
    'SNSTask',
    'SQSTask',
    'S3Task',
    'GlueTask',
    'AthenaTask',
    'ECSTask',
    'EventBridgeTask',
    'BedrockTask',
    
    # Type literals
    'TriggerRuleLiteral',
    'TaskTypeLiteral',
    
    # Decorators
    'dag',
    
    # Helper functions
    'chain',
    'cross_downstream',
    'Label',
    'TriggerRule',
    
    # Generators
    'generate_step_function_json',
    'generate_dag_json',
    'generate_mermaid',
    'generate_eventbridge_schedule',
    'generate_assets_json',
    'generate_all_assets',
    
    # CLI
    
    # Resolver
    'ARNResolver',
    'set_resolver',
    'get_resolver',
    
    # Constants
    'SCHEDULE_PRESETS',
    'TaskStatus',
    'TERMINAL_STATUSES',
    'SUCCESS_STATUSES', 
    'FAILURE_STATUSES',
    'ACTIVE_STATUSES',
    'WAITING_STATUSES',
    'COUNTDOWN_STATUSES',
    'STOPPABLE_STATUSES',
]
