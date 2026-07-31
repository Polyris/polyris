"""Unit tests for upstream_integration (ADR #88 Phase 2).

Covers the registry->AssetGraph builder, the exists adapter, the Phase 2/3
boundary helper, and plan serialization. Pure — no DDB.
"""

from upstream_integration import (
    build_asset_graph,
    make_exists_adapter,
    plan_to_response,
    requires_upstream_build,
)
from polyris.upstream_resolver import resolve_plan


def _pipe(name, tasks, dag_hash=None):
    import json
    return {'pipeline_name': name, 'tasks': json.dumps(tasks), 'dag_hash': dag_hash}


# ── build_asset_graph ────────────────────────────────────────────────────

class TestBuildAssetGraph:
    def test_registers_produced_assets_as_nodes(self):
        pipes = [
            _pipe('p_raw', [{'task_id': 't', 'outlets': [{'name': 'raw', 'granularity': 'daily'}]}], 'h1'),
        ]
        g = build_asset_graph(pipes)
        assert 'raw' in g.nodes
        assert g.nodes['raw'].pipeline == 'p_raw'
        assert g.nodes['raw'].granularity == 'daily'
        assert g.nodes['raw'].dag_hash == 'h1'

    def test_cross_pipeline_edge_created(self):
        pipes = [
            _pipe('p_raw', [{'task_id': 't1', 'outlets': [{'name': 'raw'}]}]),
            _pipe('p_cat', [{'task_id': 't2', 'outlets': [{'name': 'catalog'}],
                             'inlets': [{'name': 'raw'}]}]),
        ]
        g = build_asset_graph(pipes)
        edges = g.upstream_edges('catalog')
        assert len(edges) == 1
        assert edges[0].upstream == 'raw'

    def test_same_pipeline_edge_excluded(self):
        # raw and catalog produced by the SAME pipeline -> no cross edge
        pipes = [
            _pipe('p_one', [
                {'task_id': 't1', 'outlets': [{'name': 'raw'}]},
                {'task_id': 't2', 'outlets': [{'name': 'catalog'}], 'inlets': [{'name': 'raw'}]},
            ]),
        ]
        g = build_asset_graph(pipes)
        assert g.upstream_edges('catalog') == []  # DAG handles it, ADR #88

    def test_external_upstream_ignored(self):
        # inlet 'external' has no producer in registry -> no edge
        pipes = [
            _pipe('p_cat', [{'task_id': 't', 'outlets': [{'name': 'catalog'}],
                             'inlets': [{'name': 'external'}]}]),
        ]
        g = build_asset_graph(pipes)
        assert g.upstream_edges('catalog') == []

    def test_bare_string_outlets_inlets(self):
        pipes = [
            _pipe('p_raw', [{'task_id': 't1', 'outlets': ['raw']}]),
            _pipe('p_cat', [{'task_id': 't2', 'outlets': ['catalog'], 'inlets': ['raw']}]),
        ]
        g = build_asset_graph(pipes)
        assert 'raw' in g.nodes
        assert g.upstream_edges('catalog')[0].upstream == 'raw'

    def test_malformed_tasks_skipped(self):
        pipes = [{'pipeline_name': 'bad', 'tasks': 'not json{{'}]
        g = build_asset_graph(pipes)  # must not raise
        assert g.nodes == {}


# ── make_exists_adapter ──────────────────────────────────────────────────

class TestExistsAdapter:
    def test_returns_true_when_partition_complete(self):
        def scan(p, keys, exp): return set(keys)  # everything complete
        def expected(p): return ['t1']
        exists = make_exists_adapter(scan, expected)
        assert exists('raw', 'p_raw', '2026-05-20') is True

    def test_returns_false_when_incomplete(self):
        def scan(p, keys, exp): return set()  # nothing complete
        def expected(p): return ['t1']
        exists = make_exists_adapter(scan, expected)
        assert exists('raw', 'p_raw', '2026-05-20') is False

    def test_no_expected_tasks_means_not_complete(self):
        def scan(p, keys, exp): return set(keys)
        def expected(p): return None  # cannot prove completeness
        exists = make_exists_adapter(scan, expected)
        assert exists('raw', 'p_raw', '2026-05-20') is False

    def test_memoizes_scan_per_pipeline_partition(self):
        calls = {'scan': 0, 'expected': 0}
        def scan(p, keys, exp):
            calls['scan'] += 1
            return set(keys)
        def expected(p):
            calls['expected'] += 1
            return ['t1']
        exists = make_exists_adapter(scan, expected)
        exists('raw', 'p_raw', '2026-05-20')
        exists('raw', 'p_raw', '2026-05-20')  # repeat
        assert calls['scan'] == 1  # second call served from cache
        assert calls['expected'] == 1


