"""
Step classes for SFN-DSL.

This module contains the base Step class and all Step subclasses:
- Wait, Pass, Choice, HttpTask, Map, Sensor, ShortCircuit, LambdaTask, Succeed
- AWS Service integrations: DynamoDBTask, SNSTask, SQSTask, S3Task, GlueTask, 
  AthenaTask, ECSTask, EventBridgeTask, BedrockTask
"""

from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING, Union
from dataclasses import dataclass, field

from .context import get_current_dag

if TYPE_CHECKING:
    from .task import Task
    from .dag import DAG


# ============================================
# Step Base Class - All pipeline steps inherit from this
# ============================================

@dataclass
class Step:
    """
    Base class for all pipeline steps.
    Enables mixing Task, Wait, Choice, Pass, etc. in the same DAG.
    """
    step_id: str
    step_type: str = "step"
    
    # Internal
    # Runtime truth: TaskInstance >> Step appends Task objects here too.
    dependencies: List[Union['Step', 'Task']] = field(default_factory=list)
    _dag: Optional['DAG'] = field(default=None, repr=False)
    
    @property
    def node_id(self) -> str:
        """Unique identifier for this node in the DAG graph."""
        return self.step_id
    
    def __rshift__(self, other):
        """step >> other (supports Step, Task, TaskInstance, TaskGroup)"""
        # Import here to avoid circular imports
        from .task import Task
        from .task_group import TaskGroup
        
        if isinstance(other, TaskGroup):
            # Step >> TaskGroup: connect step to all roots of the group
            for root in other.roots:
                if self not in root.dependencies:
                    root.dependencies.append(self)
            return other
        elif isinstance(other, (Step, Task)):
            if self not in other.dependencies:
                other.dependencies.append(self)
            return other
        elif hasattr(other, 'task') and hasattr(other.task, 'dependencies'):
            # TaskInstance - access underlying Task
            if self not in other.task.dependencies:
                other.task.dependencies.append(self)
            return other
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, (Step, Task)) and self not in t.dependencies:
                    t.dependencies.append(self)
                elif hasattr(t, 'task') and self not in t.task.dependencies:
                    t.task.dependencies.append(self)
            return other
        return NotImplemented
    
    def __lshift__(self, other):
        """step << other"""
        from .task import Task
        
        if isinstance(other, (Step, Task)):
            if other not in self.dependencies:
                self.dependencies.append(other)
            return self
        elif isinstance(other, list):
            for t in other:
                if isinstance(t, (Step, Task)) and t not in self.dependencies:
                    self.dependencies.append(t)
            return self
        return NotImplemented
    
    def __rrshift__(self, other):
        """[step1, step2] >> step"""
        from .task import Task
        
        if isinstance(other, list):
            for t in other:
                if isinstance(t, (Step, Task)) and t not in self.dependencies:
                    self.dependencies.append(t)
            return self
        return NotImplemented


# ============================================
# Wait Step - Delay before next step
# ============================================

@dataclass
class Wait(Step):
    """
    Wait step - adds delay before continuing.

    
    Example:
        wait_20s = Wait(seconds=20)
        wait_until_9am = Wait(timestamp="2024-01-01T09:00:00Z")
        wait_dynamic = Wait(timestamp_path="$.scheduled_time")
        
        task_a >> wait_20s >> task_b
    """
    step_id: str = ""
    step_type: str = "wait"
    
    # Wait duration (use one of these)
    seconds: Optional[int] = None
    timestamp: Optional[str] = None  # ISO 8601 format
    timestamp_path: Optional[str] = None  # JSONPath to timestamp in input
    
    def __post_init__(self):
        if not (self.seconds or self.timestamp or self.timestamp_path):
            raise ValueError(
                "Wait(...) requires exactly one of: seconds, timestamp, "
                "timestamp_path. A Wait state with none of these generates "
                "{'Type': 'Wait'} with no duration — locally valid-looking "
                "ASL that AWS Step Functions rejects at deploy time."
            )
        if not self.step_id:
            if self.seconds:
                self.step_id = f"Wait_{self.seconds}s"
            elif self.timestamp:
                self.step_id = f"Wait_until_{self.timestamp.replace(':', '-').replace('T', '_')}"
            else:
                self.step_id = "Wait"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Pass Step - Transform/compute data
# ============================================

