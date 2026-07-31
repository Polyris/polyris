"""init_pipeline must actually scaffold something for deploy_method='cfn'.

Regression test for a real bug found in a code-review pass: `init_pipeline`
had only an `if deploy_method == "local":` branch — no `else`. Since "cfn" is
the function's own default parameter value, AND the CLI's own --help/epilog
documents plain `polyris-init my-pipeline` (no flags) as "Create a pipeline
(default)", the tool's most basic, documented, default invocation silently
created an empty directory: zero files written, nothing printed, no error.
"""
from polyris.init import init_pipeline
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
