"""
Generators for SFN-DSL.

This module contains all the generate_* functions:
- generate_step_function_json: Generate Step Functions JSON
- generate_dag_json: Generate DAG structure JSON
- generate_dag_hash: Deterministic hash for DAG change detection
- generate_mermaid: Generate Mermaid diagram
- generate_eventbridge_schedule: Generate EventBridge schedule
- generate_assets_json: Generate asset information JSON
- validate_asl: Validate Amazon States Language JSON
- generate_debug_info: Generate debug information for troubleshooting
"""

from typing import Dict, Any, List, Tuple, Iterator, Optional, Callable, Set
import json

from .constants import TaskConfigKey
from .config import config
from .dag import DAG
from .task import Task
from .steps import (
    Step, Wait, Pass, Succeed, LambdaTask, DynamoDBTask, SNSTask, SQSTask,
    S3Task, GlueTask, AthenaTask, ECSTask, EventBridgeTask, BedrockTask, HttpTask,
)
from .assets import Asset, AssetRef, AssetConsecutiveRef, AssetAll, AssetAny
from .schema import column_to_dict


# =============================================================================
# JSONata Expression Constants
# =============================================================================
# Used in task and step inputs for date/partition resolution.
# Fallback chain: variables.current_date → input.current_date → $now() today
#
# NOTE: Define_Inputs state (orchestrator level) uses a DIFFERENT fallback
# chain (→ null, not → $now()). Do NOT use these constants for Define_Inputs.

_DATE_FALLBACK = (
    "$exists($states.input.variables.current_date) "
    "? $states.input.variables.current_date "
    ": ($exists($states.input.current_date) "
    "? $states.input.current_date "
    ": $substringBefore($now(), 'T'))"
)

JSONATA_DATE = f"{{% {_DATE_FALLBACK} %}}"

JSONATA_PARTITION = (
    "{% $exists($states.input.variables.PARTITION_ARG) "
    "? $states.input.variables.PARTITION_ARG "
    ": ($exists($states.input.PARTITION_ARG) "
    "? $states.input.PARTITION_ARG "
    f": ({_DATE_FALLBACK})) %}}"
)

_EXEC_NAME_SUFFIX = (
    "($length($states.context.Execution.Name) > 8 "
    "? $substring($states.context.Execution.Name, "
    "$length($states.context.Execution.Name) - 8) "
    ": $states.context.Execution.Name)"
)

# Common JSONata expressions used across wrapper inputs and registration states
JSONATA_TOKEN = "{% $states.context.Task.Token %}"
JSONATA_EXECUTION_NAME = "{% $states.context.Execution.Name %}"
JSONATA_SKIP_TASKS = "{% $exists($states.input.skip_tasks) ? $states.input.skip_tasks : [] %}"
JSONATA_VARIABLES = "{% $exists($states.input.variables) ? $states.input.variables : {} %}"
# register_only must survive Define_Inputs the same way skip_tasks/variables do
# (bug found & fixed: Define_Inputs' Output previously replaced the whole
# state input and silently dropped register_only for any DAG with `variables`
# set, so Check_Register_Only's `$states.input.register_only = true` check
# always saw it as absent and fell through to Default: Run_All_Tasks — meaning
# polyris-register / deploy-time auto-registration on such a DAG actually ran
# the real pipeline instead of just registering metadata).
JSONATA_REGISTER_ONLY = "{% $exists($states.input.register_only) ? $states.input.register_only : false %}"
JSONATA_SFN_ARN = "{% $states.context.StateMachine.Id %}"
JSONATA_NOW = "{% $now() %}"
JSONATA_PASS_INPUT = "{% $states.input %}"
# Cascade control flags forwarded to run_task helper. Backend sets these
# from options.cascade ('auto'/'all'/'none') via bulk_backfill SFN. Default
# false → existing 'auto' behavior preserved for scheduled runs.
JSONATA_SUPPRESS_ASSET_EVENT = "{% $exists($states.input._suppress_asset_event) ? $states.input._suppress_asset_event : false %}"
JSONATA_CASCADE_ALL = "{% $exists($states.input.cascade_all) ? $states.input.cascade_all : false %}"
JSONATA_EXEC_SHORT = (
    "{% ( $exec := $states.context.Execution.Name; "
    "$short := $length($exec) > 20 ? $substring($exec, $length($exec) - 20) : $exec; "
    "$replace($replace($short, '.', ''), ':', '') ) %}"
)

# Step types that go through wrapper SFN for dependency resolution
WRAPPER_STEP_TYPES = frozenset({'lambda', 'glue', 'ecs', 'athena'})

# Step types tracked in DAG visualization (business logic, not infrastructure)
TRACKED_STEP_TYPES = frozenset({'glue', 'ecs', 'athena'})


def _make_execution_name_expr(task_id: str) -> str:
    """Build JSONata expression for deterministic SFN child execution Name.
    
    Format: {task_id}-{date}-{exec_suffix} truncated to 80 chars.
    Used for wrapper/step SFN startExecution.waitForTaskToken calls.
    """
    return (
        f"{{% $substring('{task_id}-' "
        f"& ({_DATE_FALLBACK}) "
        f"& '-' & {_EXEC_NAME_SUFFIX}, 0, 80) %}}"
    )


def _serialize_wait_for(wait_for: List[Any]) -> List[Dict[str, Any]]:
    """
    Serialize wait_for list for SFN input.
    
    Handles: Asset, AssetRef, AssetAll (AND), AssetAny (OR)
    
    Returns format:
        [
            {"asset_name": "inventory", "freshness_hours": null},
            {"asset_name": "catalog", "freshness_hours": 24},
            {"operator": "AND", "assets": [...]},
            {"operator": "OR", "assets": [...]}
        ]
    """
    result: List[Any] = []
    for item in wait_for:
        if isinstance(item, AssetConsecutiveRef):
            result.append({
                "asset_name": item.asset.name,
                "consecutive_days": item.consecutive_days
            })
        elif isinstance(item, AssetRef):
            result.append({
                "asset_name": item.asset.name,
                "freshness_hours": item.freshness_hours
            })
        elif isinstance(item, Asset):
            result.append({
                "asset_name": item.name,
                "freshness_hours": None  # latest
            })
        elif isinstance(item, (AssetAll, AssetAny)):
            # Recursively serialize grouped assets
            operator = "AND" if isinstance(item, AssetAll) else "OR"
            nested = _serialize_wait_for(item.assets)
            result.append({
                "operator": operator,
                "assets": nested
            })
    return result


def _serialize_wait_for_metadata(wait_for: List[Any]) -> List[Dict[str, Any]]:
    """
    Serialize wait_for list for metadata/lineage tracking.
    
    Similar to _serialize_wait_for but uses 'name' key (for UI/metadata)
    instead of 'asset_name' (for SFN runtime input).
    """
    result = []
    for item in wait_for:
        if isinstance(item, AssetConsecutiveRef):
            result.append({"name": item.asset.name, "consecutive_days": item.consecutive_days})
        elif isinstance(item, AssetRef):
            if item.freshness_hours:
                result.append({"name": item.asset.name, "freshness_hours": item.freshness_hours})
            else:  # pragma: no cover -- .within() requires a positive freshness, so an AssetRef with falsy freshness never reaches the metadata serializer
                result.append({"name": item.asset.name})
        elif isinstance(item, Asset):
            result.append({"name": item.name})
        elif isinstance(item, (AssetAll, AssetAny)):
            # Mirror the runtime serializer: grouped (AND/OR) dependencies must
            # appear in lineage too, otherwise the UI shows no dependency at all.
            operator = "AND" if isinstance(item, AssetAll) else "OR"
            result.append({
                "operator": operator,
                "assets": _serialize_wait_for_metadata(item.assets),
            })
    return result