@dataclass
class Pass(Step):
    """
    Pass step - transform data, compute variables.

    
    Example:
        prepare = Pass(
            step_id="prepare_params",
            output={
                "s3_path": "{% 's3://bucket/' & $.current_date & '/' %}",
                "table_name": "{% 'sales_' & $replace($.current_date, '-', '') %}"
            }
        )
        
        prepare >> task_a
    """
    step_id: str = ""
    step_type: str = "pass"
    
    # Output transformation (JSONata expressions)
    output: Optional[Dict[str, Any]] = None
    
    # Or use result_path to add to existing input
    result: Any = None
    result_path: Optional[str] = None  # Where to put result in state
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = "Pass"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Choice Step - Conditional branching
# ============================================

@dataclass 
class Choice(Step):
    """
    Choice step - conditional branching.

    
    Example:
        check_day = Choice(
            step_id="check_weekday",
            choices=[
                (Condition.string_equals("$.day_type", "weekend"), weekend_flow),
                (Condition.number_greater_than("$.records", 1000), big_data_flow),
            ],
            default=normal_flow
        )
    """
    step_id: str = ""
    step_type: str = "choice"
    
    # List of (condition, next_step) tuples
    choices: List[Tuple[str, 'Step']] = field(default_factory=list)
    
    # Default step if no condition matches
    default: Optional['Step'] = None
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = "Choice"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Condition helpers for Choice
# ============================================

class Condition:
    """Helper class to build Step Functions conditions."""
    
    @staticmethod
    def string_equals(variable: str, value: str) -> str:
        """Check if string equals value."""
        return f'{{% {variable} = "{value}" %}}'
    
    @staticmethod
    def string_matches(variable: str, pattern: str) -> str:
        """Check if string matches pattern."""
        return f'{{% $match({variable}, /{pattern}/) %}}'
    
    @staticmethod
    def number_equals(variable: str, value: float) -> str:
        """Check if number equals value."""
        return f'{{% {variable} = {value} %}}'
    
    @staticmethod
    def number_greater_than(variable: str, value: float) -> str:
        """Check if number is greater than value."""
        return f'{{% {variable} > {value} %}}'
    
    @staticmethod
    def number_less_than(variable: str, value: float) -> str:
        """Check if number is less than value."""
        return f'{{% {variable} < {value} %}}'
    
    @staticmethod
    def boolean_equals(variable: str, value: bool) -> str:
        """Check if boolean equals value."""
        return f'{{% {variable} = {"true" if value else "false"} %}}'
    
    @staticmethod
    def is_present(variable: str) -> str:
        """Check if variable exists."""
        return f'{{% $exists({variable}) %}}'
    
    @staticmethod
    def is_null(variable: str) -> str:
        """Check if variable is null."""
        return f'{{% {variable} = null %}}'
    
    @staticmethod 
    def jsonata(expression: str) -> str:
        """Custom JSONata expression."""
        return f'{{% {expression} %}}'


# ============================================
# HttpTask - Call HTTP API without Step Function
# ============================================

@dataclass
class HttpTask(Step):
    """
    HTTP API call step.

    
    Example:
        notify = HttpTask(
            step_id="notify_slack",
            url="https://hooks.slack.com/services/...",
            method="POST",
            body={"text": "Pipeline completed!"},
            headers={"Content-Type": "application/json"}
        )
    """
    step_id: str = ""
    step_type: str = "http"
    
    url: str = ""
    method: str = "GET"  # GET, POST, PUT, DELETE
    headers: Dict[str, str] = field(default_factory=dict)
    body: Any = None
    
    # Authentication
    connection_arn: Optional[str] = None  # EventBridge connection ARN for auth
    
    def __post_init__(self):
        if not self.url:
            raise ValueError(
                "HttpTask(...) requires 'url'. Without it, the generated "
                "state has an empty ApiEndpoint, which passes local "
                "validation but fails when the state actually tries to "
                "make the HTTP call."
            )
        if not self.step_id:
            self.step_id = "HttpTask"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Map Step - Dynamic Task Mapping
# ============================================

@dataclass
class Map(Step):
    """
    Map step - iterate over array, process each item.

    Example:
        process_all = Map(
            step_id="process_items",
            items_path="$.items",  # JSONPath to array
            iterator=process_task,  # Task to run for each item
            max_concurrency=10,     # Limit parallel executions
        )
    """
    step_id: str = ""
    step_type: str = "map"
    
    # Array to iterate over
    items_path: str = "$.items"  # JSONPath to array in input
    items: Optional[List[Any]] = None  # Or static list
    
    # Task/Step to run for each item
    iterator: Any = None  # Task or Step
    
    max_concurrency: int = 0  # 0 = unlimited
    
    # Tolerate failures
    tolerated_failure_percentage: Optional[float] = None
    tolerated_failure_count: Optional[int] = None
    
    # Result handling
    result_path: str = "$.results"
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = "Map"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Sensor Step - Wait for external condition
# ============================================

