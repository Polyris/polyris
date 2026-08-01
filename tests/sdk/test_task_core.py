"""Task-system tests — dependency wiring, computed properties, decorators.

Drives the real DSL surface in ``polyris.task`` (CLAUDE.md #13):

  - ``TaskInstance`` dependency extraction and the ``>>`` / ``<<`` operators
    (single, list, XComArg, ``set_upstream`` / ``set_downstream``) — the
    machinery that builds DAG edges.
  - ``Task`` computed properties (timeout / orchestration / retry / wait).
  - ``Task``-level operators (operating on Task objects, not instances).
  - The decorator surface: the base ``@task`` guard, and that all service
    variants (sfn / lambda / glue / ecs / athena / emr / batch) build a DAG
    that generates valid ASL.

Invariant exercised throughout: when B depends on A, ``A`` ends up in
``B.dependencies``.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from polyris import DAG, task
from polyris.xcom import XComArg
from polyris.generators import generate_step_function_json, validate_asl

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _two_sfn_tasks(dag_id="wiring"):
    """Two sfn Task objects defined in a fresh DAG; returned uncalled."""
    with DAG(dag_id, schedule=None):
        @task.sfn(arn=ARN)
        def a():
            pass

        @task.sfn(arn=ARN)
        def b():
            pass
    return a, b


def _three_sfn_tasks(dag_id="wiring3"):
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
    return a, b, c


def _sfn_task(dag_id="props", **kw):
    """A single sfn Task carrying the given decorator kwargs."""
    with DAG(dag_id, schedule=None):
        @task.sfn(arn=ARN, **kw)
        def a():
            pass
    return a


# ============================================================ #
# TaskInstance dependency wiring
# ============================================================ #
class TestTaskInstanceWiring:
    def test_dependency_via_task_instance_arg(self):
        a, b = _two_sfn_tasks()
        ai = a()
        b(ai)  # pass the instance as an argument
        assert a in b.dependencies

    def test_dependency_via_xcomarg(self):
        a, b = _two_sfn_tasks()
        ai = a()
        b(ai.output)  # XComArg
        assert a in b.dependencies

    def test_dependency_via_list_of_instances(self):
        a, b, c = _three_sfn_tasks()
        c([a(), b()])
        assert a in c.dependencies
        assert b in c.dependencies

    def test_dependency_via_list_of_xcomargs(self):
        a, b, c = _three_sfn_tasks()
        c([a().output, b().output])
        assert a in c.dependencies
        assert b in c.dependencies

    def test_rshift_single(self):
        a, b = _two_sfn_tasks()
        a() >> b()
        assert a in b.dependencies

    def test_rshift_list_fans_out(self):
        a, b, c = _three_sfn_tasks()
        a() >> [b(), c()]
        assert a in b.dependencies
        assert a in c.dependencies

    def test_lshift_single(self):
        a, b = _two_sfn_tasks()
        b() << a()
        assert a in b.dependencies

    def test_lshift_list(self):
        a, b, c = _three_sfn_tasks()
        # c() << [a(), b()] → c becomes downstream of both a and b, i.e. a and b
        # end up in c.dependencies (mirrors [a, b] >> c).
        ci = c()
        ci << [a(), b()]
        assert a in c.dependencies
        assert b in c.dependencies

    def test_rrshift_list_fans_in(self):
        a, b, c = _three_sfn_tasks()
        [a(), b()] >> c()
        assert a in c.dependencies
        assert b in c.dependencies

    def test_set_downstream(self):
        a, b = _two_sfn_tasks()
        a().set_downstream(b())
        assert a in b.dependencies

    def test_set_upstream(self):
        a, b = _two_sfn_tasks()
        b().set_upstream(a())
        assert a in b.dependencies

    def test_wait_before_proxied_to_task(self):
        a, _ = _two_sfn_tasks()
        ai = a()
        ai.wait_before = 42
        assert ai.wait_before == 42
        assert a.wait_before == 42

    def test_output_is_xcomarg(self):
        a, _ = _two_sfn_tasks()
        ai = a()
        out = ai.output
        assert isinstance(out, XComArg)
        assert out.task_instance is ai


# ============================================================ #
# Task computed properties
# ============================================================ #
class TestTaskProperties:
    def test_timeout_from_execution_timeout(self):
        a = _sfn_task(execution_timeout=timedelta(minutes=5))
        assert a.timeout == 300

    def test_orchestration_timeout_defaults_to_timeout(self):
        a = _sfn_task(execution_timeout=timedelta(minutes=5))
        # No explicit orchestration_timeout → falls back to timeout.
        assert a.orchestration_timeout_seconds == 300

    def test_orchestration_timeout_explicit(self):
        a = _sfn_task(orchestration_timeout=timedelta(minutes=10))
        assert a.orchestration_timeout_seconds == 600

    def test_retry_delay_seconds(self):
        a = _sfn_task(retry_delay=timedelta(seconds=45))
        assert a.retry_delay_seconds == 45

    def test_wait_before_seconds_from_int(self):
        a = _sfn_task(wait_before=15)
        assert a.wait_before_seconds == 15

    def test_wait_before_seconds_from_timedelta(self):
        a = _sfn_task(wait_before=timedelta(seconds=90))
        assert a.wait_before_seconds == 90

    def test_node_id_is_task_id(self):
        a = _sfn_task()
        assert a.node_id == a.task_id


# ============================================================ #
# Task-level operators (operate on Task objects directly)
# ============================================================ #
class TestTaskOperators:
    def test_task_rshift_task(self):
        a, b = _two_sfn_tasks()
        a >> b
        assert a in b.dependencies

    def test_task_rshift_list(self):
        a, b, c = _three_sfn_tasks()
        a >> [b, c]
        assert a in b.dependencies
        assert a in c.dependencies

    def test_task_lshift_task(self):
        a, b = _two_sfn_tasks()
        b << a
        assert a in b.dependencies

    def test_list_rshift_task(self):
        a, b, c = _three_sfn_tasks()
        [a, b] >> c
        assert a in c.dependencies
        assert b in c.dependencies


# ============================================================ #
# Decorator surface
# ============================================================ #
class TestDecorators:
    def test_bare_task_decorator_raises(self):
        with pytest.raises(TypeError):
            task(lambda: None)

    def test_bare_task_with_arn_raises(self):
        with pytest.raises(TypeError):
            task(arn="arn:aws:states:::x")

    def test_all_service_variants_generate_valid_asl(self):
        # One DAG exercising every service decorator. The generated machine
        # must pass our ASL validator — this covers each decorator plus its
        # corresponding state generator.
        with DAG("alltypes", schedule=None) as dag:
            @task.sfn(arn=ARN)
            def s():
                pass

            @task.lambda_(function_name="my-fn")
            def lam():
                pass

            @task.glue(job_name="my-job")
            def g():
                pass

            @task.ecs(cluster="my-cluster", task_definition="my-td", subnets=["subnet-1"])
            def e():
                pass

            @task.athena(query_string="SELECT 1", database="db")
            def at():
                pass

            @task.emr(
                emr_cluster_id="j-123",
                emr_step={"Name": "step", "HadoopJarStep": {"Jar": "command-runner.jar"}},
            )
            def em():
                pass

            @task.batch(job_definition="jd", job_queue="jq")
            def ba():
                pass

            s() >> lam() >> g() >> e() >> at() >> em() >> ba()

        asl = json.loads(generate_step_function_json(dag))
        ok, errors, _ = validate_asl(asl)
        assert ok, errors