def _serialize_outlet(asset) -> Dict[str, Any]:
    """Serialize an asset outlet reference with full metadata for lineage."""
    if hasattr(asset, 'name'):
        result = {
            "name": asset.name,
            "uri": getattr(asset, 'uri', ''),
        }
        # Include enrichment fields if present
        if getattr(asset, 'description', ''):
            result["description"] = asset.description
        if getattr(asset, 'owner', ''):
            result["owner"] = asset.owner
        if getattr(asset, 'tags', None):
            result["tags"] = asset.tags
        if getattr(asset, 'freshness_hours', None) is not None:
            result["freshness_hours"] = asset.freshness_hours
        if getattr(asset, 'glue_table', ''):
            result["glue_table"] = asset.glue_table
        if getattr(asset, 'glue_catalog', ''):
            result["glue_catalog"] = asset.glue_catalog
        if getattr(asset, 'glue_region', ''):
            result["glue_region"] = asset.glue_region
        if getattr(asset, 'schema', None):
            result["schema"] = [column_to_dict(c) for c in asset.schema]
        # Partition cadence (ADR #50). Always emitted when the SDK is at
        # v0.77+ because `granularity` has a default ("daily"). Older
        # pipeline_registry records without these fields are interpreted
        # as daily by the backend (see ADR #50 §Backward compatibility).
        granularity = getattr(asset, 'granularity', None)
        if granularity:
            result["granularity"] = granularity
        partition_start = getattr(asset, 'partition_start', None)
        if partition_start:
            result["partition_start"] = partition_start
        return result
    return {"name": str(asset)}  # pragma: no cover -- every Asset/AssetRef carries a name; the str() fallback guards a shape that the DSL cannot produce


def _serialize_inlet(asset) -> Dict[str, str]:
    """Serialize an asset inlet reference: {"name": ...}."""
    return {"name": asset.name}


# ============================================================================
# ASL Validation
# ============================================================================

VALID_STATE_TYPES = {"Task", "Pass", "Choice", "Wait", "Succeed", "Fail", "Parallel", "Map"}

def validate_asl(asl: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    """
    Validate Amazon States Language JSON.
    
    Returns:
        (is_valid, errors, warnings)
        - is_valid: True if no errors
        - errors: List of error messages (fatal)
        - warnings: List of warning messages (non-fatal)
    """
    errors = []
    warnings: List[str] = []
    
    # 1. Basic structure
    if "StartAt" not in asl:
        errors.append("Missing required field 'StartAt'")
    if "States" not in asl:
        errors.append("Missing required field 'States'")
        return (False, errors, warnings)
    
    states = asl.get("States", {})
    start_at = asl.get("StartAt")
    
    # 2. StartAt points to existing state
    if start_at and start_at not in states:
        errors.append(f"StartAt '{start_at}' does not exist in States")
    
    # 3. Validate each state
    all_next_refs = set()
    end_states = set()
    choice_defaults = set()
    
    for state_name, state in states.items():
        # Type is required
        if "Type" not in state:
            errors.append(f"State '{state_name}': Missing required field 'Type'")
            continue
        
        state_type = state["Type"]
        
        # Valid type
        if state_type not in VALID_STATE_TYPES:
            errors.append(f"State '{state_name}': Invalid Type '{state_type}'")
            continue
        
        # Check End/Next logic
        has_end = state.get("End", False)
        has_next = "Next" in state
        
        if state_type in ("Succeed", "Fail"):
            # These are terminal states
            end_states.add(state_name)
            if has_next:
                warnings.append(f"State '{state_name}': Type '{state_type}' should not have 'Next'")
        elif state_type == "Choice":
            # Choice must have Choices, may have Default
            if "Choices" not in state:
                errors.append(f"State '{state_name}': Choice state missing 'Choices'")
            else:
                for i, choice in enumerate(state["Choices"]):
                    if "Next" in choice:
                        all_next_refs.add(choice["Next"])
                    else:
                        errors.append(f"State '{state_name}': Choice[{i}] missing 'Next'")
            
            if "Default" in state:
                all_next_refs.add(state["Default"])
                choice_defaults.add(state_name)
            else:
                warnings.append(f"State '{state_name}': Choice without 'Default' may cause execution failure")
        else:
            # Other states need End or Next
            if not has_end and not has_next:
                errors.append(f"State '{state_name}': Must have either 'End' or 'Next'")
            if has_end and has_next:
                warnings.append(f"State '{state_name}': Has both 'End' and 'Next' (Next will be ignored)")
            if has_end:
                end_states.add(state_name)
            if has_next:
                all_next_refs.add(state["Next"])
        
        # Catch transitions (error handlers) — their targets must exist too.
        # Without this, a Catch pointing at a non-existent state passes validation
        # and only fails at deploy time in AWS.
        for catcher in state.get("Catch", []):
            if "Next" in catcher:
                all_next_refs.add(catcher["Next"])
        
        # Parallel must have at least one branch (AWS rejects an empty Parallel)
        if state_type == "Parallel":
            if not state.get("Branches"):
                errors.append(f"State '{state_name}': Parallel state has no branches (missing or empty; needs at least one)")
            else:
                for i, branch in enumerate(state["Branches"]):
                    # Recursively validate branches
                    valid, branch_errors, branch_warnings = validate_asl(branch)
                    for e in branch_errors:
                        errors.append(f"State '{state_name}' Branch[{i}]: {e}")
                    for w in branch_warnings:
                        warnings.append(f"State '{state_name}' Branch[{i}]: {w}")
        
        # Map must have Iterator or ItemProcessor
        if state_type == "Map":
            if "Iterator" not in state and "ItemProcessor" not in state:
                errors.append(f"State '{state_name}': Map state missing 'Iterator' or 'ItemProcessor'")
            else:
                # ItemProcessor contains StartAt/States directly (not in ProcessorConfig)
                iterator = state.get("Iterator") or state.get("ItemProcessor", {})
                if iterator and "StartAt" in iterator:
                    valid, iter_errors, iter_warnings = validate_asl(iterator)
                    for e in iter_errors:
                        errors.append(f"State '{state_name}' Iterator: {e}")
                    for w in iter_warnings:
                        warnings.append(f"State '{state_name}' Iterator: {w}")
    
    # 4. All Next references point to existing states
    for ref in all_next_refs:
        if ref not in states:
            errors.append(f"Invalid transition: 'Next' points to non-existent state '{ref}'")
    
    # 5. Check for unreachable states
    reachable: Set[str] = set()
    if start_at:
        _find_reachable(start_at, states, reachable)
    
    unreachable = set(states.keys()) - reachable
    for state_name in unreachable:
        warnings.append(f"State '{state_name}' is unreachable from StartAt")
    
    # 6. Must have at least one end state
    if not end_states and not errors:
        warnings.append("No terminal states (End:true, Succeed, or Fail) found")
    
    return (len(errors) == 0, errors, warnings)


def _find_reachable(state_name: str, states: Dict, reachable: set) -> None:
    """Recursively find all reachable states from a given state."""
    if state_name in reachable or state_name not in states:
        return
    
    reachable.add(state_name)
    state = states[state_name]
    
    # Follow Next
    if "Next" in state:
        _find_reachable(state["Next"], states, reachable)
    
    # Follow Catch (error handler) transitions — states reachable only via a
    # Catch (e.g. a shared Pipeline_Failed handler) are otherwise mis-flagged
    # as unreachable.
    for catcher in state.get("Catch", []):
        if "Next" in catcher:
            _find_reachable(catcher["Next"], states, reachable)
    
    # Follow Choice branches
    if state.get("Type") == "Choice":
        for choice in state.get("Choices", []):
            if "Next" in choice:
                _find_reachable(choice["Next"], states, reachable)
        if "Default" in state:
            _find_reachable(state["Default"], states, reachable)
    
    # Follow Parallel branches (start states)
    if state.get("Type") == "Parallel":
        for branch in state.get("Branches", []):
            if "StartAt" in branch:
                _find_reachable(branch["StartAt"], branch.get("States", {}), reachable)
    
    # Follow Map iterator
    if state.get("Type") == "Map":
        iterator = state.get("Iterator") or state.get("ItemProcessor", {})
        if "StartAt" in iterator:
            _find_reachable(iterator["StartAt"], iterator.get("States", {}), reachable)


def generate_debug_info(dag: "DAG") -> Dict[str, Any]:
    """
    Generate debug information showing the transformation pipeline.
    
    Returns dict with:
    - dag_structure: Python DAG representation
    - tasks: Task details with dependencies
    - asl_preview: Generated ASL (truncated)
    - validation: ASL validation results
    - asset_info: Asset triggers (if any)
    """
    from .generators import generate_step_function_json
    
    debug = {
        "dag_id": dag.dag_id,
        "schedule": dag.schedule,
        "is_asset_triggered": dag.is_asset_triggered,
    }
    
    # 1. DAG Structure
    debug["dag_structure"] = {
        "tasks": len(dag.tasks),
        "execution_order": [t.task_id for t in dag.topological_sort()],
    }
    
    # 2. Task details
    debug["tasks"] = []
    for task in dag.topological_sort():
        task_info = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "arn": task.arn[:50] + "..." if task.arn and len(task.arn) > 50 else task.arn,
            "dependencies": [d.node_id for d in task.dependencies],
            "timeout": task.timeout,
            "retries": task.retries,
        }
        if task.outlets:
            task_info["outlets"] = [a.name for a in task.outlets]
        if task.skip_on_backfill:
            task_info["skip_on_backfill"] = True
        if task.wait_before_seconds:
            task_info["wait_before"] = task.wait_before_seconds
        debug["tasks"].append(task_info)
    
    # 3. Generate ASL and validate
    try:
        asl_json = generate_step_function_json(dag)
        asl = json.loads(asl_json)
        
        # Truncated preview
        debug["asl_preview"] = {
            "StartAt": asl.get("StartAt"),
            "QueryLanguage": asl.get("QueryLanguage"),
            "states_count": len(asl.get("States", {})),
            "state_names": list(asl.get("States", {}).keys())[:10],  # First 10
        }
        
        # 4. Validation
        is_valid, errors, warnings = validate_asl(asl)
        debug["validation"] = {
            "is_valid": is_valid,
            "errors": errors,
            "warnings": warnings,
        }
        
    except Exception as e:  # pragma: no cover -- defensive: ASL generation for a validated DAG does not raise here; surfaced for debug output only
        debug["asl_preview"] = {"error": str(e)}
        debug["validation"] = {
            "is_valid": False,
            "errors": [f"ASL generation failed: {str(e)}"],
            "warnings": [],
        }
    
    # 5. Asset info
    if dag.is_asset_triggered:
        debug["asset_info"] = dag.asset_schedule_info
    
    return debug