@dataclass
class Sensor(Step):
    """
    Sensor step - wait for external condition.

    
    Example:
        # Wait for S3 file
        wait_for_file = Sensor(
            step_id="wait_for_data",
            sensor_type="s3",
            bucket="my-bucket",
            key="data/{{ ds }}/input.csv",
            poke_interval=60,
            timeout=3600
        )
        
        # Wait for external task
        wait_for_upstream = Sensor(
            step_id="wait_upstream",
            sensor_type="external_task",
            external_dag_id="upstream-dag",
            external_task_id="final_task"
        )
    """
    step_id: str = ""
    step_type: str = "sensor"
    
    sensor_type: str = "s3"  # s3, external_task, time, custom
    
    # S3 sensor params
    bucket: Optional[str] = None
    key: Optional[str] = None
    
    # External task sensor params
    external_dag_id: Optional[str] = None
    external_task_id: Optional[str] = None
    
    # Polling params
    poke_interval: int = 60  # seconds between checks
    timeout: int = 3600  # max wait time
    mode: str = "poke"  # poke or reschedule
    
    # Custom check (Lambda ARN or Step Function)
    check_arn: Optional[str] = None
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = f"Sensor_{self.sensor_type}"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# ShortCircuit - Conditional pipeline stop
# ============================================

@dataclass
class ShortCircuit(Step):
    """
    ShortCircuit step - conditionally skip downstream tasks.

    
    Example:
        # Skip rest of pipeline if condition is false
        check = ShortCircuit(
            step_id="check_data_exists",
            condition="{% $count($.records) > 0 %}",
        )
        
        check >> process >> load  # process and load skipped if condition false
    """
    step_id: str = ""
    step_type: str = "short_circuit"
    
    # JSONata condition - if false, downstream is skipped
    condition: str = "{% true %}"
    
    # What to do when condition is false
    skip_downstream: bool = True
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = "ShortCircuit"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Lambda Task - Direct Lambda invocation
# ============================================

@dataclass
class LambdaTask(Step):
    """
    Direct Lambda invocation (without nested Step Function).

    
    Example:
        validate = LambdaTask(
            step_id="validate_data",
            function_arn="arn:aws:lambda:us-east-1:123:function:validate",
            payload={"bucket": "{% $.bucket %}", "key": "{% $.key %}"}
        )
    """
    step_id: str = ""
    step_type: str = "lambda"
    
    function_arn: str = ""
    payload: Optional[Dict[str, Any]] = None
    
    # Retry
    retries: int = 0
    retry_interval: int = 1
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = "LambdaTask"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# Succeed Step - Early successful exit
# ============================================

@dataclass
class Succeed(Step):
    """
    Succeed step - end pipeline successfully.
    Useful for conditional early exit or short-circuit patterns.
    
    Example:
        # Early exit if no data
        check = Choice(
            step_id="check_data",
            choices=[
                (Condition.number_equals("$.count", 0), Succeed(step_id="no_data_exit"))
            ],
            default=process_task
        )
    """
    step_id: str = ""
    step_type: str = "succeed"
    
    # Optional output
    output: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if not self.step_id:
            self.step_id = "Succeed"
        
        # Auto-register with current DAG
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


# ============================================
# AWS SERVICE INTEGRATIONS (Native SDK)
# ============================================