# ── requires_upstream_build ──────────────────────────────────────────────

class TestRequiresUpstreamBuild:
    def _graph(self):
        from polyris.upstream_resolver import AssetGraph, AssetNode
        g = AssetGraph()
        g.add_node(AssetNode('raw', 'p_raw', 'daily'))
        g.add_node(AssetNode('catalog', 'p_cat', 'daily'))
        g.add_edge('catalog', 'raw')
        return g

    def test_missing_upstream_requires_build(self):
        plan = resolve_plan('catalog', ['2026-05-20'], self._graph(),
                            lambda a, p, k: False, mode='smart')
        to_build = requires_upstream_build(plan, 'p_cat')
        assert len(to_build) == 1
        assert to_build[0].asset == 'raw'

    def test_existing_upstream_no_build(self):
        plan = resolve_plan('catalog', ['2026-05-20'], self._graph(),
                            lambda a, p, k: True, mode='smart')
        assert requires_upstream_build(plan, 'p_cat') == []

    def test_target_never_counted_as_build(self):
        plan = resolve_plan('catalog', ['2026-05-20'], self._graph(),
                            lambda a, p, k: True, mode='smart')
        # catalog (target pipeline) is always (re)built but is not "upstream"
        assert all(i.pipeline != 'p_cat' for i in requires_upstream_build(plan, 'p_cat'))


# ── plan_to_response ─────────────────────────────────────────────────────

class TestPlanSerialization:
    def test_shape(self):
        from polyris.upstream_resolver import AssetGraph, AssetNode
        g = AssetGraph()
        g.add_node(AssetNode('catalog', 'p_cat', 'daily', dag_hash='h'))
        plan = resolve_plan('catalog', ['2026-05-20'], g,
                            lambda a, p, k: False, mode='smart')
        resp = plan_to_response(plan)
        assert 'tiers' in resp and 'warnings' in resp
        item = resp['tiers'][0][0]
        assert item['asset'] == 'catalog'
        assert item['pipeline'] == 'p_cat'
        assert item['partition'] == '2026-05-20'
        assert item['dag_hash'] == 'h'


# ── plan_to_sfn_tiers / single_tier / count_executable (Phase 3, ADR #90) ──

class TestSfnTierBuilders:
    def _plan(self, reused_raw=False):
        from polyris.upstream_resolver import AssetGraph, AssetNode, resolve_plan
        g = AssetGraph()
        g.add_node(AssetNode('raw', 'p_raw', 'daily'))
        g.add_node(AssetNode('catalog', 'p_cat', 'daily'))
        g.add_edge('catalog', 'raw')
        return resolve_plan('catalog', ['2026-05-20'], g,
                            lambda a, p, k: reused_raw, mode='smart')

    def test_plan_to_sfn_tiers_shape_and_order(self):
        from upstream_integration import plan_to_sfn_tiers
        arns = {'p_raw': 'arn:raw', 'p_cat': 'arn:cat'}
        tiers = plan_to_sfn_tiers(self._plan(), lambda n: arns[n])
        assert len(tiers) == 2
        assert tiers[0][0]['pipeline'] == 'p_raw'
        assert tiers[0][0]['sfn_arn'] == 'arn:raw'
        assert tiers[0][0]['partition_key'] == '2026-05-20'
        assert tiers[0][0]['reused'] is False
        assert tiers[-1][0]['pipeline'] == 'p_cat'

    def test_plan_to_sfn_tiers_marks_reused(self):
        from upstream_integration import plan_to_sfn_tiers
        tiers = plan_to_sfn_tiers(self._plan(reused_raw=True), lambda n: 'arn:' + n)
        flat = [i for t in tiers for i in t]
        raw = [i for i in flat if i['pipeline'] == 'p_raw'][0]
        assert raw['reused'] is True

    def test_single_tier(self):
        from upstream_integration import single_tier
        tiers = single_tier(['2026-05-19', '2026-05-20'], 'p1', 'arn:1')
        assert len(tiers) == 1
        assert len(tiers[0]) == 2
        assert all(i['pipeline'] == 'p1' and i['sfn_arn'] == 'arn:1'
                   and i['reused'] is False for i in tiers[0])
        assert {i['partition_key'] for i in tiers[0]} == {'2026-05-19', '2026-05-20'}

    def test_count_executable_excludes_reused(self):
        from upstream_integration import count_executable
        tiers = [
            [{'reused': True}, {'reused': False}],
            [{'reused': False}],
        ]
        assert count_executable(tiers) == 2


