"""Core validation-engine tests — graph, cycles, discovery, extraction.

Complements ``test_validation_schema.py`` (which exercises only
``validate_schema_consistency``). This module covers the rest of the
local-validation engine in ``polyris.validation``:

  - ``build_asset_graph``      — DAGInfo list → producer/consumer graph
  - ``detect_asset_cycles``    — BFS cycle detection over that graph
  - ``discover_pipeline_files``— filesystem scan for pipeline files
  - ``extract_dag_info``       — import a pipeline file → DAGInfo
  - ``validate_asl_from_dag``  — generate + validate ASL for a DAG object
  - ``validate_all``           — full discover → extract → graph → cycles
  - ``_validate_single``       — single-file validation entrypoint

All tests drive the *real* code paths (no mock-around-the-bug, per
CLAUDE.md #13). Pure functions are fed hand-built ``DAGInfo`` objects;
the file-based functions run against real, importable DAG fixtures
written into ``tmp_path``.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from polyris.validation import (
    DAGInfo,
    build_asset_graph,
    detect_asset_cycles,
    discover_pipeline_files,
    extract_dag_info,
    validate_all,
    validate_asl_from_dag,
    _validate_single,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _dag_info(dag_id, produces=None, triggered_by=None):
    """Build a minimal DAGInfo for graph/cycle tests."""
    return DAGInfo(
        dag_id=dag_id,
        file_path=f"{dag_id}.py",
        is_asset_triggered=bool(triggered_by),
        trigger_assets=list(triggered_by or []),
        produced_assets=list(produces or []),
    )


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    """Write a dedented .py fixture under tmp_path and return its path."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body).lstrip())
    return p


# Reusable real DAG fixtures -------------------------------------------------- #
SOLO_DAG = """
    from polyris import DAG, task
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    with DAG(dag_id="solo", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def step():
            pass
        step()
"""

ASSET_DAG = """
    from polyris import DAG, task, Asset, Column, types as t
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    orders = Asset(name="shop/orders",
                   schema=[Column("id", t.bigint()), Column("amount", t.decimal(10, 2))])
    with DAG(dag_id="producer", schedule=None) as dag:
        @task.sfn(arn=ARN, outlets=[orders])
        def make():
            pass
        @task.sfn(arn=ARN, inlets=[orders])
        def use():
            pass
        m = make()
        use(m)
"""

# Asset-triggered DAG whose trigger asset has no producer anywhere.
ORPHAN_TRIGGER_DAG = """
    from polyris import DAG, task, Asset
    ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
    upstream = Asset("nobody/makes-this")
    with DAG(dag_id="consumer", schedule=upstream) as dag:
        @task.sfn(arn=ARN)
        def go():
            pass
        go()
"""

BROKEN_DAG = """
    from polyris import DAG  # noqa: F401
    raise RuntimeError("boom at import time")
"""


# ============================================================ #
# build_asset_graph
# ============================================================ #
class TestBuildAssetGraph:
    def test_empty_input_gives_empty_graph(self):
        g = build_asset_graph([])
        assert g == {
            "producers": {},
            "consumers": {},
            "dag_produces": {},
            "dag_consumes": {},
        }

    def test_single_producer_registered(self):
        g = build_asset_graph([_dag_info("A", produces=["x"])])
        assert g["producers"] == {"x": ["A"]}
        assert g["dag_produces"] == {"A": ["x"]}
        # A is not asset-triggered → absent from consumer side.
        assert g["consumers"] == {}
        assert g["dag_consumes"] == {}

    def test_consumer_via_trigger_registered(self):
        g = build_asset_graph([_dag_info("B", triggered_by=["x"])])
        assert g["consumers"] == {"x": ["B"]}
        assert g["dag_consumes"] == {"B": ["x"]}
        # B produces nothing, but still gets an (empty) produces entry.
        assert g["producers"] == {}
        assert g["dag_produces"] == {"B": []}

    def test_producer_then_consumer_chain(self):
        a = _dag_info("A", produces=["x"])
        b = _dag_info("B", triggered_by=["x"])
        g = build_asset_graph([a, b])
        assert g["producers"]["x"] == ["A"]
        assert g["consumers"]["x"] == ["B"]

    def test_two_producers_same_asset(self):
        g = build_asset_graph([
            _dag_info("A", produces=["x"]),
            _dag_info("B", produces=["x"]),
        ])
        assert g["producers"]["x"] == ["A", "B"]

    def test_non_triggered_dag_absent_from_consumers(self):
        g = build_asset_graph([_dag_info("A", produces=["x"])])
        assert "A" not in g["dag_consumes"]
        assert g["consumers"] == {}


