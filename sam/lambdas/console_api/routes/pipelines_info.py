"""Pipeline DAG route (free / open-core).

Team observability (metrics, logs) lives in ee/team/pipelines_info.py — ADR #98.
"""
import json
from datetime import datetime, timezone
from typing import Dict

from boto3.dynamodb.conditions import Attr

from dal import executions_repo, pipelines_repo
from constants import Limits
from response import cors_response
from logger import log
from utils import parse_wait_before, should_skip_token_row


def get_pipeline_dag(pipeline_name: str, event: Dict) -> Dict:
    """Get DAG structure for a pipeline from snapshot, registry, or execution data.
    
    Priority:
    1. DAG snapshot (per-execution, survives redeploys) — if pipeline_execution given
    2. Pipeline registry (current DAG definition)
    3. Inferred from execution data (fallback)
    """
    params = event.get('queryStringParameters') or {}
    date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pipeline_execution = params.get('pipeline_execution', '')
    
    # Get execution data to supplement DAG with wait_before info
    if pipeline_execution:
        # Filter by specific execution
        exec_items = executions_repo.query_by_pipeline_execution(pipeline_execution)
    else:
        # Fallback to date filter (with pagination)
        exec_items = executions_repo.scan(
            max_items=Limits.MAX_FETCH_ITEMS,
            FilterExpression=Attr('pipeline_name').eq(pipeline_name) & Attr('date').eq(date)
        )
    
    # Build map of task_name -> wait_before from execution data (use latest value)
    wait_before_map = {}
    task_started_at = {}  # Track when each task started to pick latest
    for item in exec_items:
        if should_skip_token_row(item):
            continue
        task_name = item.get('task_name', '')
        if task_name:
            wb = parse_wait_before(item.get('wait_before'))
            if wb > 0:
                started_at = item.get('started_at', '')
                # Keep wait_before from the latest execution of this task
                if task_name not in task_started_at or started_at > task_started_at[task_name]:
                    wait_before_map[task_name] = wb
                    task_started_at[task_name] = started_at
    
    # 1. Try DAG snapshot for this specific execution (survives redeploys)
    if pipeline_execution:
        try:
            snapshot_key = f'dag_snapshot::{pipeline_execution}'
            snapshot_item = executions_repo.get(snapshot_key)
            if snapshot_item:
                dag_str = snapshot_item.get('dag', '{}')
                dag = json.loads(dag_str) if isinstance(dag_str, str) else dag_str
                if dag.get('nodes') and isinstance(dag.get('edges'), list):
                    for node in dag['nodes']:
                        node_id = node.get('id', '')
                        if node_id in wait_before_map and 'wait_before' not in node:
                            node['wait_before'] = wait_before_map[node_id]
                    
                    return cors_response(200, {
                        'name': pipeline_name,
                        'nodes': dag['nodes'],
                        'edges': dag['edges'],
                        'dag_source': 'snapshot'
                    })
        except Exception as e:
            log.error("pipelines", "Error getting DAG snapshot", error=str(e))
    
    # 2. Try current DAG from registry
    try:
        registry_item = pipelines_repo.get(pipeline_name)
        if registry_item:
            dag_str = registry_item.get('dag', '{}')
            try:
                dag = json.loads(dag_str) if isinstance(dag_str, str) else dag_str
                if dag.get('nodes') and isinstance(dag.get('edges'), list):
                    # Enrich nodes with wait_before from execution data
                    for node in dag['nodes']:
                        node_id = node.get('id', '')
                        if node_id in wait_before_map and 'wait_before' not in node:
                            node['wait_before'] = wait_before_map[node_id]
                    
                    return cors_response(200, {
                        'name': pipeline_name,
                        'nodes': dag['nodes'],
                        'edges': dag['edges'],
                        'dag_source': 'registry'
                    })
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as e:
                log.warn("pipelines_info", "Malformed DAG snapshot in registry; falling back",
                         pipeline_name=pipeline_name, error=str(e))
    except Exception as e:
        log.error("pipelines", "Error getting DAG from registry", error=str(e))
    
    # Fallback: build DAG from execution data
    nodes = []
    edges = []
    seen_tasks = set()
    
    for item in exec_items:
        if should_skip_token_row(item):
            continue
        # Use task_name field directly - don't derive from execution_name
        task_name = item.get('task_name', '')
        if not task_name:
            continue
            
        if task_name in seen_tasks:
            continue
        seen_tasks.add(task_name)
        
        node = {
            'id': task_name,
            'name': task_name.replace('_', ' ').title(),
            'type': 'task'
        }
        
        # Add wait_before if present
        if task_name in wait_before_map:
            node['wait_before'] = wait_before_map[task_name]
        
        nodes.append(node)
        
        # Parse dependencies
        deps_str = item.get('dependencies', '[]')
        try:
            deps = json.loads(deps_str) if isinstance(deps_str, str) else deps_str
        except (json.JSONDecodeError, TypeError, ValueError):
            deps = []
        
        for dep in deps:
            edges.append({
                'from': dep,
                'to': task_name
            })
    
    return cors_response(200, {
        'name': pipeline_name,
        'nodes': nodes,
        'edges': edges,
        'dag_source': 'inferred'
    })


def register(router) -> None:
    """Register the free pipeline DAG route. See ADR #97."""
    router.add('GET', '/api/pipeline-dag', get_pipeline_dag, 'name')