@dataclass
class DynamoDBTask(Step):
    """
    Direct DynamoDB operation (no Lambda needed).
    
    Example:
        # Get item
        get_config = DynamoDBTask(
            step_id="get_config",
            operation="get_item",
            table_name="config-table",
            key={"pk": {"S": "{% $.config_key %}"}}
        )
        
        # Put item
        save_result = DynamoDBTask(
            step_id="save_result", 
            operation="put_item",
            table_name="results",
            item={"pk": {"S": "{% $.id %}"}, "data": {"S": "{% $.result %}"}}
        )
        
        # Query
        get_items = DynamoDBTask(
            step_id="get_items",
            operation="query",
            table_name="items",
            key_condition="pk = :pk",
            expression_values={":pk": {"S": "{% $.partition_key %}"}}
        )
    """
    step_id: str = ""
    step_type: str = "dynamodb"
    
    operation: str = "get_item"  # get_item, put_item, update_item, delete_item, query, scan
    table_name: str = ""
    
    # For get_item, delete_item
    key: Optional[Dict[str, Any]] = None
    
    # For put_item
    item: Optional[Dict[str, Any]] = None
    
    # For update_item
    update_expression: Optional[str] = None
    expression_attribute_names: Optional[Dict[str, str]] = None
    expression_attribute_values: Optional[Dict[str, Any]] = None
    condition_expression: Optional[str] = None
    
    # For query
    key_condition: Optional[str] = None
    index_name: Optional[str] = None
    
    # Result handling
    result_path: str = "$.dynamodb_result"
    
    def __post_init__(self):
        if not self.table_name:
            raise ValueError("DynamoDBTask(...) requires 'table_name'.")
        _op_requires = {
            "get_item": ("key", self.key),
            "delete_item": ("key", self.key),
            "put_item": ("item", self.item),
            "update_item": ("update_expression", self.update_expression),
            "query": ("key_condition", self.key_condition),
        }
        if self.operation in _op_requires:
            field_name, value = _op_requires[self.operation]
            if not value:
                raise ValueError(
                    f"DynamoDBTask(operation={self.operation!r}) requires '{field_name}'."
                )
        if not self.step_id:
            self.step_id = f"DynamoDB_{self.operation}"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class SNSTask(Step):
    """
    Publish to SNS topic.
    
    Example:
        notify = SNSTask(
            step_id="notify_team",
            topic_arn="arn:aws:sns:us-east-1:123:alerts",
            message="Pipeline {% $.pipeline_name %} completed!",
            subject="Pipeline Alert"
        )
    """
    step_id: str = ""
    step_type: str = "sns"
    
    topic_arn: str = ""
    message: str = ""
    subject: Optional[str] = None
    message_attributes: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if not self.topic_arn:
            raise ValueError("SNSTask(...) requires 'topic_arn'.")
        if not self.message:
            raise ValueError("SNSTask(...) requires 'message'.")
        if not self.step_id:
            self.step_id = "SNS_Publish"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class SQSTask(Step):
    """
    Send message to SQS queue.
    
    Example:
        enqueue = SQSTask(
            step_id="enqueue_job",
            queue_url="https://sqs.us-east-1.amazonaws.com/123/my-queue",
            message_body={"job_id": "{% $.job_id %}", "data": "{% $.data %}"}
        )
    """
    step_id: str = ""
    step_type: str = "sqs"
    
    queue_url: str = ""
    message_body: Any = None
    delay_seconds: int = 0
    message_attributes: Optional[Dict[str, Any]] = None
    message_group_id: Optional[str] = None  # For FIFO queues
    
    def __post_init__(self):
        if not self.queue_url:
            raise ValueError("SQSTask(...) requires 'queue_url'.")
        if not self.step_id:
            self.step_id = "SQS_SendMessage"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class S3Task(Step):
    """
    S3 operations.
    
    Example:
        # Get object
        get_data = S3Task(
            step_id="get_input",
            operation="get_object",
            bucket="my-bucket",
            key="input/{% $.date %}/data.json"
        )
        
        # Put object  
        save_data = S3Task(
            step_id="save_output",
            operation="put_object",
            bucket="my-bucket",
            key="output/{% $.date %}/result.json",
            body="{% $string($.result) %}"
        )
    """
    step_id: str = ""
    step_type: str = "s3"
    
    operation: str = "get_object"  # get_object, put_object, copy_object, delete_object
    bucket: str = ""
    key: str = ""
    
    # For put_object
    body: Any = None
    content_type: Optional[str] = None
    
    # For copy_object
    copy_source: Optional[str] = None
    
    result_path: str = "$.s3_result"
    
    def __post_init__(self):
        if not self.bucket:
            raise ValueError("S3Task(...) requires 'bucket'.")
        if not self.key:
            raise ValueError("S3Task(...) requires 'key'.")
        if self.operation == "put_object" and not self.body:
            raise ValueError("S3Task(operation='put_object') requires 'body'.")
        if self.operation == "copy_object" and not self.copy_source:
            raise ValueError("S3Task(operation='copy_object') requires 'copy_source'.")
        if not self.step_id:
            self.step_id = f"S3_{self.operation}"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class GlueTask(Step):
    """
    Run AWS Glue job.
    
    Example:
        etl_job = GlueTask(
            step_id="run_etl",
            job_name="my-etl-job",
            arguments={
                "--source_path": "s3://bucket/input/",
                "--target_path": "s3://bucket/output/",
                "--date": "{% $.current_date %}"
            }
        )
    """
    step_id: str = ""
    step_type: str = "glue"
    
    job_name: str = ""
    arguments: Optional[Dict[str, str]] = None
    
    # Sync vs async
    wait_for_completion: bool = True
    
    def __post_init__(self):
        if not self.job_name:
            raise ValueError("GlueTask(...) requires 'job_name'.")
        if not self.step_id:
            self.step_id = f"Glue_{self.job_name}"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class AthenaTask(Step):
    """
    Run Athena query.
    
    Example:
        query = AthenaTask(
            step_id="run_query",
            query_string="SELECT * FROM sales WHERE date = '{% $.date %}'",
            database="my_database",
            output_location="s3://bucket/athena-results/"
        )
    """
    step_id: str = ""
    step_type: str = "athena"
    
    query_string: str = ""
    database: str = ""
    output_location: str = ""
    workgroup: str = "primary"
    
    wait_for_completion: bool = True
    
    def __post_init__(self):
        if not self.query_string:
            raise ValueError("AthenaTask(...) requires 'query_string'.")
        if not self.database:
            raise ValueError("AthenaTask(...) requires 'database'.")
        if not self.output_location:
            raise ValueError("AthenaTask(...) requires 'output_location'.")
        if not self.step_id:
            self.step_id = "Athena_Query"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class ECSTask(Step):
    """
    Run ECS/Fargate task.
    
    Example:
        container_job = ECSTask(
            step_id="run_container",
            cluster="my-cluster",
            task_definition="my-task:1",
            launch_type="FARGATE",
            overrides={
                "containerOverrides": [{
                    "name": "main",
                    "environment": [
                        {"name": "DATE", "value": "{% $.date %}"}
                    ]
                }]
            }
        )
    """
    step_id: str = ""
    step_type: str = "ecs"
    
    cluster: str = ""
    task_definition: str = ""
    launch_type: str = "FARGATE"  # FARGATE or EC2
    
    # Network config for Fargate
    subnets: Optional[List[str]] = None
    security_groups: Optional[List[str]] = None
    assign_public_ip: str = "DISABLED"
    
    # Overrides
    overrides: Optional[Dict[str, Any]] = None
    
    wait_for_completion: bool = True
    
    def __post_init__(self):
        if not self.cluster:
            raise ValueError("ECSTask(...) requires 'cluster'.")
        if not self.task_definition:
            raise ValueError("ECSTask(...) requires 'task_definition'.")
        # Mirrors @task.ecs()'s identical check (polyris/task.py) — Fargate
        # tasks run in an ENI and require at least one subnet; an empty
        # Subnets list fails opaquely at runTask. This is a separate,
        # independent construction path (a direct Step, not the @task.ecs
        # decorator) that previously had no such check at all.
        if self.launch_type == "FARGATE" and not self.subnets:
            raise ValueError(
                "ECSTask(launch_type='FARGATE') requires subnets "
                "(Fargate tasks run in an ENI). Pass subnets=[...]."
            )
        if not self.step_id:
            self.step_id = "ECS_RunTask"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass  
