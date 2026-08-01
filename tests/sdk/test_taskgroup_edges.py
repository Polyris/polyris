"""TaskGroup operator edges — Task-in-list, group << Task, unsupported operands.

Closes the remaining ``TaskGroup`` operator branches (CLAUDE.md #13): a bare
``Task`` object as a downstream list element, ``group << task`` wiring the task
ahead of every root, and the ``NotImplemented`` returns for unsupported operands.
"""
from __future__ import annotations

import pytest

from polyris import DAG, task
from polyris.task_group import TaskGroup

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _group_with_inner(dag):
    with TaskGroup("g") as grp:
        @task.sfn(arn=ARN)
        def inner():
            pass
        inner()
    return grp


class TestTaskGroupOperatorEdges:
    def test_group_rshift_task_in_list(self):
        with DAG("tg1", schedule=None):
            grp = _group_with_inner("tg1")

            @task.sfn(arn=ARN)
            def after():
                pass
            grp >> [after]  # bare Task in the list → Task branch
        assert grp.leaves[0] in after.dependencies

    def test_group_rshift_unsupported_raises(self):
        with DAG("tg2", schedule=None):
            grp = _group_with_inner("tg2")
        with pytest.raises(TypeError):
            grp >> 42

    def test_group_lshift_task(self):
        with DAG("tg3", schedule=None):
            grp = _group_with_inner("tg3")

            @task.sfn(arn=ARN)
            def before():
                pass
            grp << before  # Task connects ahead of every root
        assert before in grp.roots[0].dependencies

    def test_group_lshift_unsupported_raises(self):
        with DAG("tg4", schedule=None):
            grp = _group_with_inner("tg4")
        with pytest.raises(TypeError):
            grp << 42

    def test_group_lshift_chain_of_three(self):
        """Regression test: every __lshift__ branch previously returned
        `self` instead of `other`, breaking `group << b << a` — Python
        evaluates left-to-right as `(group << b) << a`, and returning `self`
        from the first op means the second op becomes `group << a` again,
        connecting BOTH b and a directly to the group's roots instead of
        chaining a -> b -> group. Verified reproducible before the fix."""
        with DAG("tg5", schedule=None):
            grp = _group_with_inner("tg5")

            @task.sfn(arn=ARN)
            def b():
                pass

            @task.sfn(arn=ARN)
            def a():
                pass
            bi, ai = b(), a()
            grp << bi << ai

        assert [d.task_id for d in grp.roots[0].dependencies] == ["b"]
        assert [d.task_id for d in bi.task.dependencies] == ["a"]

    def test_group_lshift_group_chain_of_three(self):
        """Same fix, TaskGroup-to-TaskGroup: `g1 << g2 << g3` must chain
        g3 -> g2 -> g1, not connect g2 and g3 both directly to g1's roots."""
        with DAG("tg6", schedule=None):
            with TaskGroup("g1") as g1:
                @task.sfn(arn=ARN)
                def g1_task():
                    pass
                g1_task()
            with TaskGroup("g2") as g2:
                @task.sfn(arn=ARN)
                def g2_task():
                    pass
                g2_task()
            with TaskGroup("g3") as g3:
                @task.sfn(arn=ARN)
                def g3_task():
                    pass
                g3_task()

            g1 << g2 << g3

        assert [d.task_id for d in g1.roots[0].dependencies] == ["g2.g2_task"]
        assert [d.task_id for d in g2.roots[0].dependencies] == ["g3.g3_task"]