def _gen_wait_state(step: Wait) -> Dict:
    """Generate Wait state."""
    state: Dict[str, Any] = {"Type": "Wait"}
    if step.seconds:
        state["Seconds"] = step.seconds
    elif step.timestamp:
        state["Timestamp"] = step.timestamp
    elif step.timestamp_path:
        state["TimestampPath"] = step.timestamp_path
    return state


def _gen_pass_state(step: Pass) -> Dict:
    """Generate Pass state."""
    state: Dict[str, Any] = {"Type": "Pass"}
    if step.output:
        state["Output"] = step.output
    # NOTE: Result/ResultPath not supported in JSONata mode
    return state


def _gen_succeed_state(step: Succeed) -> Dict:
    """Generate Succeed state."""
    state: Dict[str, Any] = {"Type": "Succeed"}
    if step.output:
        state["Output"] = step.output
    return state


def _gen_lambda_state(step: LambdaTask) -> Dict:
    """Generate Lambda:invoke state."""
    state: Dict[str, Any] = {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {
            "FunctionName": step.function_arn,
            "Payload": step.payload or "{% $states.input %}"
        }
    }
    if step.retries > 0:
        state["Retry"] = [
            {"ErrorEquals": ["Lambda.ServiceException"], "IntervalSeconds": step.retry_interval, "MaxAttempts": step.retries}
        ]
    return state


_DYNAMODB_RESOURCES = {
    "get_item": "arn:aws:states:::dynamodb:getItem",
    "put_item": "arn:aws:states:::dynamodb:putItem",
    "update_item": "arn:aws:states:::dynamodb:updateItem",
    "delete_item": "arn:aws:states:::dynamodb:deleteItem",
    "query": "arn:aws:states:::aws-sdk:dynamodb:query",
    "scan": "arn:aws:states:::aws-sdk:dynamodb:scan",
}


def _gen_dynamodb_state(step: DynamoDBTask) -> Dict:
    """Generate DynamoDB state (get/put/update/delete/query/scan)."""
    args: Dict[str, Any] = {"TableName": step.table_name}
    if step.key:
        args["Key"] = step.key
    if step.item:
        args["Item"] = step.item
    if step.update_expression:
        args["UpdateExpression"] = step.update_expression
    if step.expression_attribute_names:
        args["ExpressionAttributeNames"] = step.expression_attribute_names
    if step.expression_attribute_values:
        args["ExpressionAttributeValues"] = step.expression_attribute_values
    if step.condition_expression:
        args["ConditionExpression"] = step.condition_expression
    if step.key_condition:
        args["KeyConditionExpression"] = step.key_condition
    if step.index_name:
        args["IndexName"] = step.index_name
    return {
        "Type": "Task",
        "Resource": _DYNAMODB_RESOURCES.get(step.operation, _DYNAMODB_RESOURCES["get_item"]),
        "Arguments": args,
    }


def _gen_sns_state(step: SNSTask) -> Dict:
    """Generate SNS:publish state."""
    args: Dict[str, Any] = {"TopicArn": step.topic_arn, "Message": step.message}
    if step.subject:
        args["Subject"] = step.subject
    if step.message_attributes:
        args["MessageAttributes"] = step.message_attributes
    return {"Type": "Task", "Resource": "arn:aws:states:::sns:publish", "Arguments": args}


def _gen_sqs_state(step: SQSTask) -> Dict:
    """Generate SQS:sendMessage state."""
    args: Dict[str, Any] = {
        "QueueUrl": step.queue_url,
        "MessageBody": step.message_body if isinstance(step.message_body, str) else "{% $string($states.input) %}"
    }
    if step.delay_seconds:
        args["DelaySeconds"] = step.delay_seconds
    if step.message_attributes:
        args["MessageAttributes"] = step.message_attributes
    if step.message_group_id:
        args["MessageGroupId"] = step.message_group_id
    return {"Type": "Task", "Resource": "arn:aws:states:::sqs:sendMessage", "Arguments": args}


_S3_RESOURCES = {
    "get_object": "arn:aws:states:::aws-sdk:s3:getObject",
    "put_object": "arn:aws:states:::aws-sdk:s3:putObject",
    "copy_object": "arn:aws:states:::aws-sdk:s3:copyObject",
    "delete_object": "arn:aws:states:::aws-sdk:s3:deleteObject",
}


def _gen_s3_state(step: S3Task) -> Dict:
    """Generate S3 state (get/put/copy/delete)."""
    args: Dict[str, Any] = {"Bucket": step.bucket, "Key": step.key}
    if step.body:
        args["Body"] = step.body
    if step.content_type:
        args["ContentType"] = step.content_type
    if step.copy_source:
        args["CopySource"] = step.copy_source
    return {
        "Type": "Task",
        "Resource": _S3_RESOURCES.get(step.operation, _S3_RESOURCES["get_object"]),
        "Arguments": args,
    }


def _gen_glue_state(step: GlueTask) -> Dict:
    """Generate Glue:startJobRun state."""
    resource = "arn:aws:states:::glue:startJobRun.sync" if step.wait_for_completion else "arn:aws:states:::glue:startJobRun"
    args: Dict[str, Any] = {"JobName": step.job_name}
    if step.arguments:
        args["Arguments"] = step.arguments
    return {"Type": "Task", "Resource": resource, "Arguments": args}


def _gen_athena_state(step: AthenaTask) -> Dict:
    """Generate Athena:startQueryExecution state."""
    resource = "arn:aws:states:::athena:startQueryExecution.sync" if step.wait_for_completion else "arn:aws:states:::athena:startQueryExecution"
    return {
        "Type": "Task",
        "Resource": resource,
        "Arguments": {
            "QueryString": step.query_string,
            "QueryExecutionContext": {"Database": step.database},
            "ResultConfiguration": {"OutputLocation": step.output_location},
            "WorkGroup": step.workgroup,
        },
    }


def _gen_ecs_state(step: ECSTask) -> Dict:
    """Generate ECS:runTask state."""
    resource = "arn:aws:states:::ecs:runTask.sync" if step.wait_for_completion else "arn:aws:states:::ecs:runTask"
    args: Dict[str, Any] = {
        "Cluster": step.cluster,
        "TaskDefinition": step.task_definition,
        "LaunchType": step.launch_type,
    }
    if step.launch_type == "FARGATE" and step.subnets:
        args["NetworkConfiguration"] = {
            "AwsvpcConfiguration": {
                "Subnets": step.subnets,
                "SecurityGroups": step.security_groups or [],
                "AssignPublicIp": step.assign_public_ip,
            }
        }
    if step.overrides:
        args["Overrides"] = step.overrides
    return {"Type": "Task", "Resource": resource, "Arguments": args}


