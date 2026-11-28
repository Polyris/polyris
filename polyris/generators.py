"""
Generators for SFN-DSL.

This module contains all the generate_* functions:
- generate_step_function_json: Generate Step Functions JSON
- generate_dag_json: Generate DAG structure JSON
- generate_dag_hash: Deterministic hash for DAG change detection
- generate_mermaid: Generate Mermaid diagram
- generate_assets_json: Generate asset information JSON
- validate_asl: Validate Amazon States Language JSON
- generate_debug_info: Generate debug information for troubleshooting
"""

from typing import Dict, Any, List, Tuple, Iterator, Optional, Callable, Set
import json

from .constants import TaskConfigKey, TaskSpecKey, WarningKind
from .config import config
from .dag import DAG
from .task import Task
from .steps import (
    Step, Wait, Pass, Succeed, LambdaTask, DynamoDBTask, SNSTask, SQSTask,
    S3Task, GlueTask, AthenaTask, ECSTask, EventBridgeTask, BedrockTask, HttpTask,
)
from .assets import Asset, AssetRef, AssetConsecutiveRef, AssetAll, AssetAny, _flatten_asset_names
from .schema import column_to_dict
from .coordination import retention, run_row, task_spec
from .coordination import start_run as start_run_contract
from .coordination.start_run import Arg, RunArg
from . import limits


# =============================================================================
# JSONata Expression Constants
# =============================================================================
# Used in task and step inputs for date/partition resolution (ADR #133).
#
# A run has one date, the same for every task: the date it was started for
# (variables.current_date or current_date: the console, an asset trigger), else
# the UTC day its schedule fired it for (SCHEDULED_TIME_FIELD, filled in by EventBridge
# Scheduler), else the UTC day the execution started. Never $now(): each task
# builds its input at its own moment, so a run crossing midnight would get two
# dates, and a run scheduled at 23:59 that starts at 00:01 the next day's.
# Values are tested, not presence: Define_Inputs passes absent fields on as null,
# and $exists(null) is true.

#: The field of a scheduled run's input that holds the time its schedule fired it
#: for. deploy.py puts EventBridge Scheduler's <aws.scheduler.scheduled-time> there.
SCHEDULED_TIME_FIELD = "scheduled_time"

_RUN_DATE = (
    "($states.input.current_date ? $states.input.current_date "
    f": ($states.input.{SCHEDULED_TIME_FIELD} ? $substringBefore($states.input.{SCHEDULED_TIME_FIELD}, 'T') "
    ": $substringBefore($states.context.Execution.StartTime, 'T')))"
)

_DATE_FALLBACK = (
    "($states.input.variables.current_date ? $states.input.variables.current_date "
    f": {_RUN_DATE})"
)

JSONATA_DATE = f"{{% {_DATE_FALLBACK} %}}"

JSONATA_PARTITION = (
    "{% $states.input.variables.PARTITION_ARG "
    "? $states.input.variables.PARTITION_ARG "
    ": ($states.input.PARTITION_ARG "
    "? $states.input.PARTITION_ARG "
    f": {_DATE_FALLBACK}) %}}"
)

# Common JSONata expressions used by the registration states and Start_Run's payload
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
JSONATA_TRIGGERED_BY = "{% $exists($states.input.triggered_by) ? $states.input.triggered_by : 'unknown' %}"
WRAPPER_STEP_TYPES = frozenset({'lambda', 'glue', 'ecs', 'athena'})

# Step types tracked in DAG visualization (business logic, not infrastructure)
TRACKED_STEP_TYPES = frozenset({'glue', 'ecs', 'athena'})

# Standard retry for AWS SDK integration states (DynamoDB throttling / transient errors).
# Used by all DynamoDB write states in _build_registration_chain.
_STANDARD_DYNAMODB_RETRY: Dict[str, Any] = {
    "ErrorEquals": [
        "DynamoDB.ProvisionedThroughputExceededException",
        "DynamoDB.ThrottlingException",
        "DynamoDB.InternalServerError",
        "States.Timeout"
    ],
    "IntervalSeconds": 1,
    "MaxAttempts": 3,
    "BackoffRate": 2,
    "JitterStrategy": "FULL"
}


def _warning_item(subject: str, task_name: str, dag_id: str, error: str, *,
                  kind: str = WarningKind.COORDINATION, task_date: str = JSONATA_DATE) -> Dict[str, Any]:
    """A DynamoDB-typed warning row, the shape of records.warning_item (ADR #130 §9).

    `subject` and `error` are JSONata fragments. The row carries no `date`:
    the bell finds it through the attention index by write time, whatever the
    partition date of the run (B-N1). A contract test compares the two shapes.
    """
    return {
        "execution_name": {"S": f"{{% '_notify_warn_{kind}_' & {subject} %}}"},
        "kind": {"S": kind},
        "task_name": {"S": task_name},
        "pipeline_name": {"S": dag_id},
        "pipeline_execution": {"S": JSONATA_EXECUTION_NAME},
        "task_date": {"S": task_date},
        "status": {"S": "failed"},
        "error": {"S": f"{{% {error} %}}"},
        "finished_at": {"S": JSONATA_NOW},
        "attention": {"S": f"{{% 'warn#' & $substring($hash({subject}, 'SHA-256'), 0, 1) %}}"},
        "attention_at": {"S": JSONATA_NOW},
        "ttl": {"N": "{% $string($floor($toMillis($now()) / 1000) + 8 * 86400) %}"},
    }


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
        else:
            raise TypeError(
                f"Unrecognised wait_for entry type: {type(item)!r}. "
                f"Expected Asset, AssetRef, AssetConsecutiveRef, AssetAll, or AssetAny."
            )
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
        else:
            raise TypeError(
                f"Unrecognised wait_for entry type: {type(item)!r}. "
                f"Expected Asset, AssetRef, AssetConsecutiveRef, AssetAll, or AssetAny."
            )
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

