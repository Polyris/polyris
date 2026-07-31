"""DAG-object tests — scheduling, graph methods.

Drives ``polyris.dag.DAG`` directly (CLAUDE.md #13):

  - ``__post_init__`` behaviour: the ``schedule_interval`` alias and
    ``trigger_assets`` → asset schedule.
  - Graph methods: ``topological_sort`` (ordering + cycle detection),
    ``roots``, ``leaves``, ``get_task`` / ``task_dict``.
  - The ``test()`` / ``cli()`` helpers.
"""
from __future__ import annotations

import pytest

from polyris import DAG, task, Asset

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _chain():
    """a >> b >> c; returns (dag, a, b, c) as Task objects."""
    with DAG("dag_chain", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def a():
            pass

        @task.sfn(arn=ARN)
        def b():
            pass

        @task.sfn(arn=ARN)
        def c():
            pass

        a() >> b() >> c()
    return dag, a, b, c


# ============================================================ #
# scheduling (__post_init__)
# ============================================================ #
class TestScheduling:
    def test_schedule_interval_alias(self):
        dag = DAG("d", schedule_interval="rate(1 hour)")
        assert dag.schedule == "rate(1 hour)"
        assert dag.is_asset_triggered is False

    def test_trigger_assets_any_makes_asset_triggered(self):
        dag = DAG("d", trigger_assets=[Asset("ns/x")], trigger_mode="any")
        assert dag.is_asset_triggered is True
        assert "ns/x" in str(dag.asset_schedule_info)

    def test_trigger_assets_all_is_default(self):
        dag = DAG("d", trigger_assets=[Asset("ns/x"), Asset("ns/y")])
        assert dag.is_asset_triggered is True
        info = str(dag.asset_schedule_info)
        assert "ns/x" in info and "ns/y" in info

    def test_asset_schedule_via_single_asset(self):
        dag = DAG("d", schedule=Asset("ns/z"))
        assert dag.is_asset_triggered is True

    def test_asset_schedule_via_bare_asset_ref(self):
        """Regression test: `schedule=asset.within(hours=N)` (no list, no
        plain Asset) previously fell through every type-check in both
        DAG.__post_init__'s local check and normalize_asset_schedule itself,
        leaving is_asset_triggered False and _eventbridge_schedule None —
        a pipeline with no trigger mechanism at all, deployed silently."""
        dag = DAG("d", schedule=Asset("ns/z").within(hours=6))
        assert dag.is_asset_triggered is True
        assert dag._eventbridge_schedule is None
        assert dag.asset_schedule_info == {
            "operator": "AND",
            "assets": [{"asset_name": "ns/z", "freshness_hours": 6}],
        }

    def test_asset_schedule_via_bare_consecutive_ref(self):
        dag = DAG("d", schedule=Asset("ns/z").consecutive(days=3))
        assert dag.is_asset_triggered is True

    def test_asset_schedule_list_with_ref_as_first_element(self):
        """Regression test: DAG.__post_init__ previously only inspected
        `schedule[0]`'s type to decide is_asset_based for a list, and only
        recognized Asset/AssetAll/AssetAny there — an AssetRef in the first
        position (e.g. `schedule=[asset_a.within(hours=1), asset_b]`) was
        invisible to that check, even though the identical list with the
        plain Asset first (`[asset_b, asset_a.within(hours=1)]`) worked
        correctly — an entirely arbitrary, unexplainable distinction from
        the user's point of view. Fixed by delegating to
        normalize_asset_schedule as the single source of truth instead of
        re-implementing a narrower, position-sensitive check."""
        dag = DAG("d", schedule=[Asset("ns/a").within(hours=1), Asset("ns/b")])
        assert dag.is_asset_triggered is True
        assert dag._eventbridge_schedule is None


# ============================================================ #
# graph methods
# ============================================================ #
class TestGraphMethods:
    def test_topological_sort_orders_deps_first(self):
        dag, a, b, c = _chain()
        order = [t.task_id for t in dag.topological_sort()]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_topological_sort_detects_cycle(self):
        with DAG("dag_cycle", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass

            @task.sfn(arn=ARN)
            def b():
                pass

            ai, bi = a(), b()
            ai >> bi
            bi >> ai  # close the loop
        with pytest.raises(ValueError, match="Cycle detected"):
            dag.topological_sort()

    def test_topological_sort_rejects_unregistered_dependency(self):
        with DAG("dag_unreg", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass

            ai = a()

        @task.sfn(arn=ARN)
        def orphan():
            pass

        ai.task.dependencies.append(orphan)  # a Task never added to the DAG
        with pytest.raises(ValueError, match="not added to this DAG"):
            dag.topological_sort()

    def test_plain_args_are_not_dependencies(self):
        with DAG("dag_plain", schedule=None):
            @task.sfn(arn=ARN)
            def a(x, y):
                pass

            ai = a("literal", 42)  # plain values, not XComArg / TaskInstance

        assert ai.task.dependencies == []

    def test_roots_have_no_task_deps(self):
        dag, a, b, c = _chain()
        assert [t.task_id for t in dag.roots()] == ["a"]

    def test_leaves_have_no_downstream(self):
        dag, a, b, c = _chain()
        assert [t.task_id for t in dag.leaves()] == ["c"]

    def test_get_task_hit_and_miss(self):
        dag, a, b, c = _chain()
        assert dag.get_task("a") is a
        assert dag.get_task("does-not-exist") is None

    def test_task_dict_maps_ids(self):
        dag, a, b, c = _chain()
        assert set(dag.task_dict.keys()) == {"a", "b", "c"}


# ============================================================ #
# DAG helpers
# ============================================================ #
class TestCompatHelpers:
    def test_cli_is_noop(self):
        dag, *_ = _chain()
        assert dag.cli() is None

    def test_test_runs_callables(self, capsys):
        dag, *_ = _chain()
        dag.test()
        out = capsys.readouterr().out
        assert "Testing DAG" in out

    def test_test_catches_callable_errors(self, capsys):
        with DAG("dag_boom", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def boom():
                raise RuntimeError("kaboom")
            boom()
        dag.test()  # must not propagate — the runner catches and prints
        assert "Error" in capsys.readouterr().out