def _gen_eventbridge_state(step: EventBridgeTask) -> Dict:
    """Generate EventBridge:putEvents state."""
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::events:putEvents",
        "Arguments": {
            "Entries": [{
                "EventBusName": step.event_bus,
                "Source": step.source,
                "DetailType": step.detail_type,
                "Detail": step.detail if isinstance(step.detail, str) else "{% $string($states.input) %}",
            }]
        },
    }


def _gen_bedrock_state(step: BedrockTask) -> Dict:
    """Generate Bedrock:invokeModel state."""
    return {
        "Type": "Task",
        "Resource": "arn:aws:states:::bedrock:invokeModel",
        "Arguments": {
            "ModelId": step.model_id,
            "Body": step.body,
            "ContentType": step.content_type,
            "Accept": step.accept,
        },
    }


def _gen_http_state(step: HttpTask) -> Dict:
    """Generate HTTP:invoke state."""
    args: Dict[str, Any] = {"ApiEndpoint": step.url, "Method": step.method}
    if step.headers:
        args["Headers"] = step.headers
    if step.body:
        args["RequestBody"] = step.body
    if step.connection_arn:
        args["Authentication"] = {"ConnectionArn": step.connection_arn}
    return {"Type": "Task", "Resource": "arn:aws:states:::http:invoke", "Arguments": args}


# Dispatch table: step_type → builder function
_STEP_STATE_BUILDERS: Dict[str, Callable[[Any], Dict]] = {
    "wait": _gen_wait_state,
    "pass": _gen_pass_state,
    "succeed": _gen_succeed_state,
    "lambda": _gen_lambda_state,
    "dynamodb": _gen_dynamodb_state,
    "sns": _gen_sns_state,
    "sqs": _gen_sqs_state,
    "s3": _gen_s3_state,
    "glue": _gen_glue_state,
    "athena": _gen_athena_state,
    "ecs": _gen_ecs_state,
    "eventbridge": _gen_eventbridge_state,
    "bedrock": _gen_bedrock_state,
    "http": _gen_http_state,
}


def _generate_step_state(step: Step) -> Dict:
    """Generate Step Functions state JSON for any Step type.

    Dispatches to a per-type builder.  New step types only need a new
    ``_gen_<type>_state`` function and a single entry in ``_STEP_STATE_BUILDERS``.
    """
    builder = _STEP_STATE_BUILDERS.get(step.step_type)
    if builder:
        return builder(step)
    # An unrecognized step_type most likely means the base `Step` class was
    # instantiated directly (its default step_type is "step", which has no
    # entry in _STEP_STATE_BUILDERS) instead of a concrete subclass like
    # Pass/Wait/LambdaTask/etc. Silently falling back to a no-op Pass state
    # here used to mean the mistake shipped: validate_asl/validate_asl_from_dag
    # see a perfectly well-formed {"Type": "Pass"} and report zero errors or
    # warnings, so the pipeline deploys and "succeeds" while doing nothing for
    # this step — the worst kind of failure, because nothing ever surfaces it.
    raise ValueError(
        f"Step '{step.step_id}' has an unrecognized step_type={step.step_type!r} "
        f"(known types: {sorted(_STEP_STATE_BUILDERS)}). If you constructed this "
        f"with `Step(...)` directly, use a concrete subclass instead — e.g. "
        f"Pass(...), Wait(...), or a @task decorator — Step itself is a base "
        f"class and has no runnable behavior."
    )


def _build_wrapper_input(
    task_name: str,
    task_arn: str,
    task_type: str,
    dependencies: List[str],
    dag: DAG,
    *,
    cross_account_role: str = "same",
    orchestration_timeout: int = 3600,
    task_config: Optional[Dict] = None,
) -> Dict:
    """Build the common wrapper input dict shared by Task and Step branches.

    Returns the base dict with all standard fields.  Callers can add
    Task-specific extras (alerts, outlets, wait_for, etc.) to the result.
    """
    wrapper_input: Dict[str, Any] = {
        "task_name": task_name,
        "task_arn": task_arn,
        "task_type": task_type,
        "dependencies": dependencies,
        "cross_account_role": cross_account_role,
        "token": JSONATA_TOKEN,
        "pipeline_name": dag.dag_id,
        "pipeline_execution": JSONATA_EXECUTION_NAME,
        "skip_tasks": JSONATA_SKIP_TASKS,
        # Forward cascade control flags from parent execution input (set
        # by bulk_backfill SFN per ADR #57 cascade semantics). Default
        # false → run_task helper preserves existing 'auto' behavior.
        "_suppress_asset_event": JSONATA_SUPPRESS_ASSET_EVENT,
        "cascade_all": JSONATA_CASCADE_ALL,
        "date": JSONATA_DATE,
        "current_date": JSONATA_DATE,
        "PARTITION_ARG": JSONATA_PARTITION,
        "variables": JSONATA_VARIABLES,
        "orchestration_timeout": orchestration_timeout,
    }
    if task_config:
        wrapper_input["task_config"] = task_config
    return wrapper_input


def _build_task_config_and_arn(task: Task) -> Tuple[Dict[TaskConfigKey, Any], str]:
    """Build task_config (task-type-specific settings + retry policy) and
    resolve task_arn for task types (lambda) that derive it from a
    different field than task.arn.

    Returns `str`, never None: Task.arn is `str = ""` and the single Task
    constructor normalizes with `arn or ""`. Task types addressed by config
    rather than ARN (glue/ecs/athena/emr/batch take job_name / cluster /
    query_string) legitimately carry the empty string, which is the shape the
    generated ASL expects — not a missing value.

    Shared between _build_task_branch (the original dispatch's own
    wrapper_input) and _build_dag_visualization_nodes_edges (so task_config
    is also stored in the registry's dag_metadata) — previously task_config
    was never persisted anywhere retrievable after the original dispatch,
    so restart_task_helper's Start_New_Wrapper had nothing to reconstruct
    it from and always passed an empty {}, silently dropping a restarted
    task's real retries/worker-type/etc settings.
    """
    task_arn = task.arn
    task_config: Dict[TaskConfigKey, Any] = {}
    if task.task_type == 'lambda':
        task_config = {
            TaskConfigKey.FUNCTION_NAME: task.function_name,
            TaskConfigKey.PAYLOAD: task.payload or {}
        }
        # For lambda, use function ARN or build from function_name
        if not task_arn and task.function_name:
            task_arn = task.function_name  # Will be resolved by Lambda:invoke
    elif task.task_type == 'glue':
        task_config = {
            TaskConfigKey.JOB_NAME: task.job_name,
            TaskConfigKey.ARGUMENTS: task.glue_arguments or {}
        }
        if task.worker_type:
            task_config[TaskConfigKey.WORKER_TYPE] = task.worker_type
        if task.number_of_workers:
            task_config[TaskConfigKey.NUMBER_OF_WORKERS] = task.number_of_workers
        if task.allocated_capacity:
            task_config[TaskConfigKey.ALLOCATED_CAPACITY] = task.allocated_capacity
    elif task.task_type == 'ecs':
        task_config = {
            TaskConfigKey.CLUSTER: task.cluster,
            TaskConfigKey.TASK_DEFINITION: task.task_definition,
            TaskConfigKey.LAUNCH_TYPE: task.launch_type,
            TaskConfigKey.SUBNETS: task.subnets or [],
            TaskConfigKey.SECURITY_GROUPS: task.security_groups or [],
            TaskConfigKey.ASSIGN_PUBLIC_IP: task.assign_public_ip,
            TaskConfigKey.OVERRIDES: task.container_overrides or {}
        }
    elif task.task_type == 'athena':
        task_config = {
            TaskConfigKey.QUERY_STRING: task.query_string,
            TaskConfigKey.DATABASE: task.database,
            TaskConfigKey.OUTPUT_LOCATION: task.output_location,
            TaskConfigKey.WORKGROUP: task.workgroup
        }
    elif task.task_type == 'emr':
        task_config = {
            TaskConfigKey.CLUSTER_ID: task.emr_cluster_id,
            TaskConfigKey.STEP: task.emr_step or {}
        }
    elif task.task_type == 'batch':
        task_config = {
            TaskConfigKey.JOB_DEFINITION: task.job_definition,
            TaskConfigKey.JOB_QUEUE: task.job_queue,
            TaskConfigKey.PARAMETERS: task.batch_parameters or {}
        }
    # sfn type doesn't need task_config

    # Thread the retry policy into task_config only when retries are requested
    # (ADR #107). When absent, the wrapper's Check_Should_Retry defaults to 0, so
    # no-retry tasks keep their existing contract untouched (including sfn's empty
    # one) and incur no snapshot churn; sfn tasks with retries opt in uniformly.
    if task.retries:
        task_config[TaskConfigKey.RETRIES] = task.retries
        task_config[TaskConfigKey.RETRY_DELAY] = task.retry_delay_seconds
        if task.retry_exponential_backoff:
            task_config[TaskConfigKey.RETRY_BACKOFF] = True
            if task.max_retry_delay_seconds is not None:
                task_config[TaskConfigKey.MAX_RETRY_DELAY] = task.max_retry_delay_seconds
        if task.retry_jitter:
            task_config[TaskConfigKey.RETRY_JITTER] = True

    return task_config, task_arn


