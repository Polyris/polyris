"""Generator builder/render tests — ASCII rendering + rich-DAG serializers.

Pushes the remaining pure-logic in ``polyris.generators`` (CLAUDE.md #13):

  - ``render_dag_ascii``: empty/linear/convergent/trigger-rule rendering.
  - The wait_for / outlet / inlet serializers and the task-branch builders,
    exercised by generating ASL + DAG JSON for a DAG that uses every wait_for
    shape (Asset, ``.within``, ``.consecutive``, AND-group), enriched outlet
    metadata, ``skip_on_backfill`` and a non-default ``trigger_rule``.
"""
from __future__ import annotations

import json

from polyris import DAG, task, Asset, Column, types as t
from polyris.generators import (
    render_dag_ascii,
    generate_step_function_json,
    generate_dag_json,
    generate_mermaid,
    validate_asl,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


# ============================================================ #
# render_dag_ascii
# ============================================================ #
class TestRenderAscii:
    def test_empty_dag(self):
        with DAG("empty", schedule=None) as dag:
            pass
        assert render_dag_ascii(dag) == "  (empty DAG)"

    def test_linear_chain_renders_names_and_arrows(self):
        with DAG("chain", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def extract():
                pass

            @task.sfn(arn=ARN)
            def load():
                pass

            extract() >> load()
        out = render_dag_ascii(dag)
        assert "DAG: chain" in out
        assert "Tasks: 2" in out
        assert "extract" in out and "load" in out
        assert "→" in out

    def test_convergence_section(self):
        with DAG("conv", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass

            @task.sfn(arn=ARN)
            def b():
                pass

            @task.sfn(arn=ARN)
            def c():
                pass

            [a(), b()] >> c()
        out = render_dag_ascii(dag)
        assert "a + b ──→ c" in out

    def test_non_default_trigger_rule_is_annotated(self):
        with DAG("tr", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass

            @task.sfn(arn=ARN, trigger_rule="all_done")
            def b():
                pass

            a() >> b()
        out = render_dag_ascii(dag)
        assert "trigger_rule=all_done" in out


# ============================================================ #
# rich-DAG serializers + builders
# ============================================================ #
def _rich_dag():
    orders = Asset(
        "shop/orders",
        schema=[Column("id", t.bigint())],
        description="Order facts",
        owner="data-team",
        tags=["pii"],
        freshness_hours=24,
        glue_table="analytics.orders",
    )
    inv = Asset("shop/inventory")
    catalog = Asset("shop/catalog")
    with DAG("rich", schedule=None) as dag:
        @task.sfn(
            arn=ARN,
            outlets=[orders],
            inlets=[inv],
            wait_for=[inv.within(hours=12), catalog, inv.consecutive(days=3), (inv & catalog)],
            trigger_rule="all_done",
            skip_on_backfill=True,
        )
        def build():
            pass
        build()
    return dag


class TestRichGeneration:
    def test_rich_dag_generates_valid_asl(self):
        asl = json.loads(generate_step_function_json(_rich_dag()))
        ok, errors, _ = validate_asl(asl)
        assert ok, errors

    def test_dag_json_carries_outlet_and_inlet(self):
        blob = json.dumps(generate_dag_json(_rich_dag()))
        # Enriched outlet metadata and the inlet surface in the DAG JSON.
        assert "shop/orders" in blob
        assert "shop/inventory" in blob
        assert "all_done" in blob  # the non-default trigger rule
        assert "skip_on_backfill" in blob

    def test_mermaid_on_asset_dag(self):
        out = generate_mermaid(_rich_dag())
        assert isinstance(out, str)
        assert "graph" in out.lower()
