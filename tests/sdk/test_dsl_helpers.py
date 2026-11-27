"""DSL utility tests — chaining helpers and XCom args.

Covers two small modules (CLAUDE.md #13):
  - ``polyris.helpers``: ``chain`` / ``cross_downstream`` wiring and the
    ``Label`` edge passthrough.
  - ``polyris.xcom``: ``XComArg`` id/repr.
"""
from __future__ import annotations

import pytest

from polyris import DAG, task
from polyris.helpers import chain, cross_downstream, Label
from polyris.xcom import XComArg

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _four_tasks(dag_id="h"):
    with DAG(dag_id, schedule=None):
        @task.sfn(arn=ARN)
        def a():
            pass

        @task.sfn(arn=ARN)
        def b():
            pass

        @task.sfn(arn=ARN)
        def c():
            pass

        @task.sfn(arn=ARN)
        def d():
            pass
    return a, b, c, d


# ============================================================ #
# helpers: chain / cross_downstream / Label
# ============================================================ #
class TestChain:
    def test_linear_chain(self):
        a, b, c, _ = _four_tasks()
        chain(a, b, c)
        assert a in b.dependencies
        assert b in c.dependencies

    def test_chain_with_scalar_then_list_then_scalar(self):
        a, b, c, d = _four_tasks()
        chain(a, [b, c], d)
        assert a in b.dependencies and a in c.dependencies
        assert b in d.dependencies and c in d.dependencies

    def test_chain_pairwise_lists(self):
        a, b, c, d = _four_tasks()
        chain([a, b], [c, d])  # pairwise: a>>c, b>>d
        assert a in c.dependencies
        assert b in d.dependencies
        assert a not in d.dependencies

    def test_chain_mismatched_lists_raises(self):
        """B-6: chain([a, b], [c]) must raise — lengths differ."""
        a, b, c, _ = _four_tasks()
        with pytest.raises((ValueError, TypeError)):
            chain([a, b], [c])

    def test_chain_invalid_type_raises(self):
        """B-7: hasattr guards removed — chain("x", task) must raise, not silently no-op."""
        a, *_ = _four_tasks()
        with pytest.raises((TypeError, AttributeError)):
            chain("not_a_task", a)


class TestCrossDownstream:
    def test_full_cross(self):
        a, b, c, d = _four_tasks()
        cross_downstream([a, b], [c, d])
        for up in (a, b):
            assert up in c.dependencies
            assert up in d.dependencies


class TestLabel:
    def test_label_passes_dependency_through(self):
        a, b, *_ = _four_tasks()
        ai, bi = a(), b()
        ai >> Label("when ready") >> bi
        # The label is cosmetic; the real edge a→b is still created.
        assert a in b.dependencies

    def test_repr(self):
        assert repr(Label("ok")) == "Label('ok')"


# ============================================================ #
# xcom: XComArg
# ============================================================ #
class TestXComArg:
    def test_task_id_and_repr(self):
        a, *_ = _four_tasks()
        arg = XComArg(a())
        assert arg.task_id == "a"
        assert "a" in repr(arg)
        assert "return_value" in repr(arg)
