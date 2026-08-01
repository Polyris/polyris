"""Phase 2 integration for upstream smart-fill (ADR #88).

Bridges the pure SDK resolver (``polyris.upstream_resolver``) to the live
console-api world: it builds the cross-pipeline ``AssetGraph`` from the
pipeline registry and supplies an ``exists`` callable backed by the same
completeness gate the backfill pre-flight uses
(``_scan_completed_partitions``).

Scope (ADR #88 Phase 2): integration + plan computation. This module
*resolves and previews* the plan. Executing cross-pipeline upstream tiers is
Phase 3 (the SFN tiered-execution change); ``start_backfill`` uses
``requires_upstream_build`` to draw that boundary honestly rather than
silently dropping upstream work or producing wrong data.

Same-pipeline upstream is intentionally excluded from the graph — a
pipeline's own DAG already runs its tasks in dependency order (ADR #88), so
only cross-pipeline edges need resolving.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Tuple

from polyris.upstream_resolver import (
    AssetGraph,
    AssetNode,
    PlanItem,
    ResolvedPlan,
)


def _parse_tasks(pipeline: dict) -> list:
    """Parse a pipeline registry record's ``tasks`` field (JSON string or
    list). Returns [] on malformed input — never raises."""
    tasks_str = pipeline.get('tasks', '[]')
    try:
        tasks = json.loads(tasks_str) if isinstance(tasks_str, str) else tasks_str
    except (json.JSONDecodeError, TypeError):
        return []
    return tasks if isinstance(tasks, list) else []


def _asset_name(obj) -> Optional[str]:
    """An outlet/inlet entry is either a dict {'name': ...} or a bare str."""
    if isinstance(obj, dict):
        return obj.get('name')
    if isinstance(obj, str):
        return obj
    return None


def build_asset_graph(pipelines: List[dict]) -> AssetGraph:
    """Build the cross-pipeline asset dependency graph from registry records.

    Nodes are assets that some pipeline *produces* (declares as an outlet),
    carrying that pipeline's name, the outlet granularity, and the pipeline's
    ``dag_hash`` (ADR #89 R5). Edges run asset -> upstream-asset for each
    inlet, but only when the upstream asset is produced by a *different*
    pipeline (same-pipeline deps are handled by the DAG — ADR #88).

    Reuses the same outlet/inlet parsing shape as the asset graph builder and
    ``_find_producers_for_asset`` — no new registry schema.
    """
    graph = AssetGraph()

    # Pass 1 — register every produced asset as a node (asset -> producer).
    # Also remember, per task, the (outlets, inlets) so pass 2 can wire edges.
    task_io: List[Tuple[str, List[str], List[str]]] = []  # (pipeline, outs, ins)
    for pipeline in pipelines:
        pname = pipeline.get('pipeline_name', '')
        dag_hash = pipeline.get('dag_hash')
        for task in _parse_tasks(pipeline):
            outs: List[str] = []
            for outlet in task.get('outlets', []):
                name = _asset_name(outlet)
                if not name:
                    continue
                outs.append(name)
                gran = (outlet.get('granularity') if isinstance(outlet, dict) else None) or 'daily'
                # First producer wins; a multi-producer asset is rejected
                # upstream by _resolve_target before we ever resolve it.
                if name not in graph.nodes:
                    graph.add_node(AssetNode(
                        asset=name, pipeline=pname,
                        granularity=gran, dag_hash=dag_hash,
                    ))
            ins: List[str] = []
            for inlet in task.get('inlets', []):
                name = _asset_name(inlet)
                if name:
                    ins.append(name)
            if outs and ins:
                task_io.append((pname, outs, ins))

    # Pass 2 — wire cross-pipeline edges only. A task's outlet assets depend
    # on its inlet assets; skip the edge when producer is the same pipeline.
    for pname, outs, ins in task_io:
        for out_asset in outs:
            for in_asset in ins:
                up_node = graph.nodes.get(in_asset)
                if up_node is None:
                    continue  # external/ingested upstream — nothing to build
                if up_node.pipeline == pname:
                    continue  # same-pipeline — DAG handles ordering
                graph.add_edge(out_asset, in_asset)

    return graph


def make_exists_adapter(
    scan_completed: Callable[[str, List[str], Optional[List[str]]], set],
    expected_tasks_for: Callable[[str], Optional[List[str]]],
) -> Callable[[str, str, str], bool]:
    """Build the ``exists(asset, pipeline, partition)`` callable the resolver
    needs, backed by the production completeness gate.

    ``scan_completed`` is ``_scan_completed_partitions``;
    ``expected_tasks_for`` loads a pipeline's expected task ids. Both results
    are memoized so repeated lookups (e.g. 24 hourly partitions) don't rescan
    the same pipeline's task list. The per-partition DDB cost is identical to
    the existing skip-completed pre-flight (Phase 3 may batch further)."""
    expected_cache: Dict[str, Optional[List[str]]] = {}
    completed_cache: Dict[Tuple[str, str], bool] = {}

    def exists(asset: str, pipeline: str, partition: str) -> bool:
        ck = (pipeline, partition)
        if ck in completed_cache:
            return completed_cache[ck]
        if pipeline not in expected_cache:
            expected_cache[pipeline] = expected_tasks_for(pipeline)
        expected = expected_cache[pipeline]
        if not expected:
            completed_cache[ck] = False  # cannot prove completeness -> build
            return False
        done = scan_completed(pipeline, [partition], expected)
        result = partition in done
        completed_cache[ck] = result
        return result

    return exists


def requires_upstream_build(
    plan: ResolvedPlan, target_pipeline: str,
) -> List[PlanItem]:
    """Return the plan items that represent cross-pipeline upstream work that
    must actually be *built* (not reused, not the target pipeline itself).

    Used by ``start_backfill`` to draw the Phase 2/Phase 3 boundary: if this
    is non-empty on a real (non-preview) start, execution needs the SFN
    tiered run (Phase 3), so we return an honest 422 with the plan rather
    than silently skipping upstream and risking wrong data."""
    return [
        item for item in plan.all_items
        if item.pipeline != target_pipeline and not item.reused
    ]


def plan_to_response(plan: ResolvedPlan) -> dict:
    """Serialize a ResolvedPlan for the API preview/response."""
    return {
        'tiers': [
            [
                {
                    'asset': i.asset,
                    'pipeline': i.pipeline,
                    'partition': i.partition,
                    'reused': i.reused,
                    'dag_hash': i.dag_hash,
                }
                for i in tier
            ]
            for tier in plan.tiers
        ],
        'warnings': plan.warnings,
    }


def plan_to_sfn_tiers(
    plan: ResolvedPlan, arn_for: Callable[[str], str],
) -> List[List[dict]]:
    """Convert a ResolvedPlan into the bulk-backfill SFN ``tiers`` input
    (ADR #90). Each item is ``{sfn_arn, pipeline, partition_key, reused}``;
    ``arn_for`` resolves a pipeline name to its state-machine ARN.

    Empty tiers (every item reused) are dropped so the SFN does not spin up
    an outer-Map iteration with nothing to do."""
    out: List[List[dict]] = []
    for tier in plan.tiers:
        items = [
            {
                'sfn_arn': arn_for(i.pipeline),
                'pipeline': i.pipeline,
                'partition_key': i.partition,
                'reused': i.reused,
            }
            for i in tier
        ]
        if items:
            out.append(items)
    return out


def single_tier(
    partition_keys: List[str], pipeline: str, sfn_arn: str,
) -> List[List[dict]]:
    """Build a one-tier SFN plan for the no-upstream case (upstream=off).

    Behaviorally identical to the pre-Phase-3 flat partition list: one tier,
    every partition built (none reused), all against the target pipeline."""
    return [[
        {
            'sfn_arn': sfn_arn,
            'pipeline': pipeline,
            'partition_key': k,
            'reused': False,
        }
        for k in partition_keys
    ]]


def count_executable(tiers: List[List[dict]]) -> int:
    """Number of items that actually execute (not reused) across all tiers —
    the value to store as ``total_partitions`` for derived status."""
    return sum(1 for tier in tiers for item in tier if not item.get('reused'))


# ── Same-pipeline lineage frontier (ADR #92) ────────────────────────────────

def _task_dag(tasks: List[dict]) -> Dict[str, dict]:
    """Index registry tasks by id, exposing dependencies + skip_on_backfill."""
    out: Dict[str, dict] = {}
    for t in tasks:
        tid = t.get('task_id')
        if not tid:
            continue
        out[tid] = {
            'deps': list(t.get('dependencies', []) or []),
            'skip_on_backfill': bool(t.get('skip_on_backfill')),
        }
    return out


def lineage_frontier(
    producer: str,
    tasks: List[dict],
    output_missing: Callable[[str], bool],
    force: bool = False,
) -> set:
    """The set of same-pipeline tasks to RUN so that `producer` is built from
    real inputs (ADR #92).

    Walks up `producer`'s dependencies. A dependency is added to the run set
    (and recursed into) when `force` is true, or when its output is missing
    for the backfill (``output_missing(task_id)`` — true if the task's
    canonical output is absent for at least one requested partition). A
    dependency whose output is already present stops the walk on that branch
    (downstream reads the stored output). ``skip_on_backfill`` tasks are never
    added — they pull live data; their consumers read prior output from
    storage (preview warns if that stored output is itself missing).

    Returns task ids to RUN; feed straight into ``_compute_skip_task_ids`` as
    the positive subset (the producer is always included)."""
    dag = _task_dag(tasks)
    run: set = {producer}
    stack: List[str] = list(dag.get(producer, {}).get('deps', []))
    while stack:
        tid = stack.pop()
        node = dag.get(tid)
        if node is None or tid in run:
            continue
        if node['skip_on_backfill']:
            continue  # never run; consumer reads stored output
        if force or output_missing(tid):
            run.add(tid)
            stack.extend(node['deps'])
        # else: output present -> stop this branch (read from store)
    return run


def make_output_missing_adapter(
    status_for: Callable[[str, str], Dict[str, str]],
    pipeline: str,
    partitions: List[str],
    successful_statuses,
) -> Callable[[str], bool]:
    """Build ``output_missing(task_id)`` over the requested partitions.

    A task's output is "missing" if, for at least one requested partition, the
    task is not present-and-successful (so smart must (re)build it; the union
    over partitions never under-runs — Phase A carries one skip set for all
    partitions). ``status_for(pipeline, partition)`` returns the per-task
    status map for that partition (memoized by the caller)."""
    # Pre-fetch + memoize per-partition status maps once.
    maps = {pk: status_for(pipeline, pk) for pk in partitions}

    def output_missing(task_id: str) -> bool:
        for pk in partitions:
            st = maps.get(pk, {}).get(task_id)
            if st is None or st not in successful_statuses:
                return True  # missing for at least one partition
        return False

    return output_missing
