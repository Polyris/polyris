"""Pipeline DAG route (free / open-core).

Team observability (metrics, logs) lives in ee/team/pipelines_info.py — ADR #98.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError

from dal import executions_repo, pipelines_repo, specs_repo
from dal.specs_repo import SpecsMissing
from polyris.constants import TaskSpecKey as K
from polyris.coordination.task_spec import SpecError
from constants import Limits
from response import cors_response
from logger import log
from polyris.coordination.records import DAG_SNAPSHOT_PREFIX
from utils import parse_wait_before, should_skip_token_row


def _wait_for_nodes(entries: List[Any]) -> List[Dict[str, Any]]:
    """A spec's wait_for as the graph shows it: ``name`` for ``asset_name``, a freshness only when set."""
    shown: List[Dict[str, Any]] = []
    for entry in entries:
        if 'operator' in entry:
            shown.append({'operator': entry['operator'], 'assets': _wait_for_nodes(entry.get('assets', []))})
            continue
        leaf: Dict[str, Any] = {'name': entry['asset_name']}
        if entry.get('consecutive_days') is not None:
            leaf['consecutive_days'] = entry['consecutive_days']
        elif entry.get('freshness_hours'):
            leaf['freshness_hours'] = entry['freshness_hours']
        shown.append(leaf)
    return shown


def graph_from_specs(specs: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """A pipeline's graph from its task specs (ADR #137 §8): every task row is a node.

    The nodes carry what the generator's graph nodes carry for a task
    (``generators._build_dag_visualization_nodes_edges``); a wrapper step is a
    task row now, so it is drawn as a task. Direct steps have no spec and no node.
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    for task_id in sorted(specs):
        fields = specs[task_id].fields
        node: Dict[str, Any] = {'id': task_id, 'name': task_id, 'type': 'task', 'task_type': fields[K.TASK_TYPE]}
        if fields.get(K.WAIT_BEFORE):
            node['wait_before'] = fields[K.WAIT_BEFORE]
        if fields[K.TRIGGER_RULE] != 'all_success':
            node['trigger_rule'] = fields[K.TRIGGER_RULE]
        for key in (K.OUTLETS, K.INLETS):
            if fields.get(key):
                node[key.value] = fields[key]
        if fields.get(K.WAIT_FOR):
            node['wait_for'] = _wait_for_nodes(fields[K.WAIT_FOR])
        if fields.get(K.SKIP_ON_BACKFILL):
            node['skip_on_backfill'] = True
        if fields.get(K.TASK_CONFIG):
            node['task_config'] = fields[K.TASK_CONFIG]
        nodes.append(node)
        edges.extend({'from': dep, 'to': task_id} for dep in fields[K.DEPENDENCIES])
    return {'nodes': nodes, 'edges': edges}


def get_pipeline_dag(pipeline_name: str, event: Dict) -> Dict:
    """Get DAG structure for a pipeline from snapshot, task specs, or execution data.

    Priority:
    1. DAG snapshot (per-execution, survives redeploys) — if pipeline_execution given;
       only 1.2 runs have one, and 1.4 removes this reader (ADR #137, *Migration*)
    2. The task specs of the version the registry names (current pipeline definition)
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
            snapshot_key = f'{DAG_SNAPSHOT_PREFIX}{pipeline_execution}'
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
    
    # 2. The current definition: the task specs of the version the registry names
    try:
        registry_item = pipelines_repo.get(pipeline_name)
        if registry_item:
            dag = graph_from_specs(specs_repo.for_pipeline(registry_item))
            if dag['nodes']:
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
    except SpecsMissing as e:
        log.warn("pipelines_info", "Pipeline names no task specs; drawing from its runs",
                 pipeline_name=pipeline_name, error=str(e))
    except (ClientError, BotoCoreError, SpecError) as e:
        log.error("pipelines_info", "Error reading the pipeline's task specs; drawing from its runs",
                  pipeline_name=pipeline_name, error=str(e))

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
