"""Pipeline action routes (free / open-core) — register and run.

Team mutations (pause, restart) live in ee/team/pipelines_actions.py — ADR #98.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Dict

from config import sfn
from dal import pipelines_repo
from response import cors_response, safe_parse_body
from logger import log


def register_pipeline(event: Dict) -> Dict:
    """Register a new pipeline in the registry."""
    body, err = safe_parse_body(event)
    if err:
        return err
    pipeline_name = body.get('name')
    
    if not pipeline_name:
        return cors_response(400, {'error': 'Pipeline name is required'})
    
    item = {
        'pipeline_name': pipeline_name,
        'arn': body.get('arn', ''),
        'description': body.get('description', ''),
        'pipeline_group': body.get('group', ''),
        'tasks': body.get('tasks', []),
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    
    pipelines_repo.put(item)
    
    return cors_response(200, {
        'message': f'Pipeline {pipeline_name} registered',
        'pipeline': item
    })


def run_pipeline(pipeline_name: str, event: Dict) -> Dict:
    """Start a new pipeline execution."""
    # Get pipeline ARN from registry
    pipeline_arn = None
    try:
        item = pipelines_repo.get(pipeline_name) or {}
        pipeline_arn = item.get('sfn_arn', '') or item.get('arn')
    except Exception as e:
        log.error("run_pipeline", "Error getting pipeline from registry", error=str(e))
    
    # Fallback to listing state machines and finding by name
    available_pipelines = []
    if not pipeline_arn:
        try:
            paginator = sfn.get_paginator('list_state_machines')
            search_name = pipeline_name.lower().replace('_', '-').replace(' ', '-')
            candidates = []
            
            for page in paginator.paginate():
                for sm in page.get('stateMachines', []):
                    sm_name_lower = sm['name'].lower()
                    sm_name_normalized = sm_name_lower.replace('_', '-').replace(' ', '-')
                    
                    # Skip helper state machines
                    if any(helper in sm_name_lower for helper in ['wrapper', 'registration', 'run-task', 'helper', 'slack', 'failure', 'restart', 'notify']):
                        continue
                    
                    available_pipelines.append(sm['name'])
                    
                    # Exact match - use immediately
                    if sm_name_normalized == search_name or sm['name'] == pipeline_name:
                        pipeline_arn = sm['stateMachineArn']
                        break
                    
                    # Partial match - save as candidate
                    if search_name in sm_name_normalized or sm_name_normalized in search_name:
                        candidates.append({
                            'arn': sm['stateMachineArn'],
                            'name': sm['name'],
                            'score': abs(len(sm_name_normalized) - len(search_name))
                        })
                
                if pipeline_arn:
                    break
            
            # Use best candidate if no exact match
            if not pipeline_arn and candidates:
                candidates.sort(key=lambda x: x['score'])
                pipeline_arn = candidates[0]['arn']
                
        except Exception as e:
            log.error("pipelines", "Error listing state machines", error=str(e))
            import traceback
            traceback.print_exc()
    
    if not pipeline_arn:
        return cors_response(404, {
            'error': f'Pipeline {pipeline_name} not found',
            'available_pipelines': available_pipelines,
            'hint': 'Check CloudWatch logs for details'
        })
    
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
    
    # Extract skip_tasks — must be at top level for wrapper's Check_Skip_Tasks
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
    
    # skip_tasks must be top-level (wrapper checks $states.input.skip_tasks)
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
    router.add('POST', '/api/pipeline-register', register_pipeline)