#: The state types that take Retry and Catch; AWS rejects either field on any other type.
ERROR_HANDLING_TYPES = frozenset({"Task", "Parallel", "Map"})


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

    # 0. Size guard — measured as polyris-deploy sends the definition (indented, as
    # generate_step_function_json writes it): measured compact, 230 tasks validated at
    # 937 KB and deployed at 1.11 MB (ADR #137 §8).
    sent = json.dumps(asl, indent=2)
    errors.extend(limits.definition_errors(sent))
    warnings.extend(limits.definition_warnings(sent))

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
        
        if state_type not in ERROR_HANDLING_TYPES:
            for field in ("Retry", "Catch"):
                if field in state:
                    errors.append(f"State '{state_name}': Type '{state_type}' does not take '{field}'")

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
            "execution_timeout": task.execution_timeout_seconds,
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
    if step.seconds is not None:
        state["Seconds"] = step.seconds
    elif step.timestamp is not None:
        state["Timestamp"] = step.timestamp
    elif step.timestamp_path is not None:
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
        "Resource": _DYNAMODB_RESOURCES[step.operation],
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
        "Resource": _S3_RESOURCES[step.operation],
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


def _add_retry_config(
    task_config: Dict[TaskConfigKey, Any],
    retries: int,
    retry_delay: int,
    retry_exponential_backoff: bool = False,
    max_retry_delay: Optional[int] = None,
    retry_jitter: bool = False,
) -> None:
    """Thread retry policy fields into task_config in-place.

    Single source of truth for the retry contract: called by both
    _build_task_config_and_arn (Task path) and _build_step_config_and_arn
    (wrapper-step path) so they cannot drift apart.
    """
    if not retries:
        return
    task_config[TaskConfigKey.RETRIES] = retries
    task_config[TaskConfigKey.RETRY_DELAY] = retry_delay
    if retry_exponential_backoff:
        task_config[TaskConfigKey.RETRY_BACKOFF] = True
        if max_retry_delay is not None:
            task_config[TaskConfigKey.MAX_RETRY_DELAY] = max_retry_delay
    if retry_jitter:
        task_config[TaskConfigKey.RETRY_JITTER] = True


def _build_task_config_and_arn(task: Task) -> Tuple[Dict[TaskConfigKey, Any], str]:
    """Build task_config (task-type-specific settings + retry policy) and
    resolve task_arn for task types (lambda) that derive it from a
    different field than task.arn.

    Returns `str`, never None: Task.arn is `str = ""` and the single Task
    constructor normalizes with `arn or ""`. Task types addressed by config
    rather than ARN (glue/ecs/athena/emr/batch take job_name / cluster /
    query_string) legitimately carry the empty string, which is the shape the
    generated ASL expects — not a missing value.

    Shared between the task's spec (_task_spec, which Start_Run's rows and
    run_task's input come from) and _build_dag_visualization_nodes_edges.
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
            task_config[TaskConfigKey.NUMBER_OF_WORKERS] = int(task.number_of_workers)
        if task.allocated_capacity is not None:
            task_config[TaskConfigKey.ALLOCATED_CAPACITY] = task.allocated_capacity
        if task.max_capacity is not None:
            task_config[TaskConfigKey.MAX_CAPACITY] = float(task.max_capacity)
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
    # (ADR #107, retry loop). When absent, the wrapper's Check_Should_Retry defaults to 0, so
    # no-retry tasks keep their existing contract untouched (including sfn's empty
    # one) and incur no snapshot churn; sfn tasks with retries opt in uniformly.
    _add_retry_config(
        task_config,
        task.retries,
        task.retry_delay_seconds,
        task.retry_exponential_backoff,
        task.max_retry_delay_seconds,
        task.retry_jitter,
    )

    return task_config, task_arn


def _build_step_config_and_arn(step: Step) -> Tuple[Dict[TaskConfigKey, Any], str]:
    """task_config and task_arn of a wrapper step (lambda/glue/ecs/athena), as for a Task.

    Its spec (_step_spec) records it, so a wrapper step runs as a task row with
    a task's configuration (ADR #137 §1).
    """
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
    else:
        raise ValueError(f"step '{step.step_id}' ({step.step_type}) does not run as a task row")

    # Include retry policy from step (same contract as _build_task_config_and_arn for Task).
    # LambdaTask(Step) uses retry_interval; other wrapper step types currently carry no
    # retry fields, so getattr defaults keep them out of the config.
    _add_retry_config(
        task_config,
        getattr(step, 'retries', 0),
        getattr(step, 'retry_interval', 1),
    )

    # For lambda, task_arn is function_arn
    task_arn = getattr(step, 'function_arn', '') or getattr(step, 'task_arn', '') or step.step_id
    return task_config, task_arn


def _resolved_role(task: Task) -> str:
    """The task's cross_account_role: a config.roles key resolved to its ARN, anything else as written."""
    if task.role and task.role != 'same' and task.role in config.roles:
        return config.roles[task.role]
    return task.role


