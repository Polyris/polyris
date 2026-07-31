"""DSL utility tests — chaining helpers, XCom args, ARN resolution.

Covers three small modules (CLAUDE.md #13):
  - ``polyris.helpers``: ``chain`` / ``cross_downstream`` wiring and the
    ``Label`` edge passthrough.
  - ``polyris.xcom``: ``XComArg`` id/repr.
  - ``polyris.resolver``: ``ARNResolver`` loading/resolving/validating ARNs and
    the ``set_resolver`` / ``get_resolver`` singleton.
"""
from __future__ import annotations

import polyris.resolver as resolver_mod
from polyris import DAG, task
from polyris.helpers import chain, cross_downstream, Label
from polyris.xcom import XComArg
from polyris.resolver import ARNResolver, set_resolver, get_resolver

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


# ============================================================ #
# resolver: ARNResolver + singleton
# ============================================================ #
class TestARNResolver:
    def test_resolve_passthrough_when_unknown_or_plain(self):
        r = ARNResolver()
        assert r.resolve("${unknown_arn}") == "${unknown_arn}"  # not in map
        assert r.resolve("arn:plain") == "arn:plain"            # not a template

    def test_loads_flat_and_nested_tasks_json(self, tmp_path):
        (tmp_path / "tasks.json").write_text(
            '{"foo_arn": "arn:aws:states:::sfn:foo", "bar": {"arn": "arn:bar"}}'
        )
        pipeline = tmp_path / "dag.py"
        pipeline.write_text("# pipeline\n")
        r = ARNResolver(pipeline)
        assert r.resolve("${foo_arn}") == "arn:aws:states:::sfn:foo"
        assert r.resolve("${bar}") == "arn:bar"  # nested {"arn": ...} form

    def test_missing_tasks_json_is_tolerated(self, tmp_path):
        pipeline = tmp_path / "dag.py"
        pipeline.write_text("# pipeline\n")
        r = ARNResolver(pipeline)  # no tasks.json next to it
        assert r.arns == {}

    def test_validate_reports_unresolved(self):
        with DAG("r", schedule=None):
            @task.sfn(arn="${missing_arn}")
            def t():
                pass
        assert ARNResolver().validate([t]) == ["missing_arn"]

    def test_set_and_get_resolver(self, tmp_path):
        (tmp_path / "tasks.json").write_text('{"k_arn": "arn:k"}')
        pipeline = tmp_path / "dag.py"
        pipeline.write_text("#\n")
        set_resolver(pipeline)
        assert get_resolver().resolve("${k_arn}") == "arn:k"

    def test_get_resolver_creates_default_when_unset(self):
        resolver_mod._resolver = None
        r = get_resolver()
        assert isinstance(r, ARNResolver)
        assert r.arns == {}
