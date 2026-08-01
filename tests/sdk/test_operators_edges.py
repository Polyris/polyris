"""Operator edge-case tests — list branches, unsupported operands, registration.

Closes the remaining operator branches in ``steps`` and ``task`` (CLAUDE.md #13):
the list-of-mixed-targets paths, the ``NotImplemented`` returns for unsupported
operands (which surface as ``TypeError``), and the ``if dag: dag.add_step(self)``
registration branch reached only when a control-flow step is built inside a DAG.
"""
from __future__ import annotations

import pytest

from polyris import DAG, task
from polyris.task import Task
from polyris.task_group import TaskGroup
from polyris.steps import Wait, Pass, Choice, Map, Sensor, ShortCircuit, Succeed

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _two_tasks(dag_id="ope"):
    with DAG(dag_id, schedule=None):
        @task.sfn(arn=ARN)
        def a():
            pass

        @task.sfn(arn=ARN)
        def b():
            pass
    return a, b


# ============================================================ #
# Step operators — list + unsupported operands
# ============================================================ #
class TestStepOperatorEdges:
    def test_rshift_list_of_task_instances(self):
        a, b = _two_tasks()
        w = Wait(seconds=5)
        w >> [a(), b()]  # TaskInstances in a list → hasattr('task') branch
        assert w in a.dependencies and w in b.dependencies

    def test_rshift_unsupported_raises(self):
        with pytest.raises(TypeError):
            Wait(seconds=5) >> 42

    def test_lshift_list(self):
        a, b = _two_tasks()
        p = Pass()
        p << [a, b]
        assert a in p.dependencies and b in p.dependencies

    def test_lshift_unsupported_raises(self):
        with pytest.raises(TypeError):
            Pass() << 42

    def test_rrshift_unsupported_raises(self):
        with pytest.raises(TypeError):
            42 >> Pass()


# ============================================================ #
# Control-flow steps register when built inside a DAG
# ============================================================ #
class TestStepRegistrationInsideDag:
    def test_steps_register_with_active_dag(self):
        with DAG("reg", schedule=None) as dag:
            Choice(default=Pass())
            Map(items_path="$.x")
            Sensor(sensor_type="s3")
            ShortCircuit()
            Succeed()
        step_ids = {s.step_id for s in dag.steps}
        assert {"Choice", "Map", "Sensor_s3", "ShortCircuit", "Succeed"} <= step_ids


# ============================================================ #
# TaskInstance operators — TaskGroup, step-in-list, unsupported
# ============================================================ #
class TestTaskInstanceOperatorEdges:
    def test_ti_rshift_taskgroup(self):
        with DAG("tig", schedule=None):
            @task.sfn(arn=ARN)
            def up():
                pass

            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def inner():
                    pass
                inner()
            up() >> grp
        assert any(d.task_id == "up" for d in grp.roots[0].dependencies)

    def test_ti_rshift_step_in_list(self):
        with DAG("tis", schedule=None):
            @task.sfn(arn=ARN)
            def up():
                pass
            s = Pass()
            up() >> [s]
        assert any(getattr(d, "task_id", None) == "up" for d in s.dependencies)

    def test_step_in_list_rshift_ti(self):
        with DAG("sli", schedule=None):
            @task.sfn(arn=ARN)
            def down():
                pass
            s = Pass()
            [s] >> down()
        assert s in down.dependencies

    def test_ti_rrshift_unsupported_raises(self):
        with DAG("tir", schedule=None):
            @task.sfn(arn=ARN)
            def down():
                pass
            inst = down()
        with pytest.raises(TypeError):
            42 >> inst

    # ---------------------------------------------------------------- #
    # __lshift__ (<<) — regression tests for two real bugs found in a
    # code-review pass: (1) every branch returned `self` instead of
    # `other`, silently breaking 3+-item chains like `c << b << a`
    # (e.g. `load << transform << extract`);
    # (2) TaskGroup/Step/Label all raised AttributeError on `<<`, even
    # though `>>` supported all of them — the two operators were not
    # actually equivalent ways to write the same edge.
    # ---------------------------------------------------------------- #

    def test_ti_lshift_chain_of_three(self):
        """The core bug: `c << b << a` must produce a -> b -> c, not
        silently skip `b` out of the chain (which is what returning `self`
        instead of `other` from every branch previously did)."""
        with DAG("lchain", schedule=None):
            @task.sfn(arn=ARN)
            def a():
                pass
            @task.sfn(arn=ARN)
            def b():
                pass
            @task.sfn(arn=ARN)
            def c():
                pass
            ia, ib, ic = a(), b(), c()
            ic << ib << ia
        assert [d.task_id for d in ic.task.dependencies] == ["b"]
        assert [d.task_id for d in ib.task.dependencies] == ["a"]
        assert ia.task.dependencies == []

    def test_ti_lshift_taskgroup(self):
        """Mirror of test_ti_rshift_taskgroup: `down() << group` connects
        down as downstream of all the group's leaves (not roots — << is the
        reverse direction)."""
        with DAG("tig2", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def inner():
                    pass
                inner()
            @task.sfn(arn=ARN)
            def down():
                pass
            di = down()
            di << grp
        assert any(d.task_id == "g.inner" for d in di.task.dependencies)

    def test_ti_lshift_step(self):
        with DAG("tis2", schedule=None):
            @task.sfn(arn=ARN)
            def down():
                pass
            s = Pass()
            di = down()
            di << s
        assert s in di.task.dependencies

    def test_ti_lshift_step_in_list(self):
        with DAG("tisl", schedule=None):
            @task.sfn(arn=ARN)
            def down():
                pass
            s = Pass()
            di = down()
            di << [s]
        assert s in di.task.dependencies

    def test_ti_lshift_label_completes_reverse_chain(self):
        """`down << Label(...) << up` must produce the same edge as
        `up >> Label(...) >> down` — previously crashed with AttributeError
        since Label had no `_set_downstream` (or any << support at all)."""
        from polyris.helpers import Label

        with DAG("lbl2", schedule=None):
            @task.sfn(arn=ARN)
            def up():
                pass
            @task.sfn(arn=ARN)
            def down():
                pass
            ui, di = up(), down()
            di << Label("on success") << ui
        assert [d.task_id for d in di.task.dependencies] == ["up"]


# ============================================================ #
# Task (decorated, pre-call) operators
# ============================================================ #
class TestTaskOperatorEdges:
    def test_task_rshift_unsupported_raises(self):
        a, _ = _two_tasks()
        with pytest.raises(TypeError):
            a >> 42

    def test_task_lshift_list(self):
        a, b = _two_tasks()
        a << [b]
        assert b in a.dependencies

    def test_task_lshift_chain_of_three(self):
        """Same bug as TaskInstance.__lshift__, one level down: raw Task's
        __lshift__ also returned `self` instead of `other`, breaking
        `c << b << a` chains (silently dropping `b` — verified with a
        directly-constructed Task chain, not just the TaskInstance path)."""
        with DAG("task-chain", schedule=None):
            a = Task(task_id="a")
            b = Task(task_id="b")
            c = Task(task_id="c")
        c << b << a
        assert [d.task_id for d in c.dependencies] == ["b"]
        assert [d.task_id for d in b.dependencies] == ["a"]

    def test_task_lshift_unsupported_raises(self):
        a, _ = _two_tasks()
        with pytest.raises(TypeError):
            a << 42

    def test_task_rrshift_unsupported_raises(self):
        a, _ = _two_tasks()
        with pytest.raises(TypeError):
            42 >> a

    def test_lambda_without_target_raises(self):
        with pytest.raises(ValueError):
            task.lambda_()