def _build_task_branch(task: Task, dag: DAG, wrapper_arn: str) -> Dict:
    """Build a single parallel branch for a Task (wrapper-based execution).
    
    Each Task runs via nested SFN (wrapper) with waitForTaskToken.
    The wrapper handles dependency resolution, retries, and error reporting.
    
    Args:
        task: Task to generate branch for
        dag: Parent DAG (for dag_id)
        wrapper_arn: ARN of the wrapper state machine
    
    Returns:
        Branch dict: {"StartAt": "Task_{id}", "States": {...}}
    """
    # Check if task depends on direct steps (which don't support deps)
    direct_step_deps = [
        d for d in task.dependencies 
        if isinstance(d, Step) and d.step_type not in WRAPPER_STEP_TYPES
    ]
    if direct_step_deps:
        dep_names = [d.step_id for d in direct_step_deps]
        raise ValueError(
            f"Task '{task.task_id}' depends on direct step(s): {dep_names}. "
            f"Direct steps (wait/pass/sns/sqs/s3/...) do not support dependencies. "
            f"Use @task with wait_before parameter instead."
        )
    
    dep_names = [d.node_id for d in task.dependencies]
    task_config, task_arn = _build_task_config_and_arn(task)
    # Resolve cross_account_role to ARN from config.roles (pyproject.toml)
    # If role is defined in config.roles, use the ARN; otherwise keep the string
    cross_account_role = task.role
    if task.role and task.role != 'same' and task.role in config.roles:  # pragma: no cover -- cross-account role->ARN resolution depends on a populated config.roles, exercised by deploy e2e
        cross_account_role = config.roles[task.role]
    
    # Build task state (JSONata syntax)
    # Name is deterministic based on current_date (not $now()) for proper backfill support
    task_input = _build_wrapper_input(
        task_name=task.task_id,
        task_arn=task_arn,
        task_type=task.task_type,
        dependencies=dep_names,
        dag=dag,
        cross_account_role=cross_account_role,
        orchestration_timeout=task.orchestration_timeout_seconds,
        task_config=task_config,
    )
    # Task-specific extras (not applicable to Step wrapper)
    task_input["wait_before"] = task.wait_before_seconds
    task_input["trigger_rule"] = task.trigger_rule or "all_success"
    task_input["pipeline_execution_short"] = JSONATA_EXEC_SHORT
    
    # Add outlets for asset-based orchestration
    # When task completes, wrapper will emit EventBridge events for each outlet
    if task.outlets:
        task_input["outlets"] = [_serialize_outlet(a) for a in task.outlets]
    
    # Add wait_for for pull-based cross-pipeline asset dependencies
    if task.wait_for:
        task_input["wait_for"] = _serialize_wait_for(task.wait_for)
    
    task_state = {
        "Type": "Task",
        "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
        "TimeoutSeconds": task.timeout + task.wait_before_seconds,
        "Arguments": {
            "StateMachineArn": wrapper_arn,
            "Name": _make_execution_name_expr(task.task_id),
            "Input": task_input
        },
        "End": True
    }
    
    # NOTE: No Retry here! Retry on waitForTaskToken with deterministic Name
    # causes ExecutionAlreadyExists. Retries are handled inside wrapper/run_task.
    
    # Note: wait_before is now handled inside the wrapper, not as a separate Wait state
    # This ensures the task is registered in DynamoDB immediately and UI can show countdown
    
    return {"StartAt": f"Task_{task.task_id}", "States": {f"Task_{task.task_id}": task_state}}


def _build_step_branch(step: Step, dag: DAG, wrapper_arn: str) -> Dict:
    """Build a single parallel branch for a Step.
    
    Two modes:
    - Wrapper steps (lambda/glue/ecs/athena): Full dependency support via wrapper
    - Direct steps (wait/pass/sns/sqs/s3/...): Run in parallel, NO dependency support
    
    Args:
        step: Step to generate branch for
        dag: Parent DAG (for dag_id)
        wrapper_arn: ARN of the wrapper state machine
    
    Returns:
        Branch dict: {"StartAt": "{step_id}", "States": {...}}
    """
    # These types go through wrapper for dependency resolution
    if step.step_type in WRAPPER_STEP_TYPES:
        # Validate that wrapper steps don't depend on direct steps
        # Direct steps don't register in DynamoDB, so wrappers would wait forever
        direct_step_deps = [
            d for d in step.dependencies 
            if isinstance(d, Step) and d.step_type not in WRAPPER_STEP_TYPES
        ]
        if direct_step_deps:
            dep_names = [d.step_id for d in direct_step_deps]
            raise ValueError(
                f"Wrapper step '{step.step_id}' ({step.step_type}) depends on direct step(s): {dep_names}. "
                f"Direct steps (wait/pass/sns/sqs/s3/...) don't register in DynamoDB and cannot be dependencies. "
                f"Use wait_before parameter or restructure your DAG."
            )
        
        # Generate wrapper call with task_type and task_config
        dep_names = [d.node_id for d in step.dependencies]
        
        # Build task_config based on step type
        task_config: Dict[TaskConfigKey, Any] = {}
        if step.step_type == 'lambda':
            assert isinstance(step, LambdaTask)  # 1:1 step_type<->class; narrows for typing
            task_config = {TaskConfigKey.PAYLOAD: step.payload or {}}
        elif step.step_type == 'glue':
            assert isinstance(step, GlueTask)  # 1:1 step_type<->class; narrows for typing
            task_config = {
                TaskConfigKey.JOB_NAME: step.job_name,
                TaskConfigKey.ARGUMENTS: step.arguments or {}
            }
        elif step.step_type == 'ecs':
            assert isinstance(step, ECSTask)  # 1:1 step_type<->class; narrows for typing
            task_config = {
                TaskConfigKey.CLUSTER: step.cluster,
                TaskConfigKey.TASK_DEFINITION: step.task_definition,
                TaskConfigKey.LAUNCH_TYPE: step.launch_type,
                TaskConfigKey.SUBNETS: step.subnets or [],
                TaskConfigKey.SECURITY_GROUPS: step.security_groups or [],
                TaskConfigKey.ASSIGN_PUBLIC_IP: step.assign_public_ip,
                TaskConfigKey.OVERRIDES: step.overrides or {}
            }
        elif step.step_type == 'athena':
            assert isinstance(step, AthenaTask)  # 1:1 step_type<->class; narrows for typing
            task_config = {
                TaskConfigKey.QUERY_STRING: step.query_string,
                TaskConfigKey.DATABASE: step.database,
                TaskConfigKey.OUTPUT_LOCATION: step.output_location,
                TaskConfigKey.WORKGROUP: step.workgroup
            }
        
        # For lambda, task_arn is function_arn
        task_arn = getattr(step, 'function_arn', '') or getattr(step, 'task_arn', '') or step.step_id
        
        # Get timeout from step or use default (3600 for lambda, 86400 for glue/ecs/athena)
        default_timeout = 3600 if step.step_type == 'lambda' else 86400
        step_timeout = getattr(step, 'timeout', default_timeout)
        
        step_state = {
            "Type": "Task",
            "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
            "TimeoutSeconds": step_timeout,
            "Arguments": {
                "StateMachineArn": wrapper_arn,
                "Name": _make_execution_name_expr(step.step_id),
                "Input": _build_wrapper_input(
                    task_name=step.step_id,
                    task_arn=task_arn,
                    task_type=step.step_type,
                    dependencies=dep_names,
                    dag=dag,
                    orchestration_timeout=step_timeout,
                    task_config=task_config,
                ),
            },
            "End": True
        }
        
        return {"StartAt": step.step_id, "States": {step.step_id: step_state}}
    else:
        # Direct steps (wait, pass, sns, sqs, s3, etc.) - NOT integrated with wrapper
        # Dependencies on direct steps will NOT work - fail fast with clear error
        if step.dependencies:
            dep_names = [d.node_id for d in step.dependencies]
            raise ValueError(
                f"Direct step '{step.step_id}' ({step.step_type}) cannot have dependencies: {dep_names}. "
                f"Only wrapper steps (lambda/glue/ecs/athena) support dependencies. "
                f"Use @task with wait_before parameter instead."
            )
        
        step_state = _generate_step_state(step)
        step_state["End"] = True
        
        return {"StartAt": step.step_id, "States": {step.step_id: step_state}}