class EventBridgeTask(Step):
    """
    Put events to EventBridge.
    
    Example:
        emit_event = EventBridgeTask(
            step_id="emit_completion",
            event_bus="default",
            source="my.pipeline",
            detail_type="PipelineCompleted",
            detail={"pipeline": "{% $.pipeline_name %}", "status": "success"}
        )
    """
    step_id: str = ""
    step_type: str = "eventbridge"
    
    event_bus: str = "default"
    source: str = ""
    detail_type: str = ""
    detail: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if not self.source:
            raise ValueError("EventBridgeTask(...) requires 'source'.")
        if not self.detail_type:
            raise ValueError("EventBridgeTask(...) requires 'detail_type'.")
        if not self.step_id:
            self.step_id = "EventBridge_PutEvents"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)


@dataclass
class BedrockTask(Step):
    """
    Invoke Bedrock model (AI/ML).
    
    Example:
        ai_process = BedrockTask(
            step_id="analyze_text",
            model_id="anthropic.claude-3-sonnet",
            body={
                "prompt": "Analyze this data: {% $.data %}",
                "max_tokens": 1000
            }
        )
    """
    step_id: str = ""
    step_type: str = "bedrock"
    
    model_id: str = ""
    body: Optional[Dict[str, Any]] = None
    content_type: str = "application/json"
    accept: str = "application/json"
    
    result_path: str = "$.ai_result"
    
    def __post_init__(self):
        if not self.model_id:
            raise ValueError("BedrockTask(...) requires 'model_id'.")
        if not self.body:
            raise ValueError("BedrockTask(...) requires 'body'.")
        if not self.step_id:
            self.step_id = "Bedrock_InvokeModel"
        dag = get_current_dag()
        if dag:
            dag.add_step(self)