# ============================================================ #
# detect_asset_cycles
# ============================================================ #
class TestDetectAssetCycles:
    def test_no_cycle_in_linear_chain(self):
        # A produces x → triggers B; B produces z that triggers nobody.
        a = _dag_info("A", produces=["x"])
        b = _dag_info("B", produces=["z"], triggered_by=["x"])
        cycles = detect_asset_cycles(build_asset_graph([a, b]))
        assert cycles == []

    def test_simple_two_dag_cycle(self):
        # A: triggered by x, produces y.  B: triggered by y, produces x.
        a = _dag_info("A", produces=["y"], triggered_by=["x"])
        b = _dag_info("B", produces=["x"], triggered_by=["y"])
        cycles = detect_asset_cycles(build_asset_graph([a, b]))
        assert cycles, "expected the A↔B asset cycle to be detected"
        starts = {c["start_dag"] for c in cycles}
        assert starts & {"A", "B"}

    def test_self_cycle_detected(self):
        # A produces x AND is triggered by x.
        a = _dag_info("A", produces=["x"], triggered_by=["x"])
        cycles = detect_asset_cycles(build_asset_graph([a]))
        assert cycles

    def test_three_dag_cycle_detected(self):
        # A→y→B→z→C→x→A — a longer multi-DAG trigger loop.
        a = _dag_info("A", produces=["y"], triggered_by=["x"])
        b = _dag_info("B", produces=["z"], triggered_by=["y"])
        c = _dag_info("C", produces=["x"], triggered_by=["z"])
        cycles = detect_asset_cycles(build_asset_graph([a, b, c]))
        assert cycles, "expected the A→B→C→A asset cycle to be detected"

    def test_no_triggered_dags_means_no_cycles(self):
        a = _dag_info("A", produces=["x"])  # producer only, nothing triggered
        assert detect_asset_cycles(build_asset_graph([a])) == []

    def test_cycle_has_one_entry_per_start_dag(self):
        a = _dag_info("A", produces=["y"], triggered_by=["x"])
        b = _dag_info("B", produces=["x"], triggered_by=["y"])
        cycles = detect_asset_cycles(build_asset_graph([a, b]))
        # De-dup guard: at most one cycle record per start_dag.
        starts = [c["start_dag"] for c in cycles]
        assert len(starts) == len(set(starts))


# ============================================================ #
# discover_pipeline_files
# ============================================================ #
class TestDiscoverPipelineFiles:
    def test_missing_directory_returns_empty(self, tmp_path):
        assert discover_pipeline_files(str(tmp_path / "does-not-exist")) == []

    def test_finds_dag_py(self, tmp_path):
        _write(tmp_path, "sub/dag.py", "x = 1\n")
        found = discover_pipeline_files(str(tmp_path))
        assert any(f.endswith("/dag.py") for f in found)

    def test_finds_named_pipeline_and_dag_files(self, tmp_path):
        _write(tmp_path, "foo_pipeline.py", "x = 1\n")
        _write(tmp_path, "bar_dag.py", "x = 1\n")
        found = discover_pipeline_files(str(tmp_path))
        assert any(f.endswith("foo_pipeline.py") for f in found)
        assert any(f.endswith("bar_dag.py") for f in found)

    def test_finds_file_by_dag_content(self, tmp_path):
        # Name matches no pattern → only the 'DAG(' content marks it.
        _write(tmp_path, "thing.py", "from x import DAG\nwith DAG('a'):\n    pass\n")
        found = discover_pipeline_files(str(tmp_path))
        assert any(f.endswith("thing.py") for f in found)

    def test_excludes_tests_pycache_and_plain_files(self, tmp_path):
        _write(tmp_path, "test_thing.py", "with DAG('x'):\n    pass\n")
        _write(tmp_path, "__pycache__/cached.py", "DAG(\n")
        _write(tmp_path, "plain.py", "x = 1\n")  # no DAG, no name match
        found = discover_pipeline_files(str(tmp_path))
        assert not any("test_thing.py" in f for f in found)
        assert not any("__pycache__" in f for f in found)
        assert not any(f.endswith("plain.py") for f in found)

    def test_result_is_sorted_and_deduped(self, tmp_path):
        _write(tmp_path, "b/dag.py", "x = 1\n")
        _write(tmp_path, "a/dag.py", "x = 1\n")
        found = discover_pipeline_files(str(tmp_path))
        assert found == sorted(set(found))


