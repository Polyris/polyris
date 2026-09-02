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

    def test_invalid_schedule_type_raises(self):
        """Regression: non-str, non-Asset schedule types must raise immediately.

        Previously a timedelta/int/tuple/empty-list fell through both branches
        in __post_init__ leaving _asset_schedule=None and
        _eventbridge_schedule=None — the pipeline deployed but never ran.
        """
        import datetime
        with pytest.raises(TypeError, match="schedule"):
            DAG("d", schedule=datetime.timedelta(hours=1))

    def test_invalid_schedule_int_raises(self):
        with pytest.raises(TypeError, match="schedule"):
            DAG("d", schedule=42)

    def test_invalid_schedule_empty_list_raises(self):
        with pytest.raises(TypeError, match="schedule"):
            DAG("d", schedule=[])

    def test_none_schedule_is_valid(self):
        """schedule=None means manually-triggered — must not raise."""
        dag = DAG("d", schedule=None)
        assert dag.is_asset_triggered is False
        assert dag._eventbridge_schedule is None

    def test_invalid_trigger_mode_raises(self):
        """B-17: trigger_mode must be 'all' or 'any'; anything else silently
        defaulted to 'all' behaviour before this fix."""
        with pytest.raises(ValueError, match="trigger_mode"):
            DAG("d", trigger_assets=[Asset("ns/x")], trigger_mode="ANY")

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
class TestDuplicateTaskId:
    """Regression tests for B-1: duplicate task_id must be caught at add_task time."""

    def test_duplicate_task_id_raises_immediately(self):
        with pytest.raises(ValueError, match="Duplicate task_id"):
            with DAG("dag_dup", schedule=None):
                @task.sfn(arn=ARN)
                def a():
                    pass

                @task.sfn(arn=ARN)
                def a():  # noqa: F811 — intentional redefinition for test
                    pass

                a()
                a()

    def test_duplicate_task_id_error_names_the_id(self):
        with pytest.raises(ValueError, match="my_task"):
            with DAG("dag_dup2", schedule=None):
                @task.sfn(arn=ARN, task_id="my_task")
                def first():
                    pass

                @task.sfn(arn=ARN, task_id="my_task")
                def second():
                    pass

                first()
                second()

    def test_same_task_object_added_twice_is_idempotent(self):
        """Adding the exact same Task object twice must NOT raise — idempotency."""
        with DAG("dag_idem", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def a():
                pass

            ai = a()
            dag.add_task(ai.task)  # second add of the same object

        assert len(dag.tasks) == 1

    def test_duplicate_task_id_within_task_group_raises(self):
        """Regression: two tasks with the same name in one TaskGroup must raise.

        Previously the duplicate check ran before task_group.add_task applied
        the group prefix, so both tasks registered as 'a' → passed the check →
        then both were renamed to 'group.a', silently producing two tasks with
        the same id in dag.tasks.
        """
        from polyris.task_group import TaskGroup
        with pytest.raises(ValueError, match="Duplicate task_id"):
            with DAG("dag_dup_grp", schedule=None):
                with TaskGroup("grp"):
                    @task.sfn(arn=ARN)
                    def a():
                        pass

                    @task.sfn(arn=ARN)
                    def a():  # noqa: F811
                        pass

    def test_task_with_same_prefixed_id_as_existing_group_task_raises(self):
        """A task outside a group whose explicit task_id collides with an already-prefixed
        group task must raise — caught by dag.add_task since the prefixed id is already
        in task_dict when the outside task is added."""
        from polyris.task_group import TaskGroup
        with pytest.raises(ValueError, match="Duplicate task_id"):
            with DAG("dag_dup_cross", schedule=None):
                with TaskGroup("grp"):
                    @task.sfn(arn=ARN)
                    def clash():
                        pass

                @task.sfn(arn=ARN, task_id="grp.clash")
                def second():
                    pass


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


# ============================================================ #
# identity semantics — DAG must use identity, not value equality
# ============================================================ #

class TestDAGIdentity:
    """DAG objects must be hashable and use identity-based equality (B-2)."""

    def test_dag_is_hashable(self):
        dag = DAG("hash_check", schedule=None)
        assert hash(dag) is not None

    def test_dag_can_be_used_in_set(self):
        dag1 = DAG("dag_set_1", schedule=None)
        dag2 = DAG("dag_set_2", schedule=None)
        s = {dag1, dag2}
        assert len(s) == 2

    def test_two_dags_with_same_id_are_not_equal(self):
        dag1 = DAG("same_id", schedule=None)
        dag2 = DAG("same_id", schedule=None)
        assert dag1 is not dag2
        assert dag1 != dag2

    def test_same_dag_equals_itself(self):
        dag = DAG("self_eq", schedule=None)
        assert dag == dag
