"""Pipeline action routes (free / open-core) — run.

Team mutations (pause, restart) live in ee/team/pipelines_actions.py — ADR #98.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Dict

from botocore.exceptions import BotoCoreError, ClientError

from config import sfn
from dal import pipelines_repo
from response import cors_response, safe_parse_body
from logger import log


def unregistered_message(pipeline_name: str) -> str:
    """What the console answers for a Run of a pipeline the registry does not hold."""
    return (f"Pipeline '{pipeline_name}' is not registered on this platform. A pipeline registers within a "
            "minute of polyris-deploy; deploy it, then run it.")


def run_pipeline(pipeline_name: str, event: Dict) -> Dict:
    """Start a new execution of a registered pipeline.

    The registry is the one source of a pipeline's state machine (ADR #23): a name it does not
    hold answers 404, and a registry that cannot be read 500. The console never looks a state
    machine up by its name, which started any state machine of the account whose name was close.
    """
    try:
        item = pipelines_repo.get(pipeline_name) or {}
    except (ClientError, BotoCoreError) as e:
        log.error("run_pipeline", "Error getting pipeline from registry", error=str(e), pipeline_name=pipeline_name)
        return cors_response(500, {'error': f'Could not read the pipeline registry: {e}'})
    pipeline_arn = item.get('sfn_arn', '')
    if not pipeline_arn:
        return cors_response(404, {'error': unregistered_message(pipeline_name)})

    # Parse input from body
    body, err = safe_parse_body(event)
    if err:
        return err
    
    input_data = body.get('input', {})
    
    # Add date if not provided
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    current_date = input_data.pop('current_date', today)
    partition_arg = input_data.pop('PARTITION_ARG', current_date)
    date_val = input_data.pop('date', None)  # Legacy field
    if date_val and current_date == today:
        current_date = date_val
        partition_arg = date_val
    
    # skip_tasks: the tasks Start_Run writes as skipped, read from the run input's top level
    skip_tasks = input_data.pop('skip_tasks', None)
    
    # Wrap user params in variables so they reach tasks via clean input
    execution_data = {
        'current_date': current_date,
        'PARTITION_ARG': partition_arg,
        'triggered_by': 'console_run',
        'variables': {
            'current_date': current_date,
            'PARTITION_ARG': partition_arg,
            **input_data
        }
    }
    
    # Top level, where Start_Run reads it (run_input.SKIP_TASKS)
    if skip_tasks:
        execution_data['skip_tasks'] = skip_tasks
    
    try:
        # Start execution with idempotent name to prevent double-start on retry/double-click
        exec_id = uuid.uuid4().hex[:8]
        execution_name = f"{pipeline_name}-run-{current_date}-{exec_id}"
        response = sfn.start_execution(
            stateMachineArn=pipeline_arn,
            name=execution_name,
            input=json.dumps(execution_data)
        )
        
        return cors_response(200, {
            'execution_arn': response['executionArn'],
            'started_at': response['startDate'].isoformat()
        })
    except sfn.exceptions.ExecutionAlreadyExists:
        return cors_response(409, {'error': f'Execution already exists for {pipeline_name} on {current_date}'})
    except Exception as e:
        log.error("unknown", "Unexpected error", error=str(e))
        return cors_response(500, {'error': f'Failed to start pipeline: {str(e)}'})


def register(router) -> None:
    """Register the free pipeline action routes (register, run). See ADR #97."""
    router.add('POST', '/api/pipeline-run', run_pipeline, 'name')