#: The key of a branch's output that records its error. After the Parallel, the pipeline fails its
#: execution with the recorded errors, so a failed branch never cancels the others (ADR #137 §1).
BRANCH_ERROR = "_polyris_branch_error"
#: Step Functions' cap on a Fail state's cause, in characters.
_CAUSE_MAX = 32768
_JSONATA_EXECUTION_START = "{% $states.context.Execution.StartTime %}"
_KEEP_INPUT = "{% $states.input %}"


#: The run's date in a warning written after a Catch: the date ``_branch_error`` recorded from the state's input.
_BRANCH_ERROR_DATE = f"{{% $states.input.{BRANCH_ERROR}.date %}}"


def _branch_error(branch: str) -> Dict[str, Any]:
    """A Catch's Output that records the caught error under BRANCH_ERROR, naming its branch, with the run's date
    read from the state's input: the states after the Catch read the error, not the run's input."""
    return {BRANCH_ERROR: {
        "branch": branch,
        "error": "{% $states.errorOutput.Error %}",
        "cause": "{% $exists($states.errorOutput.Cause) ? $states.errorOutput.Cause : '' %}",
        "date": JSONATA_DATE,
    }}


def _check_dependencies(dag: DAG) -> None:
    """Direct steps are branches, not rows: they have no dependencies, and no task or wrapper step
    depends on one, since nothing would ever record that it finished."""
    def is_direct(node: Any) -> bool:
        return isinstance(node, Step) and node.step_type not in WRAPPER_STEP_TYPES

    for task in dag.tasks:
        direct = [d.node_id for d in task.dependencies if is_direct(d)]
        if direct:
            raise ValueError(
                f"Task '{task.task_id}' depends on direct step(s): {direct}. "
                f"Direct steps (wait/pass/sns/sqs/s3/...) do not support dependencies. "
                f"Use @task with wait_before parameter instead."
            )
    for step in dag.steps:
        if step.step_type in WRAPPER_STEP_TYPES:
            direct = [d.node_id for d in step.dependencies if is_direct(d)]
            if direct:
                raise ValueError(
                    f"Wrapper step '{step.step_id}' ({step.step_type}) depends on direct step(s): {direct}. "
                    f"Direct steps (wait/pass/sns/sqs/s3/...) don't register in DynamoDB and cannot be dependencies. "
                    f"Use wait_before parameter or restructure your DAG."
                )
        elif step.dependencies:
            raise ValueError(
                f"Direct step '{step.step_id}' ({step.step_type}) cannot have dependencies: "
                f"{[d.node_id for d in step.dependencies]}. "
                f"Only wrapper steps (lambda/glue/ecs/athena) support dependencies. "
                f"Use @task with wait_before parameter instead."
            )


