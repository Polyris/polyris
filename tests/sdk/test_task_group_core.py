"""TaskGroup tests — grouping, prefixing, roots/leaves, operators, decorator.

Drives ``polyris.task_group`` through the real DSL (CLAUDE.md #13): tasks are
defined inside ``with TaskGroup(...)`` under a ``with DAG(...)`` so the
``DAG.add_task`` → ``TaskGroup.add_task`` routing (prefixing + membership) runs
exactly as it does for users.
"""
from __future__ import annotations

from polyris import DAG, task
from polyris.task_group import TaskGroup, task_group

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


# ============================================================ #
# context, membership, prefixing, roots/leaves
# ============================================================ #
class TestTaskGroupMembership:
    def test_tasks_are_added_and_prefixed(self):
        with DAG("d", schedule=None):
            with TaskGroup("extract") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass

                @task.sfn(arn=ARN)
                def b():
                    pass

                a()
                b()
        assert {t.task_id for t in grp._tasks} == {"extract.a", "extract.b"}
        assert a.task_id == "extract.a"

    def test_prefix_can_be_disabled(self):
        with DAG("d", schedule=None):
            with TaskGroup("g", prefix_group_id=False):
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
        assert a.task_id == "a"

    def test_roots_and_leaves_within_group(self):
        with DAG("d", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass

                @task.sfn(arn=ARN)
                def b():
                    pass

                a() >> b()
        assert [t.task_id for t in grp.roots] == ["g.a"]
        assert [t.task_id for t in grp.leaves] == ["g.b"]

    def test_context_clears_current_group_on_exit(self):
        with DAG("d", schedule=None) as dag:
            with TaskGroup("g"):
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
            # After the group context exits, the DAG has no active group.
            assert dag._current_task_group is None


# ============================================================ #
# operators
# ============================================================ #
class TestTaskGroupOperators:
    def test_group_rshift_taskinstance_connects_leaves(self):
        with DAG("d", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass

                @task.sfn(arn=ARN)
                def b():
                    pass

                a() >> b()

            @task.sfn(arn=ARN)
            def after():
                pass
            ai = after()
        grp >> ai
        # leaf (g.b) becomes an upstream dependency of `after`
        assert b in ai.task.dependencies

    def test_group_rshift_task_object(self):
        with DAG("d", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()

            @task.sfn(arn=ARN)
            def after():
                pass
        grp >> after
        assert a in after.dependencies

    def test_group_rshift_list(self):
        with DAG("d", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()

            @task.sfn(arn=ARN)
            def x():
                pass

            @task.sfn(arn=ARN)
            def y():
                pass
            xi, yi = x(), y()
        grp >> [xi, yi]
        assert a in xi.task.dependencies
        assert a in yi.task.dependencies

    def test_group_rshift_group(self):
        with DAG("d", schedule=None):
            with TaskGroup("g1") as g1:
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
            with TaskGroup("g2") as g2:
                @task.sfn(arn=ARN)
                def b():
                    pass
                b()
        g1 >> g2
        # g1 leaf (a) becomes a dependency of g2 root (b)
        assert a in b.dependencies

    def test_group_lshift_taskinstance(self):
        with DAG("d", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()

            @task.sfn(arn=ARN)
            def before():
                pass
            bi = before()
        grp << bi
        # `before` becomes an upstream dependency of the group root (a)
        assert before in a.dependencies

    def test_group_lshift_group(self):
        with DAG("d", schedule=None):
            with TaskGroup("g1") as g1:
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
            with TaskGroup("g2") as g2:
                @task.sfn(arn=ARN)
                def b():
                    pass
                b()
        g2 << g1
        # g1 leaf (a) connects into g2 root (b)
        assert a in b.dependencies


# ============================================================ #
# task_group decorator
# ============================================================ #
class TestTaskGroupDecorator:
    def test_decorator_returns_group_with_tasks(self):
        with DAG("d", schedule=None):
            @task_group(group_id="extract")
            def extract():
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
            grp = extract()
        assert isinstance(grp, TaskGroup)
        assert grp.group_id == "extract"
        assert any(t.task_id == "extract.a" for t in grp._tasks)

    def test_decorator_defaults_group_id_to_func_name(self):
        with DAG("d", schedule=None):
            @task_group()
            def my_stage():
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
            grp = my_stage()
        assert grp.group_id == "my_stage"