def _build_dag_visualization_nodes_edges(dag: DAG) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build the canonical (nodes, edges) pair for DAG visualization.

    Single source of truth for what a "node" looks like in visualization JSON —
    used by both generate_dag_json (the public polyris-output/polyris-validate
    surface) and _build_pipeline_metadata (whose dag_metadata is what gets
    stored in the pipeline registry and per-execution snapshots, and is what
    /pipeline-dag's registry fallback returns for "current structure"/blueprint
    mode). These were previously two independent implementations that had
    quietly drifted apart: generate_dag_json had `type`/`trigger_rule`/tracked
    steps that _build_pipeline_metadata's version lacked, while
    _build_pipeline_metadata had `wait_for` (asset pull dependency) that
    generate_dag_json lacked. Unifying means every consumer gets the full
    field set, not whichever subset its particular caller happened to add
    over time.

    Only includes business tasks that go through the wrapper (@task.sfn,
    GlueTask, ECSTask, AthenaTask, EMRTask, BatchTask) plus directly-tracked
    steps (Glue/ECS/Athena run outside the wrapper too, via `dag.steps`).
    Excludes infrastructure steps (Wait, Pass, SNS, etc.) — those don't
    represent a "task" a user would want to see as a graph node.
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    for t in dag.tasks:
        node: Dict[str, Any] = {
            "id": t.task_id,
            "name": t.task_id,
            "type": "task",
            "task_type": t.task_type,
        }
        if t.wait_before_seconds > 0:
            node["wait_before"] = t.wait_before_seconds
        if t.trigger_rule and t.trigger_rule != 'all_success':
            node["trigger_rule"] = t.trigger_rule
        if t.outlets:
            node["outlets"] = [_serialize_outlet(a) for a in t.outlets]
        if t.inlets:
            node["inlets"] = [_serialize_inlet(a) for a in t.inlets]
        if t.wait_for:
            wait_for_assets = _serialize_wait_for_metadata(t.wait_for)
            if wait_for_assets:
                node["wait_for"] = wait_for_assets
        if t.skip_on_backfill:
            node["skip_on_backfill"] = True
        task_config, _ = _build_task_config_and_arn(t)
        if task_config:
            node["task_config"] = task_config
        nodes.append(node)

        for dep in t.dependencies:
            edges.append({"from": dep.node_id, "to": t.task_id})

    for s in dag.steps:
        if s.step_type in TRACKED_STEP_TYPES:
            nodes.append({"id": s.step_id, "name": s.step_id, "type": s.step_type})
            for dep in s.dependencies:
                edges.append({"from": dep.node_id, "to": s.step_id})

    return nodes, edges


def _build_pipeline_metadata(dag: DAG) -> Tuple[List, Dict, Dict]:
    """Build tasks_metadata, dag_metadata, and asset_schedule.
    
    Pure data transformation — no I/O, no side effects.
    
    Returns:
        Tuple of (tasks_metadata, dag_metadata, asset_schedule)
    """
    # Tasks metadata for registration
    tasks_metadata = []
    for task in dag.tasks:
        task_meta: Dict[str, Any] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "dependencies": [d.node_id for d in task.dependencies]
        }
        if task.skip_on_backfill:
            task_meta["skip_on_backfill"] = True
        if task.outlets:
            task_meta["outlets"] = [_serialize_outlet(a) for a in task.outlets]
        if task.inlets:
            task_meta["inlets"] = [_serialize_inlet(a) for a in task.inlets]
        if task.wait_for:
            wait_for_assets = _serialize_wait_for_metadata(task.wait_for)
            if wait_for_assets:
                task_meta["wait_for"] = wait_for_assets
        tasks_metadata.append(task_meta)
    
    # DAG visualization metadata — shared builder (see _build_dag_visualization_nodes_edges)
    _nodes, _edges = _build_dag_visualization_nodes_edges(dag)
    dag_metadata: Dict[str, Any] = {"nodes": _nodes, "edges": _edges}
    
    # Asset schedule (for asset-triggered pipelines)
    asset_schedule = {}
    if dag.is_asset_triggered and dag.asset_schedule_info:
        asset_schedule = dag.asset_schedule_info
    
    return tasks_metadata, dag_metadata, asset_schedule