def _start_run_branch(dag: DAG, specs: task_spec.Version, start_run_function_arn: str, tokens_table: str) -> Dict:
    """The branch that runs the pipeline's tasks (ADR #137 §1).

    Start_Run writes the run's rows and returns; the state waits for the run's
    token, which the dispatcher answers once every row is final. A failed run's
    answer is recorded without a warning: its tasks failed visibly. Any other
    failure starts no task, so it also writes a `coordination` warning.
    """
    payload = {
        Arg.FORMAT_VERSION: task_spec.FORMAT_VERSION,
        Arg.SPEC_ID: specs.spec_id,
        Arg.TASK_COUNT: specs.task_count,
        Arg.PIPELINE_NAME: dag.dag_id,
        Arg.EXECUTION_NAME: JSONATA_EXECUTION_NAME,
        Arg.EXECUTION_ARN: "{% $states.context.Execution.Id %}",
        Arg.EXECUTION_START: _JSONATA_EXECUTION_START,
        Arg.TOKEN: JSONATA_TOKEN,
        Arg.RETRY_COUNT: "{% $states.context.State.RetryCount %}",
        Arg.REDRIVE_COUNT: ("{% $exists($states.context.Execution.RedriveCount) "
                            "? $states.context.Execution.RedriveCount : 0 %}"),
        Arg.DATE: JSONATA_DATE,
        Arg.INPUT: {
            RunArg.VARIABLES: JSONATA_VARIABLES,
            RunArg.SKIP_TASKS: JSONATA_SKIP_TASKS,
            RunArg.CASCADE_ALL: JSONATA_CASCADE_ALL,
            RunArg.SUPPRESS_ASSET_EVENT: JSONATA_SUPPRESS_ASSET_EVENT,
            RunArg.TRIGGERED_BY: JSONATA_TRIGGERED_BY,
            RunArg.PARTITION_ARG: JSONATA_PARTITION,
            # From the execution's own input: Define_Inputs passes on only the fields it lists.
            RunArg.BACKFILL_ID: ("{% $exists($states.context.Execution.Input.backfill_id) "
                                 "? $states.context.Execution.Input.backfill_id : null %}"),
        },
    }
    error = _branch_error("Start_Run")
    return {"StartAt": "Start_Run", "States": {
        "Start_Run": {
            "Type": "Task",
            "Comment": "Write the run's rows; the dispatcher answers this token once every row is final",
            "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
            "Arguments": {"FunctionName": start_run_function_arn, "Payload": payload},
            "Output": {},
            "Retry": [{
                "ErrorEquals": list(start_run_contract.RETRY_ERRORS),
                "IntervalSeconds": int(start_run_contract.RETRY_INTERVAL.total_seconds()),
                "MaxAttempts": start_run_contract.RETRY_ATTEMPTS,
                "BackoffRate": start_run_contract.RETRY_BACKOFF,
                "MaxDelaySeconds": int(start_run_contract.RETRY_MAX_DELAY.total_seconds()),
                "JitterStrategy": "FULL",
            }],
            "Catch": [
                {"ErrorEquals": [run_row.FAILED_RUN_ERROR], "Output": error, "Next": "Run_Ended_Failed"},
                {"ErrorEquals": ["States.DataLimitExceeded"], "Output": error, "Next": "Warn_Start_Run_Failed"},
                {"ErrorEquals": ["States.ALL"], "Output": error, "Next": "Warn_Start_Run_Failed"},
            ],
            "End": True,
        },
        "Warn_Start_Run_Failed": {
            "Type": "Task",
            "Comment": "No task of the run started: show why in the console bell",
            "Resource": "arn:aws:states:::dynamodb:putItem",
            "Arguments": {
                "TableName": tokens_table,
                "Item": _warning_item(
                    "$states.context.Execution.Name & '-start_run'", "Start_Run", dag.dag_id,
                    f"'Start_Run failed, so no task of the run started: ' & $states.input.{BRANCH_ERROR}.error "
                    f"& ': ' & $states.input.{BRANCH_ERROR}.cause", task_date=_BRANCH_ERROR_DATE),
            },
            "Output": _KEEP_INPUT,
            "Retry": [_STANDARD_DYNAMODB_RETRY],
            "Catch": [{"ErrorEquals": ["States.ALL"], "Output": _KEEP_INPUT, "Next": "Run_Ended_Failed"}],
            "Next": "Run_Ended_Failed",
        },
        "Run_Ended_Failed": {"Type": "Pass", "Comment": "The error is recorded; the pipeline fails after the Parallel",
                             "End": True},
    }}


def _mark_state(step: Step, tokens_table: str, next_state: str) -> Dict[str, Any]:
    """The mark of a failed direct step on the run row (ADR #137 §1, §3).

    It carries the execution's name and StartTime, so it and Start_Run's writes
    accept each other in either order; it keeps run_version, and writes the
    history TTL when the row has none.
    """
    history = int(retention.HISTORY.total_seconds())
    return {
        "Type": "Task",
        "Comment": f"Mark the run failed by its direct step {step.step_id}",
        "Resource": "arn:aws:states:::dynamodb:updateItem",
        "Arguments": {
            "TableName": tokens_table,
            "Key": {run_row.TABLE_KEY: {"S": f"{{% '{run_row.KEY_PREFIX}' & $states.context.Execution.Name %}}"}},
            "UpdateExpression": "SET #mark = :mark, #execution = :execution, #run_start = :run_start, "
                                "#ttl = if_not_exists(#ttl, :ttl)",
            "ConditionExpression": "attribute_not_exists(#execution) OR "
                                   "(#execution = :execution AND #run_start = :run_start)",
            "ExpressionAttributeNames": {"#mark": run_row.STEP_FAILURE, "#execution": run_row.EXECUTION,
                                         "#run_start": run_row.RUN_START, "#ttl": run_row.TTL},
            "ExpressionAttributeValues": {
                ":mark": {"M": {
                    run_row.STEP_MARK_STEP: {"S": run_row.clip(step.step_id, run_row.STEP_MAX)},
                    run_row.STEP_MARK_ERROR: {"S": f"{{% $substring($states.input.{BRANCH_ERROR}.error, 0, "
                                                   f"{run_row.ERROR_CHARS}) %}}"},
                }},
                ":execution": {"S": JSONATA_EXECUTION_NAME},
                ":run_start": {"S": _JSONATA_EXECUTION_START},
                ":ttl": {"N": f"{{% $string($floor($toMillis($now()) / 1000) + {history}) %}}"},
            },
        },
        "Output": _KEEP_INPUT,
        # A transaction of the dispatcher checks the run row, and may conflict with this write (§7).
        "Retry": [{**_STANDARD_DYNAMODB_RETRY,
                   "ErrorEquals": _STANDARD_DYNAMODB_RETRY["ErrorEquals"] + ["DynamoDB.TransactionConflictException"]}],
        "Catch": [{"ErrorEquals": ["States.ALL"], "Output": _KEEP_INPUT, "Next": next_state}],
        "Next": next_state,
    }


