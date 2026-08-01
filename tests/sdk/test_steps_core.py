"""Step-class tests — operators, Choice/Condition, control-flow steps.

Covers the remaining pure-logic in ``polyris.steps`` (CLAUDE.md #13):

  - The base ``Step`` ``>>`` / ``<<`` operators across Step / Task /
    TaskInstance / TaskGroup / list targets (dependency wiring).
  - ``Choice`` construction and the ``Condition`` JSONata builders.
  - ``Map`` / ``Sensor`` / ``ShortCircuit`` construction and step-id defaults.
"""
from __future__ import annotations

from polyris import DAG, task
from polyris.task_group import TaskGroup
from polyris.steps import (
    Wait,
    Pass,
    Choice,
    Condition,
    Map,
    Sensor,
    ShortCircuit,
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
# Choice + Condition
# ============================================================ #
class TestChoiceAndCondition:
    def test_choice_default_step_id(self):
        c = Choice(choices=[(Condition.number_greater_than("$.n", 5), Pass())], default=Pass())
        assert c.step_id == "Choice"
        assert c.step_type == "choice"

    def test_choice_explicit_step_id(self):
        c = Choice(step_id="route", default=Pass())
        assert c.step_id == "route"

    def test_condition_string_equals(self):
        assert Condition.string_equals("$.day", "weekend") == '{% $.day = "weekend" %}'

    def test_condition_string_matches(self):
        assert Condition.string_matches("$.name", "ab.*") == "{% $match($.name, /ab.*/) %}"

    def test_condition_number_equals(self):
        assert Condition.number_equals("$.n", 5) == "{% $.n = 5 %}"

    def test_condition_number_greater_than(self):
        assert Condition.number_greater_than("$.n", 10) == "{% $.n > 10 %}"

    def test_condition_number_less_than(self):
        assert Condition.number_less_than("$.n", 3) == "{% $.n < 3 %}"

    def test_condition_boolean_equals(self):
        assert Condition.boolean_equals("$.flag", True) == "{% $.flag = true %}"
        assert Condition.boolean_equals("$.flag", False) == "{% $.flag = false %}"

    def test_condition_is_present(self):
        assert Condition.is_present("$.x") == "{% $exists($.x) %}"

    def test_condition_is_null(self):
        assert Condition.is_null("$.x") == "{% $.x = null %}"

    def test_condition_jsonata(self):
        assert Condition.jsonata("$.a + $.b") == "{% $.a + $.b %}"


# ============================================================ #
# Map / Sensor / ShortCircuit
# ============================================================ #
class TestControlFlowSteps:
    def test_map_defaults(self):
        m = Map(items_path="$.rows")
        assert m.step_id == "Map"
        assert m.step_type == "map"
        assert m.items_path == "$.rows"

    def test_sensor_step_id_includes_type(self):
        s = Sensor(sensor_type="s3", bucket="b", key="k")
        assert s.step_id == "Sensor_s3"
        assert s.step_type == "sensor"

    def test_short_circuit_defaults(self):
        sc = ShortCircuit(condition="{% $count($.records) > 0 %}")
        assert sc.step_id == "ShortCircuit"
        assert sc.step_type == "short_circuit"
        assert sc.skip_downstream is True
