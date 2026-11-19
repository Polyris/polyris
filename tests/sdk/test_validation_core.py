"""Core validation-engine tests — graph, cycles, discovery, extraction.

Complements ``test_validation_schema.py`` (which exercises only
``validate_schema_consistency``). This module covers the rest of the
local-validation engine in ``polyris.validation``:

  - ``build_asset_graph``      — DAGInfo list → producer/consumer graph
  - ``detect_asset_cycles``    — BFS cycle detection over that graph
  - which files ``--all`` loads (``polyris.loader``)
  - ``_dag_infos``             — a loaded pipeline file → DAGInfo
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

import pytest

from polyris.validation import (
    DAGInfo,
    build_asset_graph,
    detect_asset_cycles,
    _dag_infos,
    validate_all,
    validate_asl_from_dag,
    _validate_single,
    _print_error,
)


def discover_pipeline_files(directory) -> list:
    """The files --all loads under ``directory`` (``polyris.loader``), as paths."""
    from polyris.loader import files_in, pipeline_folders
    return [str(p) for folder in pipeline_folders(directory) for p in files_in(folder).pipelines]


def extract_dag_info(path) -> list:
    """The DAG info of one file, loaded as every command loads it."""
    from polyris.loader import load_pipeline
    return _dag_infos(str(path), load_pipeline(path).dags)

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

    def test_finds_any_file_whose_code_imports_a_dag_maker_from_polyris_whatever_its_name(self, tmp_path):
        """The file's code (parsed, never run) imports DAG, dag or Pipeline from polyris."""
        _write(tmp_path, "p/orders.py", "from polyris import DAG\n")
        _write(tmp_path, "p/latest_prices.py", "from polyris.helpers import dag\n")
        _write(tmp_path, "p/inventory.py", "from polyris import Pipeline\n")
        _write(tmp_path, "p/aliased.py", "import polyris as p\nwith p.DAG('x'):\n    pass\n")
        _write(tmp_path, "p/starred.py", "from polyris import *\n")
        _write(tmp_path, "p/broken.py", "from polyris import DAG\nthis is not ) python\n")  # loaded: its error is reported
        found = {Path(f).name for f in discover_pipeline_files(str(tmp_path))}
        assert found == {"orders.py", "latest_prices.py", "inventory.py", "aliased.py", "starred.py", "broken.py"}

    def test_a_file_is_read_as_python_reads_it(self, tmp_path):
        """A BOM (or a coding line) failed the parse of decoded text, and the fallback ran a Glue
        script. ast reads the bytes, as Python does."""
        bom = b"\xef\xbb\xbf"
        folder = tmp_path / "p"
        folder.mkdir()
        (folder / "glue_bom.py").write_bytes(bom + b"import awsglue\nfrom polyris import xcom\n")
        (folder / "orders_bom.py").write_bytes(bom + b"from polyris import DAG\n")
        (folder / "latin.py").write_bytes(b"# coding: latin-1\nfrom polyris import xcom\nna\xefve = 1\n")
        found = {Path(f).name for f in discover_pipeline_files(str(tmp_path))}
        assert found == {"orders_bom.py"}

    def test_a_file_that_does_not_parse_is_loaded_only_when_it_imports_a_dag_maker(self, tmp_path):
        """Any broken file that mentioned polyris was loaded, so a Lambda handler written for a
        newer Python failed validate --all. Too deep for ast is the same case; an import over
        several lines still counts, a DAG named in a comment does not."""
        _write(tmp_path, "p/newer_python.py", "from polyris import xcom\ntype Rows[T = dict] = list[T]\n")
        _write(tmp_path, "p/broken_pipeline.py", "from polyris import DAG, task\nwith DAG('x') as\n")
        _write(tmp_path, "p/broken_aliased.py", "import polyris\nwith polyris.DAG('x') as\n")
        _write(tmp_path, "p/too_deep.py", "from polyris import xcom\nx = " + "+".join(["1"] * 20000) + "\n")
        _write(tmp_path, "p/broken_multiline.py", "from polyris import (\n    task,\n    DAG,\n)\nwith DAG('x') as\n")
        _write(tmp_path, "p/broken_comment.py", "from polyris import xcom  # used by the DAG\nwith x as\n")
        found = {Path(f).name for f in discover_pipeline_files(str(tmp_path))}
        assert found == {"broken_pipeline.py", "broken_aliased.py", "broken_multiline.py"}

    def test_a_star_import_counts_only_from_a_module_that_exports_a_dag_maker(self, tmp_path):
        _write(tmp_path, "p/handler.py", "from polyris.xcom import *\n")
        _write(tmp_path, "p/orders.py", "from polyris.dag import *\n")
        _write(tmp_path, "p/orders2.py", "from polyris import *\n")
        found = {Path(f).name for f in discover_pipeline_files(str(tmp_path))}
        assert found == {"orders.py", "orders2.py"}

    def test_a_comment_or_string_naming_a_dag_does_not_make_a_pipeline(self, tmp_path):
        """ "polyris" and "dag" in the text matched a Glue script's comment."""
        _write(tmp_path, "p/transform_spark.py",
               "# Spark step of the all-task-types DAG\nimport awsglue\nfrom polyris import xcom\n"
               "NAME = os.environ['POLYRIS_PIPELINE_NAME']  # 'DAG'\n")
        _write(tmp_path, "p/uses_module.py", "import polyris\npolyris.xcom.push('k', 1)\n")
        _write(tmp_path, "p/not_python.py", "this is not ) python, and names no DAG\n")
        assert discover_pipeline_files(str(tmp_path)) == []

    def test_a_file_that_does_not_mention_polyris_is_not_run(self, tmp_path):
        # A Glue script, a migration: never executed by --all, however it is named
        _write(tmp_path, "p/glue_job_pipeline.py", "import awsglue\n")
        _write(tmp_path, "p/transform.py", "import awsglue\nfrom polyris import xcom\nos.environ['POLYRIS_PIPELINE_NAME']\n")  # a real xcom script
        _write(tmp_path, "p/thing.py", "from x import DAG\nwith DAG('a'):\n    pass\n")
        found = discover_pipeline_files(str(tmp_path))
        assert found == []

    def test_excludes_tests_pycache_hidden_folders_and_plain_files(self, tmp_path):
        _write(tmp_path, "p/test_thing.py", "from polyris import DAG\n")
        _write(tmp_path, "p/thing_test.py", "from polyris import DAG\n")
        _write(tmp_path, "p/conftest.py", "from polyris import DAG\n")
        _write(tmp_path, "__pycache__/cached.py", "from polyris import DAG\n")
        _write(tmp_path, ".venv/lib/polyris_plugin.py", "from polyris import DAG\n")
        _write(tmp_path, "p/plain.py", "x = 1\n")
        assert discover_pipeline_files(str(tmp_path)) == []

    def test_only_the_folders_directly_in_the_run_directory_are_loaded(self, tmp_path):
        """D110: validate --all loads what deploy --all deploys; the rest is warned about."""
        from polyris.loader import pipelines_out_of_reach
        _write(tmp_path, "dag.py", "from polyris import DAG\n")
        _write(tmp_path, "orders/dag.py", "from polyris import DAG\n")
        _write(tmp_path, "team/billing/dag.py", "from polyris import DAG\n")
        _write(tmp_path, ".venv/x/dag.py", "from polyris import DAG\n")
        assert discover_pipeline_files(str(tmp_path)) == [str(tmp_path / "orders" / "dag.py")]
        assert pipelines_out_of_reach(tmp_path) == [tmp_path / "dag.py", tmp_path / "team" / "billing" / "dag.py"]

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

    def test_broken_file_raises(self, tmp_path):
        # extract_dag_info must surface import failures so validate_all can
        # append them to results['errors'] rather than silently using partial data.
        f = _write(tmp_path, "broken/dag.py", BROKEN_DAG)
        with pytest.raises(Exception, match="boom at import time"):
            extract_dag_info(str(f))


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
        the task's spec already accepts this (falls through unresolved-
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

    def test_placeholder_arn_account_id_is_invalid(self):
        """ARNs containing ACCOUNT_ID (generated by polyris-init) must be
        caught before deploy — not silently passed to CloudFormation."""
        from polyris import DAG, task

        placeholder_arn = "arn:aws:states:REGION:ACCOUNT_ID:stateMachine:my-pipeline-extract"
        with DAG("placeholder-test", schedule=None) as dag:
            @task.sfn(arn=placeholder_arn)
            def extract():
                pass
            extract()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is False
        assert any("placeholder" in e.lower() for e in errors)
        assert any("extract" in e for e in errors)

    def test_real_arn_is_not_flagged_as_placeholder(self):
        """A real AWS ARN (with a numeric account ID) must not trigger the
        placeholder check — only the literal sentinel tokens ACCOUNT_ID /
        REGION generated by polyris-init are blocked."""
        is_valid, errors, _ = validate_asl_from_dag(self._build_chain())
        assert is_valid is True
        assert not any("placeholder" in e.lower() for e in errors)

    def test_pipeline_named_region_not_falsely_blocked(self):
        """A pipeline whose name contains 'REGION' or 'ACCOUNT_ID' must not
        be blocked after the user replaces the placeholder segments. The check
        must scope to the region (segment 4) and account (segment 5) only."""
        from polyris import DAG, task

        # Simulates: user ran polyris-init REGION-processor and then replaced
        # the actual region/account placeholders with real AWS values.
        real_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:REGION-processor-extract"
        with DAG("REGION-processor", schedule=None) as dag:
            @task.sfn(arn=real_arn)
            def extract():
                pass
            extract()
        is_valid, errors, _ = validate_asl_from_dag(dag)
        assert is_valid is True
        assert not any("placeholder" in e.lower() for e in errors)


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

    def test_broken_pipeline_error_in_results(self, tmp_path):
        # A pipeline that raises on import must appear in errors[], not be silently
        # skipped or cause partial/stale DAGInfo to pollute the graph.
        _write(tmp_path, "broken/dag.py", BROKEN_DAG)
        res = validate_all(str(tmp_path), verbose=False)
        assert any("boom at import time" in e for e in res["errors"])
        assert res["pipelines"] == []

    def test_broken_and_valid_pipeline_continue(self, tmp_path):
        # validate_all must continue processing after one file fails —
        # the valid pipeline's DAGInfo appears in results['pipelines'] and
        # the broken file's error appears in results['errors'].
        _write(tmp_path, "good/dag.py", SOLO_DAG)
        _write(tmp_path, "bad/dag.py", BROKEN_DAG)
        res = validate_all(str(tmp_path), verbose=False)
        assert len(res["pipelines"]) == 1
        assert res["pipelines"][0].dag_id == "solo"
        assert any("boom at import time" in e for e in res["errors"])

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
        valid, _ = _validate_single(str(tmp_path / "nope.py"), verbose=False)
        assert valid is False

    def test_valid_dag_file_is_valid(self, tmp_path):
        f = _write(tmp_path, "solo/dag.py", SOLO_DAG)
        valid, errors = _validate_single(str(f), verbose=False)
        assert valid is True
        assert errors == []

    def test_file_without_dag_is_invalid(self, tmp_path):
        f = _write(tmp_path, "nodag.py", "x = 1\ny = 2\n")
        valid, _ = _validate_single(str(f), verbose=False)
        assert valid is False


class TestPrintError:
    def test_single_line_error_printed_in_red(self, mocker, capsys):
        mocker.patch("sys.stdout.isatty", return_value=True)
        _print_error("role 'bad' is not recognised")
        out = capsys.readouterr().out
        assert "role 'bad' is not recognised" in out
        assert "\033[31m" in out  # RED

    def test_two_line_error_prints_arn_in_yellow(self, mocker, capsys):
        mocker.patch("sys.stdout.isatty", return_value=True)
        _print_error(
            "Task 'x' has a placeholder ARN — replace REGION\n"
            "     arn:aws:states:REGION:ACCOUNT_ID:stateMachine:x"
        )
        out = capsys.readouterr().out
        assert "\033[33m" in out  # YELLOW on ARN line
        assert "arn:aws:states:REGION" in out

    def test_no_ansi_when_not_a_tty(self, mocker, capsys):
        mocker.patch("sys.stdout.isatty", return_value=False)
        _print_error("some error\n     arn:aws:states:REGION:x:y:z")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "some error" in out
        assert "arn:aws:states:REGION" in out