def _direct_step_branch(step: Step, number: int, dag: DAG, tokens_table: str, *, marks_run: bool) -> Dict:
    """A direct step's branch (ADR #137 §1). Its failure is caught in the branch: the branch records
    the error, writes a `step` warning and, in a pipeline with tasks, marks the run row."""
    warn, mark, failed = f"Warn_Step_{number}_Failed", f"Mark_Step_{number}_Failed", f"Step_{number}_Failed"
    state = _generate_step_state(step)
    state["End"] = True
    if state["Type"] not in ERROR_HANDLING_TYPES:
        # A Wait, Pass or Succeed state takes no Catch; it fails only on a malformed definition.
        return {"StartAt": step.step_id, "States": {step.step_id: state}}
    error = _branch_error(step.step_id)
    state["Catch"] = list(state.get("Catch", [])) + [
        {"ErrorEquals": ["States.DataLimitExceeded"], "Output": error, "Next": warn},
        {"ErrorEquals": ["States.ALL"], "Output": error, "Next": warn},
    ]
    states: Dict[str, Any] = {step.step_id: state}
    states[warn] = {
        "Type": "Task",
        "Comment": f"The direct step {step.step_id} failed: show why in the console bell",
        "Resource": "arn:aws:states:::dynamodb:putItem",
        "Arguments": {
            "TableName": tokens_table,
            "Item": _warning_item(
                f"$states.context.Execution.Name & '-step-' & $states.input.{BRANCH_ERROR}.branch",
                step.step_id, dag.dag_id,
                f"'The direct step ' & $states.input.{BRANCH_ERROR}.branch & ' failed: ' "
                f"& $states.input.{BRANCH_ERROR}.error & ': ' & $states.input.{BRANCH_ERROR}.cause",
                kind=WarningKind.STEP, task_date=_BRANCH_ERROR_DATE),
        },
        "Output": _KEEP_INPUT,
        "Retry": [_STANDARD_DYNAMODB_RETRY],
        "Catch": [{"ErrorEquals": ["States.ALL"], "Output": _KEEP_INPUT, "Next": mark if marks_run else failed}],
        "Next": mark if marks_run else failed,
    }
    if marks_run:
        states[mark] = _mark_state(step, tokens_table, failed)
    states[failed] = {"Type": "Pass", "Comment": f"The error of {step.step_id} is recorded; "
                                                 "the pipeline fails after the Parallel", "End": True}
    return {"StartAt": step.step_id, "States": states}


