"""Step-class tests — operators and step identity.

Covers the remaining pure-logic in ``polyris.steps`` (CLAUDE.md #13):

  - The base ``Step`` ``>>`` / ``<<`` operators across Step / Task /
    TaskInstance / TaskGroup / list targets (dependency wiring).
"""
from __future__ import annotations

from polyris import DAG, task
from polyris.task_group import TaskGroup
from polyris.steps import (
    Wait,
    Pass,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _two_tasks(dag_id="st"):
    with DAG(dag_id, schedule=None):
        @task.sfn(arn=ARN)
        def a():
            pass

        @task.sfn(arn=ARN)
        def b():
            pass
    return a, b


# ============================================================ #
# Step operators
# ============================================================ #
class TestStepOperators:
    def test_node_id_is_step_id(self):
        assert Wait(seconds=5).node_id == "Wait_5s"

    def test_step_rshift_task(self):
        a, _ = _two_tasks()
        w = Wait(seconds=5)
        w >> a
        assert w in a.dependencies

    def test_step_rshift_step(self):
        w, p = Wait(seconds=5), Pass()
        w >> p
        assert w in p.dependencies

    def test_step_rshift_taskinstance(self):
        a, _ = _two_tasks()
        w = Wait(seconds=5)
        w >> a()
        assert w in a.dependencies

    def test_step_rshift_list(self):
        a, b = _two_tasks()
        w = Wait(seconds=5)
        w >> [a, b]
        assert w in a.dependencies and w in b.dependencies

    def test_step_rshift_taskgroup(self):
        w = Wait(seconds=5)
        with DAG("sg", schedule=None):
            with TaskGroup("g") as grp:
                @task.sfn(arn=ARN)
                def a():
                    pass
                a()
        w >> grp
        assert w in grp.roots[0].dependencies

    def test_step_lshift_task(self):
        a, _ = _two_tasks()
        p = Pass()
        p << a
        assert a in p.dependencies

    def test_list_rshift_step(self):
        w1, w2, target = Wait(seconds=5), Wait(seconds=10), Pass()
        [w1, w2] >> target
        assert w1 in target.dependencies and w2 in target.dependencies


# ============================================================ #
# identity semantics — Step and subclasses must use identity eq
# ============================================================ #

class TestStepIdentity:
    """Step objects must be hashable and use identity-based equality (B-2).

    Plain @dataclass(eq=True) makes Pass() == Pass() True, so
    'if step not in deps' silently discards distinct edges.
    """

    def test_step_is_hashable(self):
        p = Pass()
        assert hash(p) is not None

    def test_step_subclass_is_hashable(self):
        w = Wait(seconds=5)
        assert hash(w) is not None

    def test_two_pass_steps_with_same_fields_are_not_equal(self):
        p1 = Pass()
        p2 = Pass()
        assert p1 is not p2
        assert p1 != p2  # identity-based: different objects → not equal

    def test_step_can_be_used_in_set(self):
        p1 = Pass()
        p2 = Pass()
        s = {p1, p2}
        assert len(s) == 2  # two distinct objects, even with same field values

    def test_same_step_object_equals_itself(self):
        p = Pass()
        assert p == p

    def test_step_dependency_uses_identity(self):
        with DAG("step_id_dag", schedule=None):
            @task.sfn(arn=ARN)
            def a():
                pass

            p = Pass()
            p >> a()

        assert p in a.dependencies
        # A different Pass() with same fields must NOT appear as a dependency
        other_pass = Pass()
        assert other_pass not in a.dependencies
