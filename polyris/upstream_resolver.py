"""Cross-pipeline upstream resolver for backfill smart-fill.

Implements the resolver described in ADR #88 (Python-planned, SFN-trusted)
on the partition-mapping shape of ADR #87. Given a target asset and the
partitions to backfill, it produces a tiered execution plan: tier *i* must
complete before tier *i+1*, and within a tier the items are independent.

Scope (ADR #88):
  - Only *cross-pipeline* upstream is resolved here. Same-pipeline upstream
    is handled for free by the pipeline's own DAG ordering — the caller
    excludes same-pipeline edges when building the graph.
  - The default partition mapping is the intersecting time window
    (``partitions.partitions_covering``): 1↔1 for equal granularity, the
    covering set for cross-granularity.
  - Window offsets (ADR #87 Phase 2 / ADR #89 R2) are *reserved but not yet
    honored*: an edge carrying an offset is resolved as 1↔1 and a warning is
    collected. This keeps the authored API stable for when Phase 2 lands.

Production notes addressed (vs. the Phase 0.5 spike):
  - Depth/tiering is computed in O(V+E) via a Kahn-style forward
    propagation over the discovered DAG, not by re-walking on each deeper
    path (the spike's noted inefficiency).
  - ``PlanItem`` records the producing ``dag_hash`` (ADR #89 R5) so a later
    code-version policy needs no retroactive data backfill.

This module is pure: no AWS, no DDB. The caller (Phase 2 integration) builds
the ``AssetGraph`` from the pipeline registry and supplies an ``exists``
callable backed by ``_scan_completed_partitions``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .partitions import Granularity, partitions_covering


class CycleError(ValueError):
    """Raised when the cross-pipeline asset graph contains a cycle."""


@dataclass(frozen=True)
class AssetNode:
    """An asset, the pipeline that produces it, and its partition cadence.

    ``dag_hash`` is the producing pipeline's current DAG hash (ADR #89 R5);
    recorded on built plan items so future code-version policy is additive.
    """
    asset: str
    pipeline: str
    granularity: Granularity
    dag_hash: Optional[str] = None


@dataclass(frozen=True)
class UpstreamEdge:
    """A cross-pipeline dependency edge: ``asset`` consumes ``upstream``.

    ``offset`` reserves the window surface (ADR #87 Phase 2 / #89 R2). When
    set in Phase 1 it is *not honored* — the edge resolves 1↔1 and a warning
    is emitted. Shape mirrors Dagster's start/end offset.
    """
    asset: str
    upstream: str
    offset: Optional[Tuple[int, int]] = None  # (start, end), Phase 2


@dataclass
class AssetGraph:
    """Cross-pipeline asset dependency graph (same-pipeline edges excluded).

    ``nodes[asset]`` -> AssetNode; ``_edges[asset]`` -> list[UpstreamEdge].
    """
    nodes: Dict[str, AssetNode] = field(default_factory=dict)
    _edges: Dict[str, List[UpstreamEdge]] = field(default_factory=dict)

    def add_node(self, node: AssetNode) -> None:
        self.nodes[node.asset] = node

    def add_edge(self, asset: str, upstream: str,
                 offset: Optional[Tuple[int, int]] = None) -> None:
        self._edges.setdefault(asset, []).append(
            UpstreamEdge(asset=asset, upstream=upstream, offset=offset)
        )

    def upstream_edges(self, asset: str) -> List[UpstreamEdge]:
        return self._edges.get(asset, [])

    def node(self, asset: str) -> AssetNode:
        return self.nodes[asset]


@dataclass(frozen=True)
class PlanItem:
    """One unit of work: materialize ``asset``'s ``partition`` via
    ``pipeline``. ``reused`` True means the partition already exists and
    smart-fill skips building it. ``dag_hash`` is the producer's current
    hash at plan time (ADR #89 R5)."""
    asset: str
    pipeline: str
    partition: str
    reused: bool
    dag_hash: Optional[str] = None


@dataclass
class ResolvedPlan:
    """Result of resolution: ordered tiers plus any non-fatal warnings.

    ``tiers[0]`` runs first; each tier's items are independent. ``warnings``
    follows the SDK convention (collected, not logged) so the caller decides
    how to surface them (e.g. in the backfill API response)."""
    tiers: List[List[PlanItem]]
    warnings: List[str] = field(default_factory=list)

    @property
    def all_items(self) -> List[PlanItem]:
        return [item for tier in self.tiers for item in tier]


Unit = Tuple[str, str]  # (asset, partition)


def resolve_plan(
    target_asset: str,
    target_partitions: List[str],
    graph: AssetGraph,
    exists: Callable[[str, str, str], bool],  # (asset, pipeline, partition)
    mode: str = "smart",
) -> ResolvedPlan:
    """Resolve the cross-pipeline backfill plan for ``target_asset`` over
    ``target_partitions``.

    Args:
      graph: cross-pipeline dependency graph (same-pipeline edges excluded).
      exists: returns True if (asset, pipeline, partition) is already
        complete — wired to ``_scan_completed_partitions`` in Phase 2.
      mode: ``"smart"`` reuses existing upstream partitions and builds only
        the missing; ``"force"`` rebuilds all upstream regardless. The
        target's own partitions are always (re)built — that is the backfill.

    Returns a ``ResolvedPlan`` with deepest-dependency-first tiers.

    Raises ``CycleError`` if the graph has a cycle.
    """
    if mode not in ("smart", "force"):
        raise ValueError(f"mode must be 'smart' or 'force'; got {mode!r}")

    warnings: List[str] = []

    # ── Phase A: discovery — collect all required units and their edges,
    # with cycle detection. Each unit's upstream is computed once (memoized
    # in ``adj``); this is the O(V+E) discovery the spike lacked.
    adj: Dict[Unit, List[Unit]] = {}
    state: Dict[Unit, int] = {}  # 0 unvisited, 1 in-progress, 2 done

    def discover(unit: Unit, path: List[str]) -> None:
        label = f"{unit[0]}@{unit[1]}"
        st = state.get(unit, 0)
        if st == 1:
            cyc = path[path.index(label):] + [label]
            raise CycleError("cycle detected: " + " -> ".join(cyc))
        if st == 2:
            return
        state[unit] = 1
        path = path + [label]
        asset, partition = unit
        a_node = graph.node(asset)
        ups: List[Unit] = []
        for edge in graph.upstream_edges(asset):
            up_node = graph.node(edge.upstream)
            if edge.offset is not None:
                warnings.append(
                    f"window offset on {edge.asset} <- {edge.upstream} is not "
                    f"yet honored (Phase 2); resolving as same-period 1↔1"
                )
            for up_part in partitions_covering(
                partition, a_node.granularity, up_node.granularity
            ):
                up_unit = (edge.upstream, up_part)
                ups.append(up_unit)
                discover(up_unit, path)
        adj[unit] = ups
        state[unit] = 2

    seeds: List[Unit] = [(target_asset, p) for p in target_partitions]
    for seed in seeds:
        discover(seed, [])

    # ── Phase B: depth = longest distance from any target seed. Forward
    # propagation in topological order (Kahn) — O(V+E), no re-walking.
    # Build reverse in-degree over the discovered DAG.
    # Ensure every discovered unit (including leaf upstreams) is present in
    # both adj and indeg before counting.
    for u, ups in list(adj.items()):
        for up in ups:
            adj.setdefault(up, [])
    indeg: Dict[Unit, int] = {u: 0 for u in adj}
    for u, ups in adj.items():
        for up in ups:
            indeg[up] += 1

    depth: Dict[Unit, int] = {u: 0 for u in adj}
    # Kahn queue starting from seeds (in-degree 0 are the targets).
    # This pass computes tier depth only — cycles were already rejected above
    # (CycleError), so the queue is guaranteed to drain every node.
    queue = [u for u in adj if indeg.get(u, 0) == 0]
    while queue:
        u = queue.pop()
        for up in adj.get(u, []):
            if depth[u] + 1 > depth[up]:
                depth[up] = depth[u] + 1
            indeg[up] -= 1
            if indeg[up] == 0:
                queue.append(up)

    max_depth = max(depth.values()) if depth else 0

    # ── Phase C: bucket units into tiers (deepest dependency first) and
    # decide reuse vs build.
    tiers: List[List[PlanItem]] = [[] for _ in range(max_depth + 1)]
    for unit, d in depth.items():
        asset, partition = unit
        node = graph.node(asset)
        is_target = asset == target_asset and partition in set(target_partitions)
        reused = (
            (not is_target)
            and mode == "smart"
            and exists(asset, node.pipeline, partition)
        )
        tier_index = max_depth - d  # deepest dependency runs first
        tiers[tier_index].append(PlanItem(
            asset=asset, pipeline=node.pipeline, partition=partition,
            reused=reused, dag_hash=node.dag_hash,
        ))

    for tier in tiers:
        tier.sort(key=lambda i: (i.asset, i.partition))

    # Dedup warnings (the same offset edge fires once per covered partition).
    deduped = list(dict.fromkeys(warnings))

    return ResolvedPlan(tiers=tiers, warnings=deduped)