# ============================================================ #
# extract_dag_info
# ============================================================ #
class TestExtractDagInfo:
    def test_extracts_single_dag(self, tmp_path):
        f = _write(tmp_path, "solo/dag.py", SOLO_DAG)
        infos = extract_dag_info(str(f))
        assert len(infos) == 1
        assert infos[0].dag_id == "solo"

    def test_extracts_assets_and_schema(self, tmp_path):
        f = _write(tmp_path, "prod/dag.py", ASSET_DAG)
        infos = extract_dag_info(str(f))
        assert len(infos) == 1
        info = infos[0]
        assert "shop/orders" in info.produced_assets
        assert "shop/orders" in info.consumed_assets
        # Typed schema captured for cross-pipeline validation.
        cols = info.outlet_schemas.get("shop/orders", [])
        names = {c.get("name") for c in cols}
        assert {"id", "amount"} <= names

    def test_broken_file_returns_empty_not_raises(self, tmp_path):
        f = _write(tmp_path, "broken/dag.py", BROKEN_DAG)
        # Import error is swallowed; no DAG is produced.
        assert extract_dag_info(str(f)) == []


# ============================================================ #
# validate_asl_from_dag
# ============================================================ #
class TestValidateAslFromDag:
    def _build_chain(self):
        from polyris import DAG, task

        with DAG("chain", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def extract():
                pass

            @task.sfn(arn=ARN)
            def load():
                pass

            extract() >> load()
        return dag

    def test_valid_dag_passes(self):
        is_valid, errors, _warnings = validate_asl_from_dag(self._build_chain())
        assert is_valid is True
        assert errors == []

    def test_single_task_dag_passes(self):
        from polyris import DAG, task

        with DAG("one", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def only():
                pass
            only()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is True
        assert errors == []

    def test_generation_failure_is_reported(self):
        # A non-DAG object makes ASL generation raise; the helper must
        # convert that into (False, [error], []) rather than propagating.
        is_valid, errors, warnings = validate_asl_from_dag(object())
        assert is_valid is False
        assert errors and isinstance(errors[0], str)
        assert warnings == []

    def test_role_same_passes(self):
        """The default role='same' is always valid — no config.roles lookup needed."""
        from polyris import DAG, task

        with DAG("role-same", schedule=None) as dag:
            @task.sfn(arn=ARN, role="same")
            def t():
                pass
            t()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is True
        assert errors == []

    def test_undefined_role_is_a_validation_error(self):
        """A role that is neither 'same' nor a key in config.roles previously
        passed validation silently (verified) and reached the deployed task's
        payload as a raw, unresolved string — only failing at AWS runtime with
        a far less helpful error. It must be caught here instead."""
        from polyris import DAG, task

        with DAG("role-typo", schedule=None) as dag:
            @task.sfn(arn=ARN, role="prod-analytis-role")  # not a real config.roles key
            def t():
                pass
            t()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is False
        assert any("prod-analytis-role" in e and "role" in e for e in errors)

    def test_role_as_raw_arn_passthrough_is_valid(self):
        """polyris.roles' own documented usage (`role=roles["data_warehouse"]`)
        passes the *resolved ARN value* directly, not a config.roles key —
        _build_task_branch already accepts this (falls through unresolved-
        as-is when the value isn't found as a key), so the validator must not
        reject it. An earlier version of this check did reject it, based on
        the (incomplete) assumption that role is always a key, never a raw
        ARN — contradicting polyris.roles' own documented example."""
        from polyris import DAG, task

        with DAG("role-arn-passthrough", schedule=None) as dag:
            @task.sfn(arn=ARN, role="arn:aws:iam::111111111111:role/dw-role")
            def t():
                pass
            t()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is True
        assert errors == []

    def test_undefined_role_does_not_mask_a_structurally_valid_dag(self):
        """The role check must compose with the structural ASL check — an
        undefined role on an otherwise well-formed DAG should be the *only*
        reported error, not swallow or duplicate the structural pass."""
        from polyris import DAG, task

        with DAG("role-typo-2", schedule=None) as dag:
            @task.sfn(arn=ARN, role="not-a-real-role")
            def t():
                pass
            t()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is False
        assert len(errors) == 1


# ============================================================ #
# validate_all  (full pipeline)
# ============================================================ #
class TestValidateAll:
    def test_empty_directory_warns_no_files(self, tmp_path):
        res = validate_all(str(tmp_path), verbose=False)
        assert res["pipelines"] == []
        assert any("No pipeline files" in w for w in res["warnings"])

    def test_valid_pipeline_builds_graph_without_errors(self, tmp_path):
        _write(tmp_path, "prod/dag.py", ASSET_DAG)
        res = validate_all(str(tmp_path), verbose=False)
        assert res["errors"] == []
        assert res["pipelines"], "expected the producer DAG to be discovered"
        assert res["graph"] is not None
        assert "shop/orders" in res["graph"]["producers"]

    def test_orphan_trigger_asset_warns(self, tmp_path):
        _write(tmp_path, "cons/dag.py", ORPHAN_TRIGGER_DAG)
        res = validate_all(str(tmp_path), verbose=False)
        assert any("no producer" in w for w in res["warnings"])

    def test_cross_pipeline_cycle_reported_end_to_end(self, tmp_path):
        # Two asset-triggered pipelines forming a real loop:
        #   A (triggered by cyc/x) produces cyc/y
        #   B (triggered by cyc/y) produces cyc/x
        # The full discover→extract→graph→detect path must flag it. verbose=True
        # also exercises the error-summary reporting branch.
        cyc_a = """
            from polyris import DAG, task, Asset
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
            x = Asset("cyc/x"); y = Asset("cyc/y")
            with DAG(dag_id="cyc-a", schedule=x) as dag:
                @task.sfn(arn=ARN, outlets=[y])
                def a():
                    pass
                a()
        """
        cyc_b = """
            from polyris import DAG, task, Asset
            ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
            x = Asset("cyc/x"); y = Asset("cyc/y")
            with DAG(dag_id="cyc-b", schedule=y) as dag:
                @task.sfn(arn=ARN, outlets=[x])
                def b():
                    pass
                b()
        """
        _write(tmp_path, "a/dag.py", cyc_a)
        _write(tmp_path, "b/dag.py", cyc_b)
        res = validate_all(str(tmp_path), verbose=True)
        assert any("Cycle detected" in e for e in res["errors"]), res["errors"]


# ============================================================ #
# _validate_single
# ============================================================ #
class TestValidateSingle:
    def test_missing_file_is_invalid(self, tmp_path):
        assert _validate_single(str(tmp_path / "nope.py"), verbose=False) is False

    def test_valid_dag_file_is_valid(self, tmp_path):
        f = _write(tmp_path, "solo/dag.py", SOLO_DAG)
        assert _validate_single(str(f), verbose=False) is True

    def test_file_without_dag_is_invalid(self, tmp_path):
        f = _write(tmp_path, "nodag.py", "x = 1\ny = 2\n")
        assert _validate_single(str(f), verbose=False) is False