def _build_registration_chain(
    dag: DAG,
    tasks_metadata: List,
    dag_metadata: Dict,
    asset_schedule: Dict,
    registry_table: str,
    tokens_table: str,
    asset_subscriptions_table: str,
) -> Tuple[Dict, str]:
    """Build the registration state chain that runs before tasks.
    
    Produces states: Define_Inputs (optional) → Register_Pipeline →
    Save_DAG_Snapshot → Register_Asset_Subscriptions (optional) →
    Check_Register_Only / Registration_Complete
    
    Args:
        dag: DAG for metadata (dag_id, description, variables, schedule)
        tasks_metadata: From _build_pipeline_metadata
        dag_metadata: From _build_pipeline_metadata
        asset_schedule: From _build_pipeline_metadata
        registry_table: DynamoDB table name for pipeline registry
        tokens_table: DynamoDB table name for tokens/snapshots
        asset_subscriptions_table: DynamoDB table name for asset subscriptions
            (consumed by the platform's notify_asset_consumers SFN — must match
            its own ${asset_subscriptions_table} substitution, both ultimately
            sourced from the same SSM parameter /polyris/{stage}/asset_subscriptions_table)
    
    Returns:
        Tuple of (states_dict, start_at_state_name)
    """
    states: Dict[str, Any] = {}
    start_at = "Register_Pipeline"
    
    # Define_Inputs if variables are defined
    if dag.variables:
        start_at = "Define_Inputs"
        variables_output = {var_name: var_expr for var_name, var_expr in dag.variables.items()}
        
        states["Define_Inputs"] = {
            "Type": "Pass",
            "Comment": "Compute pipeline variables (e.g. current_date, execution_id)",
            "Output": {
                "variables": variables_output,
                "skip_tasks": JSONATA_SKIP_TASKS,
                "register_only": JSONATA_REGISTER_ONLY,
                "_suppress_asset_event": JSONATA_SUPPRESS_ASSET_EVENT,
                "cascade_all": JSONATA_CASCADE_ALL,
                "current_date": "{% $exists($states.input.current_date) ? $states.input.current_date : null %}",
                "PARTITION_ARG": "{% $exists($states.input.PARTITION_ARG) ? $states.input.PARTITION_ARG : ($exists($states.input.current_date) ? $states.input.current_date : null) %}"
            },
            "Next": "Register_Pipeline"
        }
    
    # Register_Pipeline — registers DAG metadata on each execution
    states["Register_Pipeline"] = {
        "Type": "Task",
        "Comment": "Register pipeline metadata for UI and asset triggers",
        "Resource": "arn:aws:states:::dynamodb:putItem",
        "Arguments": {
            "TableName": registry_table,
            "Item": {
                "pipeline_name": {"S": dag.dag_id},
                "sfn_arn": {"S": JSONATA_SFN_ARN},
                "description": {"S": dag.description or ""},
                "pipeline_group": {"S": dag.group or ""},
                "tasks": {"S": json.dumps(tasks_metadata)},
                "dag": {"S": json.dumps(dag_metadata)},
                "schedule": {"S": dag.schedule if isinstance(dag.schedule, str) else ""},
                "asset_schedule": {"S": json.dumps(asset_schedule)},
                "registered_at": {"S": JSONATA_NOW},
                "last_execution": {"S": JSONATA_EXECUTION_NAME}
            }
        },
        "Output": JSONATA_PASS_INPUT,
        "Next": "Save_DAG_Snapshot"
    }
    
    # Save DAG snapshot per execution — ensures UI shows correct DAG for each run
    # even after pipeline definition changes (key = dag_snapshot::{execution_name})
    states["Save_DAG_Snapshot"] = {
        "Type": "Task",
        "Comment": "Snapshot DAG structure for this execution (survives redeploys)",
        "Resource": "arn:aws:states:::dynamodb:putItem",
        "Arguments": {
            "TableName": tokens_table,
            "Item": {
                "execution_name": {"S": "{% 'dag_snapshot::' & $states.context.Execution.Name %}"},
                "pipeline_name": {"S": dag.dag_id},
                "pipeline_execution": {"S": JSONATA_EXECUTION_NAME},
                "dag": {"S": json.dumps(dag_metadata)},
                "snapshot_at": {"S": JSONATA_NOW},
                "ttl": {"N": "{% $string($floor($toMillis($now()) / 1000) + 10368000) %}"}
            }
        },
        "Output": JSONATA_PASS_INPUT,
        "Next": "Register_Asset_Subscriptions" if asset_schedule.get("assets") else "Check_Register_Only"
    }
    
    # Asset subscription registration for asset-triggered pipelines
    if asset_schedule.get("assets"):
        states["Register_Asset_Subscriptions"] = {
            "Type": "Map",
            "Comment": "Register subscription for each asset (enables Query instead of Scan)",
            "Items": asset_schedule["assets"],
            "MaxConcurrency": 10,
            "ItemProcessor": {
                "ProcessorConfig": {"Mode": "INLINE"},
                "StartAt": "WriteSubscription",
                "States": {
                    "WriteSubscription": {
                        "Type": "Task",
                        "Resource": "arn:aws:states:::dynamodb:putItem",
                        "Arguments": {
                            "TableName": asset_subscriptions_table,
                            "Item": {
                                "asset_name": {"S": "{% $states.input %}"},
                                "pipeline_name": {"S": dag.dag_id},
                                "sfn_arn": {"S": JSONATA_SFN_ARN},
                                "operator": {"S": asset_schedule.get("operator", "AND")},
                                "assets": {"S": json.dumps(asset_schedule["assets"])},
                                "registered_at": {"S": JSONATA_NOW}
                            }
                        },
                        "Output": "{% null %}",
                        "End": True
                    }
                }
            },
            "Output": JSONATA_PASS_INPUT,
            "Next": "Check_Register_Only"
        }
    
    # Check if this is a registration-only run
    states["Check_Register_Only"] = {
        "Type": "Choice",
        "Comment": "Skip task execution if register_only=true (used by polyris-register CLI)",
        "Choices": [{
            "Condition": "{% $states.input.register_only = true %}",
            "Next": "Registration_Complete"
        }],
        "Default": "Run_All_Tasks"
    }
    
    states["Registration_Complete"] = {
        "Type": "Succeed",
        "Comment": "Registration completed without running tasks"
    }
    
    return states, start_at


def generate_step_function_json(
    dag: DAG, 
    dependency_wrapper_arn: Optional[str] = None,
    wrapper_arn: Optional[str] = None,
    registry_table: Optional[str] = None,
    tokens_table: Optional[str] = None,
    asset_subscriptions_table: Optional[str] = None,
) -> str:
    """
    Generate AWS Step Functions JSON from DAG.
    
    Args:
        dag: DAG to generate JSON for
        dependency_wrapper_arn: (deprecated) Use wrapper_arn instead
        wrapper_arn: ARN of wrapper state machine (default: ${wrapper_arn})
        registry_table: Name of pipeline registry table (default: ${registry_table})
        tokens_table: Name of pipeline tokens table (default: ${tokens_table})
        asset_subscriptions_table: Name of the asset subscriptions table that
            asset-triggered pipelines register into (default:
            ${asset_subscriptions_table}). Must match the platform's own
            AssetSubscriptionsTable — both are sourced from the SSM parameter
            /polyris/{stage}/asset_subscriptions_table (see deploy.py).
    
    Features:
    - variables: Creates Define_Inputs Pass state at start
    - wait_before: Adds Wait state before task in branch
    - Passes context (variables, skip_tasks, current_date) to all tasks
    - Supports all Step types (Lambda, DynamoDB, SNS, S3, etc.)
    - Supports all Task types (sfn, lambda, glue, ecs, athena, emr, batch)
    
    Step types:
    - Wrapper steps (lambda/glue/ecs/athena): Full dependency support via wrapper
    - Direct steps (wait/pass/sns/sqs/s3/...): Run in parallel, NO dependency support
    """
    # Handle deprecated parameter
    _wrapper_arn = wrapper_arn or dependency_wrapper_arn or "${wrapper_arn}"
    _registry_table = registry_table or "${registry_table}"
    _tokens_table = tokens_table or "${tokens_table}"
    _asset_subscriptions_table = asset_subscriptions_table or "${asset_subscriptions_table}"
    
    # 1. Build parallel branches (one per task/step)
    branches = []
    for item in list(dag.tasks) + list(dag.steps):
        if isinstance(item, Task):
            branches.append(_build_task_branch(item, dag, _wrapper_arn))
        elif isinstance(item, Step):
            branches.append(_build_step_branch(item, dag, _wrapper_arn))
    
    # 2. Build metadata (tasks, DAG viz, asset schedule)
    tasks_metadata, dag_metadata, asset_schedule = _build_pipeline_metadata(dag)
    
    # 3. Build registration state chain
    states, start_at = _build_registration_chain(
        dag, tasks_metadata, dag_metadata, asset_schedule,
        _registry_table, _tokens_table, _asset_subscriptions_table,
    )
    
    # 4. Add parallel execution + failure handler
    states["Run_All_Tasks"] = {
        "Type": "Parallel",
        "Branches": branches,
        "End": True,
        "Catch": [{
            "ErrorEquals": ["States.ALL"],
            "Next": "Pipeline_Failed"
        }]
    }
    states["Pipeline_Failed"] = {
        "Type": "Fail",
        "Error": "PipelineFailed",
        "Cause": "Infrastructure error - wrapper did not handle"
    }
    
    # 5. Assemble definition
    registration_metadata = {
        "dag_id": dag.dag_id,
        "description": dag.description or "",
        "group": dag.group or "",
        "schedule": dag.schedule if isinstance(dag.schedule, str) else None,
        "asset_schedule": asset_schedule if asset_schedule else None,
        "tasks": tasks_metadata,
        "dag": dag_metadata,
        "polyris_version": "1.0"
    }
    
    definition = {
        "Comment": json.dumps(registration_metadata),
        "QueryLanguage": "JSONata",
        "StartAt": start_at,
        "States": states
    }
    
    return json.dumps(definition, indent=2)


def generate_dag_json(dag: DAG) -> Dict:
    """Generate DAG visualization JSON.

    See _build_dag_visualization_nodes_edges for exactly what's included
    (business tasks + tracked Glue/ECS/Athena steps; excludes infrastructure
    steps like Wait/Pass/SNS).
    """
    nodes, edges = _build_dag_visualization_nodes_edges(dag)
    return {"name": dag.dag_id, "nodes": nodes, "edges": edges}


def generate_dag_hash(dag: DAG) -> str:
    """Generate deterministic hash of DAG structure for change detection.
    
    Used by PipelineRegistration dynamic provider to skip re-registration
    when DAG hasn't changed between deploys.
    """
    import hashlib
    dag_json = generate_dag_json(dag)
    asset_info = dag.asset_schedule_info or {}
    hashable = json.dumps({"dag": dag_json, "assets": asset_info}, sort_keys=True)
    return hashlib.sha256(hashable.encode()).hexdigest()[:8]