def _build_dag_visualization_nodes_edges(dag: DAG) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build the canonical (nodes, edges) pair for DAG visualization.

    Single source of truth for what a "node" looks like in visualization JSON —
    used by generate_dag_json (the public polyris-output/polyris-validate
    surface). Until 1.3 the registry and each run's snapshot stored a second
    copy (ADR #137 §8: the console draws a run from its spec version now).

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


def _asset_schedule(dag: DAG) -> Dict:
    """The schedule of an asset-triggered pipeline, as the registry stores it; {} for any other."""
    if dag.is_asset_triggered and dag.asset_schedule_info:
        return dag.asset_schedule_info
    return {}


def _task_spec(task: Task) -> Dict[str, Any]:
    """A task's spec (ADR #137 §8): everything needed to run the task and to draw it.

    Keys are ``TaskSpecKey`` values. An optional key is absent when the task does not
    set it, so a spec says only what the task declares: no execution_timeout means no
    limit on the work, no orchestration_timeout no cap on the wait (§5).
    """
    task_config, task_arn = _build_task_config_and_arn(task)
    spec: Dict[str, Any] = {
        TaskSpecKey.TASK_ID: task.task_id,
        TaskSpecKey.TASK_TYPE: task.task_type,
        TaskSpecKey.TASK_ARN: task_arn,
        TaskSpecKey.TASK_CONFIG: task_config,
        TaskSpecKey.CROSS_ACCOUNT_ROLE: _resolved_role(task),
        TaskSpecKey.DEPENDENCIES: [d.node_id for d in task.dependencies],
        TaskSpecKey.TRIGGER_RULE: task.trigger_rule or "all_success",
    }
    if task.execution_timeout_seconds is not None:
        spec[TaskSpecKey.EXECUTION_TIMEOUT] = task.execution_timeout_seconds
    if task.orchestration_timeout_seconds is not None:
        spec[TaskSpecKey.ORCHESTRATION_TIMEOUT] = task.orchestration_timeout_seconds
    if task.wait_before_seconds:
        spec[TaskSpecKey.WAIT_BEFORE] = task.wait_before_seconds
    if task.outlets:
        spec[TaskSpecKey.OUTLETS] = [_serialize_outlet(a) for a in task.outlets]
    if task.inlets:
        spec[TaskSpecKey.INLETS] = [_serialize_inlet(a) for a in task.inlets]
    if task.wait_for:
        spec[TaskSpecKey.WAIT_FOR] = _serialize_wait_for(task.wait_for)
    if task.skip_on_backfill:
        spec[TaskSpecKey.SKIP_ON_BACKFILL] = True
    return spec


def _step_spec(step: Step) -> Dict[str, Any]:
    """A wrapper step's spec: it runs as a task row, with a task's defaults (ADR #137 §1)."""
    task_config, task_arn = _build_step_config_and_arn(step)
    return {
        TaskSpecKey.TASK_ID: step.step_id,
        TaskSpecKey.TASK_TYPE: step.step_type,
        TaskSpecKey.TASK_ARN: task_arn,
        TaskSpecKey.TASK_CONFIG: task_config,
        TaskSpecKey.CROSS_ACCOUNT_ROLE: "same",
        TaskSpecKey.DEPENDENCIES: [d.node_id for d in step.dependencies],
        TaskSpecKey.TRIGGER_RULE: "all_success",
    }


def build_task_specs(dag: DAG) -> List[Dict[str, Any]]:
    """The spec of every node of ``dag`` that runs as a task row: its tasks and wrapper steps.

    Direct steps (wait/pass/sns/...) are branches of the definition, not rows, and have none.
    """
    specs = [_task_spec(t) for t in dag.tasks]
    specs += [_step_spec(s) for s in dag.steps if s.step_type in WRAPPER_STEP_TYPES]
    return specs


def build_spec_version(dag: DAG) -> task_spec.Version:
    """The spec version ``polyris-deploy`` writes to ``pipeline-specs`` and the definition names."""
    return task_spec.build_version(dag.dag_id, build_task_specs(dag))


def _build_registration_chain(
    dag: DAG,
    *,
    asset_schedule: Dict,
    registry_table: str,
    tokens_table: str,
    asset_subscriptions_table: str,
    specs: task_spec.Version,
) -> Tuple[Dict, str]:
    """Build the registration state chain that runs before tasks.

    Produces states: Define_Inputs (optional) → Register_Pipeline →
    [Warn_Registration_Failed] →
    Register_Asset_Subscriptions (optional, with WriteSubscription +
    Warn_WriteSubscription_Failed + Subscription_Skipped inside its Map) →
    Check_Register_Only / Registration_Complete

    Each DynamoDB write carries _STANDARD_DYNAMODB_RETRY and a Catch that routes
    to a Warn_* state writing a _notify_warn_ record before continuing, so a
    ThrottlingException degrades to a UI notification rather than killing the run.
    
    Args:
        dag: DAG for metadata (dag_id, description, variables, schedule)
        asset_schedule: From _asset_schedule
        registry_table: DynamoDB table name for pipeline registry
        tokens_table: DynamoDB table name for tokens (its warnings)
        asset_subscriptions_table: DynamoDB table name for asset subscriptions
            (read by the platform's asset-fanout function — must be the table
            in its ASSET_SUBSCRIPTIONS_TABLE, both sourced from the SAM stack's
            AssetSubscriptionsTable output that polyris-deploy reads via
            describe_stacks)
        specs: the pipeline's spec version (build_spec_version); Register_Pipeline
            names it in the registry with its task count (ADR #137 §8)
    
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
                # The Output replaces the whole input: every field a task reads must be passed on here.
                "triggered_by": JSONATA_TRIGGERED_BY,
                # The same chains the tasks use: the declared variables replace the input's, so the
                # input's variables.current_date is read here or never (ADR #133).
                "current_date": JSONATA_DATE,
                "PARTITION_ARG": JSONATA_PARTITION,
            },
            "Next": "Register_Pipeline"
        }

    # The state after the registry's write, on its success and on its warning path alike.
    _after_registry = "Register_Asset_Subscriptions" if asset_schedule.get("assets") else "Check_Register_Only"

    # Register_Pipeline — refreshes DAG metadata on each execution.
    # Deliberately does NOT touch `registered_at`: that field means "last
    # polyris-deploy", not "last run", and polyris-deploy writes it directly
    # to pipeline-registry. If this state included it, every normal run would
    # overwrite the deploy timestamp with the run start time. updateItem (not
    # putItem) is required so the deploy-written `registered_at` survives.
    states["Register_Pipeline"] = {
        "Type": "Task",
        "Comment": "Refresh pipeline metadata for UI and asset triggers "
                   "(registered_at is owned by polyris-deploy, not written here)",
        "Resource": "arn:aws:states:::dynamodb:updateItem",
        "Arguments": {
            "TableName": registry_table,
            "Key": {
                "pipeline_name": {"S": dag.dag_id},
            },
            # `description`, `dag`, `schedule`, and `tasks` are DynamoDB reserved
            # words and must be aliased via ExpressionAttributeNames in any
            # expression (UpdateExpression, ConditionExpression, etc.). They work
            # fine as bare keys in PutItem's `Item` map because that is not an
            # expression, which is why the previous putItem never triggered this.
            "UpdateExpression": (
                "SET sfn_arn = :sfn_arn, "
                "#description = :description, "
                "pipeline_group = :pipeline_group, "
                "#schedule = :schedule, "
                "asset_schedule = :asset_schedule, "
                "last_execution = :last_execution, "
                "spec_id = :spec_id, "
                "task_count = :task_count "
                # 1.2 wrote the graph and every task into the registry; the spec version names them now
                # (ADR #137 §8), and a run removes what an older deploy left.
                "REMOVE #tasks, #dag"
            ),
            "ExpressionAttributeNames": {
                "#description": "description",
                "#tasks": "tasks",
                "#dag": "dag",
                "#schedule": "schedule",
            },
            "ExpressionAttributeValues": {
                ":sfn_arn": {"S": JSONATA_SFN_ARN},
                ":description": {"S": dag.description or ""},
                ":pipeline_group": {"S": dag.group or ""},
                ":schedule": {"S": dag.schedule if isinstance(dag.schedule, str) else ""},
                ":asset_schedule": {"S": json.dumps(asset_schedule)},
                ":last_execution": {"S": JSONATA_EXECUTION_NAME},
                ":spec_id": {"S": specs.spec_id},
                ":task_count": {"N": str(specs.task_count)},
            },
        },
        "Output": JSONATA_PASS_INPUT,
        "Retry": [_STANDARD_DYNAMODB_RETRY],
        # States.ALL does not match States.DataLimitExceeded (AWS documents it outside it), so the
        # Catch names it too: an input over 256 KB must reach the bell, not kill the run (ADR #137 §8).
        "Catch": [{
            "ErrorEquals": ["States.DataLimitExceeded"],
            "Output": "{% $merge([$states.input, {'_registration_error': $states.errorOutput.Cause}]) %}",
            "Next": "Warn_Registration_Failed"
        }, {
            "ErrorEquals": ["States.ALL"],
            "Output": "{% $merge([$states.input, {'_registration_error': $states.errorOutput.Cause}]) %}",
            "Next": "Warn_Registration_Failed"
        }],
        "Next": _after_registry
    }

    # Warn_Registration_Failed — writes a _notify_warn_ record so the UI shows the failure
    states["Warn_Registration_Failed"] = {
        "Type": "Task",
        "Comment": "Write register failure to UI notifications",
        "Resource": "arn:aws:states:::dynamodb:putItem",
        "Arguments": {
            "TableName": tokens_table,
            "Item": _warning_item("$states.context.Execution.Name & '-register_pipeline'", "Register_Pipeline",
                                  dag.dag_id, "'Register_Pipeline failed: ' & $string($states.input._registration_error)")
        },
        "Output": "{% $sift($states.input, function($v, $k) { $k != '_registration_error' }) %}",
        "Retry": [_STANDARD_DYNAMODB_RETRY],
        "Catch": [{
            "ErrorEquals": ["States.ALL"],
            "Output": "{% $sift($states.input, function($v, $k) { $k != '_registration_error' }) %}",
            "Next": _after_registry
        }],
        "Next": _after_registry
    }

    # Asset subscription registration for asset-triggered pipelines
    if asset_schedule.get("assets"):
        states["Register_Asset_Subscriptions"] = {
            "Type": "Map",
            "Comment": "Register subscription for each asset (enables Query instead of Scan)",
            "Items": _flatten_asset_names(dag._asset_schedule),
            "MaxConcurrency": 10,
            "ItemProcessor": {
                "ProcessorConfig": {"Mode": "INLINE"},
                "StartAt": "WriteSubscription",
                "States": {
                    "WriteSubscription": {
                        "Type": "Task",
                        "Comment": "subscribed_at is written once: a consumer reacts only to facts recorded after it subscribed (ADR #130 §6.1)",
                        "Resource": "arn:aws:states:::dynamodb:updateItem",
                        "Arguments": {
                            "TableName": asset_subscriptions_table,
                            "Key": {
                                "asset_name": {"S": "{% $states.input %}"},
                                "pipeline_name": {"S": dag.dag_id},
                            },
                            "UpdateExpression": ("SET sfn_arn = :arn, #op = :op, #assets = :assets, registered_at = :now, "
                                                 "subscribed_at = if_not_exists(subscribed_at, :now)"),
                            "ExpressionAttributeNames": {"#op": "operator", "#assets": "assets"},
                            "ExpressionAttributeValues": {
                                ":arn": {"S": JSONATA_SFN_ARN},
                                ":op": {"S": asset_schedule.get("operator", "AND")},
                                ":assets": {"S": json.dumps(_flatten_asset_names(dag._asset_schedule))},
                                ":now": {"S": JSONATA_NOW},
                            },
                        },
                        "Output": "{% null %}",
                        "Retry": [_STANDARD_DYNAMODB_RETRY],
                        "Catch": [{
                            "ErrorEquals": ["States.ALL"],
                            "Output": "{% {'_asset_name': $states.input, '_subscription_error': $states.errorOutput.Cause} %}",
                            "Next": "Warn_WriteSubscription_Failed"
                        }],
                        "End": True
                    },
                    "Warn_WriteSubscription_Failed": {
                        "Type": "Task",
                        "Comment": "Write subscription failure to UI notifications",
                        "Resource": "arn:aws:states:::dynamodb:putItem",
                        "Arguments": {
                            "TableName": tokens_table,
                            "Item": _warning_item(
                                "$states.context.Execution.Name & '-write_subscription-' & $string($states.input._asset_name)",
                                "WriteSubscription", dag.dag_id,
                                "'WriteSubscription failed for ' & $string($states.input._asset_name) & ': ' "
                                "& $string($states.input._subscription_error)")
                        },
                        "Output": "{% null %}",
                        "Retry": [_STANDARD_DYNAMODB_RETRY],
                        "Catch": [{
                            "ErrorEquals": ["States.ALL"],
                            "Output": "{% null %}",
                            "Next": "Subscription_Skipped"
                        }],
                        "End": True
                    },
                    "Subscription_Skipped": {
                        "Type": "Succeed"
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
    *,
    start_run_function_arn: Optional[str] = None,
    registry_table: Optional[str] = None,
    tokens_table: Optional[str] = None,
    asset_subscriptions_table: Optional[str] = None,
) -> str:
    """
    Generate AWS Step Functions JSON from DAG.
    
    Args:
        dag: DAG to generate JSON for
        start_run_function_arn: ARN of the Start_Run function that writes each run's
            rows (default: ${start_run_function_arn})
        registry_table: Name of pipeline registry table (default: ${registry_table})
        tokens_table: Name of pipeline tokens table (default: ${tokens_table})
        asset_subscriptions_table: Name of the asset subscriptions table that
            asset-triggered pipelines register into (default:
            ${asset_subscriptions_table}). Must match the platform's own
            AssetSubscriptionsTable — both are sourced from the SAM stack's
            AssetSubscriptionsTable CFN output that polyris-deploy reads via
            describe_stacks (see deploy.py).
    
    The registration chain runs first; then a Parallel holds one branch for the
    pipeline's tasks, Start_Run, and one per direct step (ADR #137 §1):
    - Tasks and wrapper steps (lambda/glue/ecs/athena) are rows: Start_Run writes
      them from the pipeline's spec version, which polyris-deploy writes before
      the stack, and the state ends when the dispatcher answers the run's token.
    - Direct steps (wait/pass/sns/sqs/s3/...) run as branches, with no dependencies.
      A failed one records its error, writes a `step` warning and marks the run row.
    After the Parallel, the execution fails when a branch recorded an error, so a
    failure never cancels a branch that is still running.
    """
    _start_run_arn = start_run_function_arn or "${start_run_function_arn}"
    _registry_table = registry_table or "${registry_table}"
    _tokens_table = tokens_table or "${tokens_table}"
    _asset_subscriptions_table = asset_subscriptions_table or "${asset_subscriptions_table}"
    
    # 1. The branches: Start_Run for the rows, one per direct step
    _check_dependencies(dag)
    specs = build_spec_version(dag)
    has_rows = specs.task_count > 0
    branches = [_start_run_branch(dag, specs, _start_run_arn, _tokens_table)] if has_rows else []
    direct_steps = [s for s in dag.steps if s.step_type not in WRAPPER_STEP_TYPES]
    branches += [_direct_step_branch(step, number, dag, _tokens_table, marks_run=has_rows)
                 for number, step in enumerate(direct_steps, start=1)]

    # 2. Build registration state chain
    states, start_at = _build_registration_chain(
        dag, asset_schedule=_asset_schedule(dag), registry_table=_registry_table, tokens_table=_tokens_table,
        asset_subscriptions_table=_asset_subscriptions_table, specs=specs,
    )

    # 4. The Parallel; the execution fails after it when a branch recorded an error
    errors = f"$states.input[$exists({BRANCH_ERROR})].{BRANCH_ERROR}"
    states["Run_All_Tasks"] = {
        "Type": "Parallel",
        "Branches": branches,
        "Next": "Check_Branches",
        "Catch": [{"ErrorEquals": ["States.DataLimitExceeded"], "Next": "Pipeline_Failed"},
                  {"ErrorEquals": ["States.ALL"], "Next": "Pipeline_Failed"}],
    }
    states["Check_Branches"] = {
        "Type": "Choice",
        "Comment": "Fail the execution once every branch has ended, if one of them recorded an error",
        "Choices": [{"Condition": f"{{% $count({errors}) > 0 %}}", "Next": "Run_Failed"}],
        "Default": "Run_Succeeded",
    }
    states["Run_Failed"] = {
        "Type": "Fail",
        "Error": f"{{% ($errors := {errors}; $errors[0].error) %}}",
        "Cause": (f"{{% ($errors := {errors}; $substring($join($append([], $errors.(branch & ': ' & error "
                  f"& (cause = '' ? '' : ': ' & cause))), '; '), 0, {_CAUSE_MAX})) %}}"),
    }
    states["Run_Succeeded"] = {"Type": "Succeed"}
    states["Pipeline_Failed"] = {
        "Type": "Fail",
        "Comment": "The Parallel itself failed: a branch failed outside its own Catch",
        "Error": "{% $exists($states.input.Error) ? $states.input.Error : 'PipelineFailed' %}",
        "Cause": "{% $exists($states.input.Cause) ? $states.input.Cause : '' %}",
    }
    
    # 5. Assemble definition
    definition = {
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
    """Generate deterministic hash of DAG structure for change detection."""
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