# ── Same-pipeline lineage frontier (ADR #92) ────────────────────────────────

from upstream_integration import lineage_frontier, make_output_missing_adapter

# Mike's acme-daily shape (subset relevant to classification_result lineage)
_ACME = [
    {"task_id": "extract_listings", "dependencies": [], "skip_on_backfill": True},
    {"task_id": "stage_listings", "dependencies": ["extract_listings"]},
    {"task_id": "build_product_details", "dependencies": ["stage_listings"]},
    {"task_id": "run_classification_model", "dependencies": ["build_product_details"]},
    {"task_id": "build_sales_raw", "dependencies": ["stage_metrics"]},
    {"task_id": "stage_metrics", "dependencies": ["extract_metrics"]},
    {"task_id": "extract_metrics", "dependencies": [], "skip_on_backfill": True},
]


class TestLineageFrontier:
    def test_force_pulls_full_lineage_minus_skip_on_backfill(self):
        run = lineage_frontier("run_classification_model", _ACME,
                               lambda t: True, force=True)
        assert run == {"run_classification_model", "build_product_details", "stage_listings"}
        assert "extract_listings" not in run  # skip_on_backfill never runs

    def test_smart_stops_at_present_output(self):
        # build_product_details present -> stop there; only producer runs
        present = {"build_product_details"}
        run = lineage_frontier(
            "run_classification_model", _ACME,
            lambda t: t not in present, force=False)
        assert run == {"run_classification_model"}

    def test_smart_builds_missing_chain(self):
        # nothing present -> build full chain (minus skip_on_backfill)
        run = lineage_frontier("run_classification_model", _ACME,
                               lambda t: True, force=False)
        assert run == {"run_classification_model", "build_product_details", "stage_listings"}

    def test_smart_partial_chain(self):
        # stage_listings present, product_details missing -> build product, stop at stage
        present = {"stage_listings"}
        run = lineage_frontier(
            "run_classification_model", _ACME,
            lambda t: t not in present, force=False)
        assert run == {"run_classification_model", "build_product_details"}

    def test_unrelated_branch_excluded(self):
        run = lineage_frontier("run_classification_model", _ACME,
                               lambda t: True, force=True)
        assert "build_sales_raw" not in run and "stage_metrics" not in run

    def test_producer_with_no_deps(self):
        run = lineage_frontier("extract_metrics", _ACME, lambda t: True, force=True)
        assert run == {"extract_metrics"}  # producer always included even if skip flag


class TestOutputMissingAdapter:
    def test_missing_if_absent_for_any_partition(self):
        def status_for(pipe, pk):
            return {"t": "success"} if pk == "2026-05-19" else {}
        miss = make_output_missing_adapter(status_for, "p",
                                           ["2026-05-19", "2026-05-20"], {"success"})
        assert miss("t") is True  # absent for 05-20

    def test_present_if_successful_for_all(self):
        miss = make_output_missing_adapter(
            lambda p, pk: {"t": "success"}, "p", ["2026-05-19", "2026-05-20"], {"success"})
        assert miss("t") is False

    def test_non_successful_status_counts_missing(self):
        miss = make_output_missing_adapter(
            lambda p, pk: {"t": "failed"}, "p", ["2026-05-19"], {"success", "skipped"})
        assert miss("t") is True