def generate_mermaid(dag: DAG) -> str:
    """Generate Mermaid diagram.
    
    Only includes business tasks (same filtering as generate_dag_json).
    """
    lines = ["graph LR"]
    
    # Add task edges
    for task in dag.tasks:
        for dep in task.dependencies:
            dep_id = dep.node_id
            lines.append(f"    {dep_id} --> {task.task_id}")
    
    # Add only tracked step edges
    for step in dag.steps:
        if step.step_type in TRACKED_STEP_TYPES:
            for dep in step.dependencies:
                dep_id = dep.node_id
                lines.append(f"    {dep_id} --> {step.step_id}")
    
    # If no dependencies, just list items
    if len(lines) == 1:
        for task in dag.tasks:
            lines.append(f"    {task.task_id}")
        for step in dag.steps:
            if step.step_type in TRACKED_STEP_TYPES:
                lines.append(f"    {step.step_id}")
    
    return "\n".join(lines)


def generate_eventbridge_schedule(dag: DAG) -> Optional[Dict]:
    """Generate EventBridge Scheduler configuration."""
    if not dag._eventbridge_schedule:
        return None
    
    return {
        "Name": f"{dag.dag_id}-schedule",
        "ScheduleExpression": dag._eventbridge_schedule,
        "State": "DISABLED" if dag.is_paused_upon_creation else "ENABLED",
        "Target": {
            "Arn": "${sfn_arn}",
            "RoleArn": "${scheduler_role_arn}",
            "Input": json.dumps({
                "triggered_by": "schedule",
                "schedule": dag.schedule
            })
        }
    }


# ============================================
# Asset-based Orchestration Generation
# ============================================

def _iter_dag_assets(dag: DAG) -> Iterator[Tuple[str, Asset, str]]:
    """Yield ``(task_id, asset, role)`` for all Asset outlets/inlets in a DAG.

    Shared iteration logic used by both single-DAG and multi-DAG asset generators.
    """
    for t in dag.tasks:
        for asset in t.outlets:
            if isinstance(asset, Asset):
                yield t.task_id, asset, "outlet"
        for asset in t.inlets:
            if isinstance(asset, Asset):
                yield t.task_id, asset, "inlet"


def generate_assets_json(dag: DAG) -> str:
    """
    Generate JSON with asset information from DAG.
    
    Returns JSON with:
    - All assets referenced in this DAG (outlets + inlets)
    - Producer/consumer relationships
    - Schedule information (if asset-triggered)
    
    Example output:
    {
        "dag_id": "sephora-daily",
        "is_asset_triggered": false,
        "schedule_info": null,
        "assets": [
            {
                "name": "raw/inventory",
                "uri": "s3://bucket/...",
                "group": "raw",
                "role": "outlet",
                "task": "get_inventory"
            }
        ]
    }
    """
    assets = []
    seen_assets = set()
    
    for task_id, asset, role in _iter_dag_assets(dag):
        if asset.name not in seen_assets:
            seen_assets.add(asset.name)
            assets.append({
                "name": asset.name,
                "uri": asset.uri,
                "group": asset.group,
                "description": asset.description,
                "role": role,
                "task": task_id,
            })
    
    result = {
        "dag_id": dag.dag_id,
        "is_asset_triggered": dag.is_asset_triggered,
        "schedule_info": dag.asset_schedule_info,
        "assets": assets
    }
    
    return json.dumps(result, indent=2)


def generate_all_assets(dags: List[DAG]) -> Dict[str, Any]:
    """
    Generate complete asset registry from multiple DAGs.
    
    Aggregates all assets across DAGs with:
    - Full producer/consumer lineage
    - Cross-DAG dependencies
    
    Returns:
    {
        "assets": {
            "raw/inventory": {
                "uri": "s3://...",
                "group": "raw",
                "producers": ["sephora-daily.get_inventory"],
                "consumers": ["processing-daily.process_inventory"]
            }
        },
        "dag_triggers": {
            "processing-daily": {
                "operator": "AND",
                "assets": ["raw/inventory", "raw/catalog"]
            }
        }
    }
    """
    assets: Dict[str, Dict[str, Any]] = {}  # name -> asset info
    dag_triggers = {}  # dag_id -> schedule info
    
    for dag in dags:
        for task_id, asset, role in _iter_dag_assets(dag):
            if asset.name not in assets:
                assets[asset.name] = {
                    "uri": asset.uri,
                    "group": asset.group,
                    "description": asset.description,
                    "producers": [],
                    "consumers": [],
                }
            target = "producers" if role == "outlet" else "consumers"
            qualified_id = f"{dag.dag_id}.{task_id}"
            if qualified_id not in assets[asset.name][target]:
                assets[asset.name][target].append(qualified_id)
        
        # Collect DAG trigger info
        if dag.is_asset_triggered:
            dag_triggers[dag.dag_id] = dag.asset_schedule_info
    
    return {
        "assets": assets,
        "dag_triggers": dag_triggers
    }



def render_dag_ascii(dag):
    """Render DAG as ASCII graph in terminal."""
    tasks = dag.tasks
    if not tasks:
        return "  (empty DAG)"

    task_map = {t.task_id: t for t in tasks}
    downstream_map = {t.task_id: [] for t in tasks}
    for t in tasks:
        for dep in t.dependencies:
            downstream_map[dep.task_id].append(t.task_id)

    # Assign layers via topological sort
    in_deg = {t.task_id: len(t.dependencies) for t in tasks}
    layers = []
    remaining = set(t.task_id for t in tasks)

    while remaining:
        layer = sorted([tid for tid in remaining if in_deg[tid] == 0])
        if not layer:  # pragma: no cover -- only reachable for a cyclic graph; validation rejects cycles before ascii rendering
            layer = sorted(remaining)
            remaining = set()
        else:
            for tid in layer:
                remaining.discard(tid)
                for d in downstream_map[tid]:
                    in_deg[d] -= 1
        layers.append(layer)

    # Build chains: trace each root through its longest path
    assigned = set()

    def trace_chain(start):
        chain = [start]
        current = start
        while downstream_map[current]:
            candidates = [d for d in downstream_map[current] if d not in assigned]
            if not candidates:
                break
            best = None
            for c in candidates:
                if len(task_map[c].dependencies) == 1:
                    best = c
                    break
            if not best:
                best = candidates[0]
            chain.append(best)
            assigned.add(best)
            current = best
        return chain

    roots = [t.task_id for t in tasks if not t.dependencies]
    chains = []
    for root in roots:
        assigned.add(root)
        chains.append(trace_chain(root))

    for t in tasks:
        if t.task_id not in assigned:  # pragma: no cover -- every task in a valid DAG traces back to a root; only cyclic graphs leave tasks unassigned
            assigned.add(t.task_id)
            chains.append(trace_chain(t.task_id))

    # Layer index for each task
    layer_of = {}
    for li, layer in enumerate(layers):
        for tid in layer:
            layer_of[tid] = li

    col_width = max(len(tid) for t in tasks for tid in [t.task_id]) + 1
    arrow = " → "

    lines = []
    lines.append(f"  DAG: {dag.dag_id}")
    lines.append(f"  Schedule: {dag.schedule or 'None'}")
    lines.append(f"  Tasks: {len(tasks)}")
    lines.append("")

    for chain in chains:
        row_parts = []
        prev_col = -1
        for tid in chain:
            col = layer_of[tid]
            gap = col - prev_col - 1
            for _ in range(gap):  # pragma: no cover -- trace_chain follows single-dependency children, so a chain never skips a layer in practice
                row_parts.append(f"{'':>{col_width}}{' ' * len(arrow)}")
            if prev_col >= 0:
                row_parts.append(arrow)
            name = tid
            t = task_map[tid]
            if hasattr(t, 'trigger_rule') and t.trigger_rule and t.trigger_rule != 'all_success':
                name = f"{tid} ⟨{t.trigger_rule}⟩"
            row_parts.append(f"{name:<{col_width}}")
            prev_col = col
        lines.append("  " + "".join(row_parts).rstrip())

    # Show convergence points
    convergent = [t for t in tasks if len(t.dependencies) > 1]
    if convergent:
        lines.append("")
        for t in convergent:
            deps = [d.task_id for d in t.dependencies]
            lines.append(f"  {' + '.join(deps)} ──→ {t.task_id}")

    # Show non-default trigger rules
    trigger_info = [(t.task_id, t.trigger_rule) for t in tasks
                    if hasattr(t, 'trigger_rule') and t.trigger_rule and t.trigger_rule != 'all_success']
    if trigger_info:
        lines.append("")
        for tid, rule in trigger_info:
            lines.append(f"  ⚙ {tid}: trigger_rule={rule}")

    return "\n".join(lines)

