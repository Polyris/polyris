"""Pipeline DAG route (free / open-core).

Team observability (metrics, logs) lives in ee/team/pipelines_info.py — ADR #98.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from botocore.exceptions import BotoCoreError, ClientError

from dal import executions_repo, pipelines_repo, specs_repo
from dal.specs_repo import SpecsMissing
from feed import dependencies_of, latest_run_of_date, readiness_of
from polyris.constants import TaskSpecKey as K
from polyris.coordination import run_row
from polyris.coordination.task_spec import SpecError
from response import cors_response
from logger import log
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
    """A pipeline's graph, or one run's (``pipeline_execution``).

    Priority (``dag_source``):
    1. ``run`` — a run started on 1.3: the specs of the version its run row names, the
       graph it ran with (ADR #137 §8);
    2. ``registry`` — the specs of the version the registry names: the current definition, which a
       run from before 1.3 (no run row) shows too (ADR #137, *Migration and upgrade*);
    3. ``inferred`` — from the run's task rows, or the latest run of the date's, when no
       specs can be read. No route scans the tasks table (ADR #137 §5).
    """
    params = event.get('queryStringParameters') or {}
    date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pipeline_execution = params.get('pipeline_execution', '')

    if pipeline_execution:
        run = executions_repo.get(run_row.key(pipeline_execution))
        if run is not None:
            dag = _run_graph(pipeline_name, run)
            if dag is not None:
                return _graph_response(pipeline_name, dag, 'run')

    # The current definition: the task specs of the version the registry names
    try:
        registry_item = pipelines_repo.get(pipeline_name)
        if registry_item:
            dag = graph_from_specs(specs_repo.for_pipeline(registry_item))
            if dag['nodes']:
                return _graph_response(pipeline_name, dag, 'registry')
    except SpecsMissing as e:
        log.warn("pipelines_info", "Pipeline names no task specs; drawing from its runs",
                 pipeline_name=pipeline_name, error=str(e))
    except (ClientError, BotoCoreError, SpecError) as e:
        log.error("pipelines_info", "Error reading the pipeline's task specs; drawing from its runs",
                  pipeline_name=pipeline_name, error=str(e))

    # Fallback: the graph the run's task rows give, or the latest run's of the date
    run_name = pipeline_execution or latest_run_of_date(pipeline_name, date)
    rows = executions_repo.query_by_pipeline_execution(run_name) if run_name else []
    return _graph_response(pipeline_name, _inferred_graph(rows), 'inferred')


def _graph_response(pipeline_name: str, dag: Dict[str, Any], source: str) -> Dict:
    return cors_response(200, {'name': pipeline_name, 'nodes': dag['nodes'], 'edges': dag['edges'],
                               'dag_source': source})


def _run_graph(pipeline_name: str, run: Mapping[str, Any]) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """A 1.3 run's graph from the specs of the version its run row names, or None when they cannot be read."""
    try:
        dag = graph_from_specs(specs_repo.for_pipeline(run))
    except (SpecsMissing, SpecError, ClientError, BotoCoreError) as e:
        log.error("pipelines_info", "Error reading the run's task specs; drawing the pipeline's",
                  pipeline_name=pipeline_name, run=str(run.get(run_row.TABLE_KEY, '')), error=str(e))
        return None
    return dag if dag['nodes'] else None


def _countdowns(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """task_name -> wait_before of a 1.2 run's task rows, which carried it."""
    countdowns: Dict[str, int] = {}
    for item in rows:
        if should_skip_token_row(item) or not item.get('task_name'):
            continue
        wait_before = parse_wait_before(item.get('wait_before'))
        if wait_before > 0:
            countdowns[item['task_name']] = wait_before
    return countdowns


def _inferred_graph(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """A graph from a run's task rows: a node per task, an edge per dependency the row names."""
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    countdowns = _countdowns(rows)
    seen_tasks = set()
    for item in rows:
        if should_skip_token_row(item):
            continue
        task_name = item.get('task_name', '')
        if not task_name or task_name in seen_tasks:
            continue
        seen_tasks.add(task_name)
        node: Dict[str, Any] = {'id': task_name, 'name': task_name.replace('_', ' ').title(), 'type': 'task'}
        if task_name in countdowns:
            node['wait_before'] = countdowns[task_name]
        nodes.append(node)
        edges.extend({'from': dep, 'to': task_name} for dep in dependencies_of(item, readiness_of(item)))
    return {'nodes': nodes, 'edges': edges}


def register(router) -> None:
    """Register the free pipeline DAG route. See ADR #97."""
    router.add('GET', '/api/pipeline-dag', get_pipeline_dag, 'name')
