"""init_pipeline must actually scaffold something for deploy_method='cfn'.

Regression test for a real bug found in a code-review pass: `init_pipeline`
had only an `if deploy_method == "local":` branch — no `else`. Since "cfn" is
the function's own default parameter value, AND the CLI's own --help/epilog
documents plain `polyris-init my-pipeline` (no flags) as "Create a pipeline
(default)", the tool's most basic, documented, default invocation silently
created an empty directory: zero files written, nothing printed, no error.
"""
from polyris.init import init_pipeline
from polyris.generators import _build_task_config_and_arn
from polyris.constants import TaskConfigKey
from polyris.validation import validate_asl_from_dag


def _load_dag(dag_py_path):
    """Exec the generated dag.py file and return its `dag` object, the same
    way `_load_dag_from_file` (deploy.py) and real usage do."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("generated_dag", dag_py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.dag


class TestInitPipelineCfnDefault:
    def test_default_deploy_method_writes_a_file(self, tmp_path):
        init_pipeline(name="my-pipeline", base_dir=str(tmp_path))
        pipeline_dir = tmp_path / "my-pipeline"
        assert pipeline_dir.is_dir()
        assert (pipeline_dir / "dag.py").exists(), (
            "default (cfn) deploy_method must write dag.py — it previously "
            "wrote nothing at all"
        )

    def test_explicit_cfn_deploy_method_writes_a_file(self, tmp_path):
        init_pipeline(name="my-pipeline", base_dir=str(tmp_path), deploy_method="cfn")
        assert (tmp_path / "my-pipeline" / "dag.py").exists()

    def test_generated_cfn_pipeline_is_actually_valid(self, tmp_path):
        """The scaffolded dag.py must be a real, deployable pipeline — not
        just any non-empty file."""
        init_pipeline(name="my-pipeline", base_dir=str(tmp_path))
        dag = _load_dag(str(tmp_path / "my-pipeline" / "dag.py"))
        is_valid, errors, _warnings = validate_asl_from_dag(dag, verbose=False)
        assert is_valid, errors
        assert dag.dag_id == "my-pipeline"

    def test_generated_cfn_pipeline_has_no_leftover_local_wording(self, tmp_path):
        """The cfn-mode scaffold must use its own header, not the --local
        template's — proves the fix wired up CFN_PIPELINE, not a copy of
        LOCAL_PIPELINE with the deploy_method check dropped."""
        init_pipeline(name="my-pipeline", base_dir=str(tmp_path))
        content = (tmp_path / "my-pipeline" / "dag.py").read_text()
        assert "CloudFormation-ready" in content
        assert "polyris init --local" not in content
        assert "polyris-deploy" in content

    def test_local_deploy_method_unaffected(self, tmp_path):
        """Control: the pre-existing --local path must still work exactly
        as before, and must be distinguishable from the cfn template — this
        fix must not have swapped or merged the two."""
        init_pipeline(name="my-pipeline", base_dir=str(tmp_path), deploy_method="local")
        content = (tmp_path / "my-pipeline" / "dag.py").read_text()
        assert "polyris init --local" in content
        assert "CloudFormation-ready" not in content
        dag = _load_dag(str(tmp_path / "my-pipeline" / "dag.py"))
        is_valid, errors, _warnings = validate_asl_from_dag(dag, verbose=False)
        assert is_valid, errors


class TestStarterPipelineRetryDefaults:
    """Starter pipeline sets default_args={"retries": 0} — the documented
    system default: pause on first failure, no automatic retries.  Both
    templates (cfn and local) must apply the same policy, and an explicit
    retries=N on any task must override the DAG-level default without
    disturbing other tasks."""

    def test_cfn_template_tasks_inherit_zero_retries(self, tmp_path):
        """Each task in the cfn scaffold inherits retries=0 from default_args:
        pause on first failure, 1 total attempt."""
        init_pipeline(name="retry-test", base_dir=str(tmp_path))
        dag = _load_dag(str(tmp_path / "retry-test" / "dag.py"))
        assert dag.default_args.get("retries") == 0, (
            "starter pipeline must declare default_args={'retries': 0}"
        )
        for t in dag.tasks:
            assert t.retries == 0, (
                f"task {t.task_id!r} must inherit retries=0 from default_args "
                f"(pause on first failure); got retries={t.retries}"
            )

    def test_local_template_tasks_inherit_zero_retries(self, tmp_path):
        """Same policy for the --local scaffold."""
        init_pipeline(name="retry-local", base_dir=str(tmp_path), deploy_method="local")
        dag = _load_dag(str(tmp_path / "retry-local" / "dag.py"))
        assert dag.default_args.get("retries") == 0
        for t in dag.tasks:
            assert t.retries == 0

    def test_zero_retries_produces_no_retry_keys_in_task_config(self, tmp_path):
        """With retries=0 the wrapper adds no retry keys to task_config;
        Check_Should_Retry defaults to 0 and never fires.

        Note: the starter pipeline uses @task.sfn, which always starts with an
        empty task_config.  The assertion is meaningful because _add_retry_config
        exits early on retries=0 — if it were broken and inserted the key, the
        assert would catch it even for sfn tasks."""
        init_pipeline(name="retry-cfg", base_dir=str(tmp_path))
        dag = _load_dag(str(tmp_path / "retry-cfg" / "dag.py"))
        for t in dag.tasks:
            tc, _ = _build_task_config_and_arn(t)
            assert TaskConfigKey.RETRIES not in tc, (
                f"task_config for {t.task_id!r} must have no retries key "
                f"when retries=0 (wrapper defaults to 0, never retries)"
            )

    def test_explicit_retries_overrides_zero_dag_default(self):
        """retries=N on a task must override default_args={'retries': 0}:
        the task gets retries=N in task_config → wrapper retries N times
        (N+1 total attempts).  Other tasks keep retries=0."""
        from polyris import DAG, task

        with DAG("override-test", schedule=None, default_args={"retries": 0}):
            @task.sfn(
                arn="arn:aws:states:us-east-1:123456789012:stateMachine:with-retry",
                retries=2,
            )
            def with_retry():
                pass

            @task.sfn(
                arn="arn:aws:states:us-east-1:123456789012:stateMachine:no-retry",
            )
            def no_retry():
                pass

            with_retry() >> no_retry()

        assert with_retry.retries == 2, (
            "explicit retries=2 must override default_args={'retries': 0}"
        )
        assert no_retry.retries == 0, (
            "task without explicit retries must keep default retries=0"
        )

        tc_with, _ = _build_task_config_and_arn(with_retry)
        tc_no, _ = _build_task_config_and_arn(no_retry)

        assert tc_with.get(TaskConfigKey.RETRIES) == 2, (
            "task with retries=2 must carry retries=2 in task_config — "
            "3 total attempts (1 initial + 2 retries)"
        )
        assert TaskConfigKey.RETRIES not in tc_no, (
            "task with retries=0 must have no retries key in task_config — "
            "1 total attempt (pause on first failure)"
        )
