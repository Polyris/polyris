"""@dag decorator tests — DAG construction via decorator.

Covers ``polyris.helpers.dag`` (CLAUDE.md #13): default vs explicit ``dag_id``,
schedule/description passthrough, and that the wrapped function body runs inside
the DAG context so its tasks register.
"""
from __future__ import annotations

import warnings

from polyris import task
from polyris.helpers import dag

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


class TestDagDecorator:
    def test_default_dag_id_is_function_name(self):
        @dag()
        def my_pipeline():
            @task.sfn(arn=ARN)
            def step():
                pass
            step()

        built = my_pipeline()
        assert built.dag_id == "my_pipeline"
        assert len(built.tasks) == 1

    def test_explicit_dag_id_and_schedule(self):
        @dag(dag_id="custom_id", schedule="rate(1 hour)", description="nightly job")
        def pipe():
            @task.sfn(arn=ARN)
            def a():
                pass
            a()

        built = pipe()
        assert built.dag_id == "custom_id"
        assert built.schedule == "rate(1 hour)"
        assert built.description == "nightly job"

    def test_description_falls_back_to_docstring(self):
        @dag()
        def documented():
            """Doc-as-description."""
            @task.sfn(arn=ARN)
            def a():
                pass
            a()

        assert documented().description == "Doc-as-description."

    def test_body_runs_inside_context_and_wires_deps(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            @dag(dag_id="wired")
            def flow():
                @task.sfn(arn=ARN)
                def extract():
                    pass

                @task.sfn(arn=ARN)
                def load():
                    pass

                extract() >> load()

            built = flow()
        ids = {t.task_id for t in built.tasks}
        assert ids == {"extract", "load"}
        load_task = built.get_task("load")
        assert any(d.task_id == "extract" for d in load_task.dependencies)
