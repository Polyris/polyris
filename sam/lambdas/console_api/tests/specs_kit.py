"""Task specs for console tests: write a pipeline's spec version as polyris-deploy does (ADR #137 §8).

The console reads a pipeline's tasks only through ``dal.specs_repo``, from the
version the registry entry names; these helpers write that version to a moto
``pipeline-specs`` table with the real ``spec_store.write`` and return the
registry fields that name it.
"""
import importlib
import importlib.util
from pathlib import Path
from typing import Any, Dict, Iterable

from polyris.coordination import spec_store, task_spec

SPECS_TABLE = "test-pipeline-specs"


def spec(task_id: str, **fields: Any) -> Dict[str, Any]:
    """A task's spec with the keys every spec has, and ``fields`` (TaskSpecKey values) besides."""
    return {"task_id": task_id, "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success", **fields}


def use_specs_table(mocker, resource) -> None:
    """Create ``pipeline-specs`` as the template declares it and point ``specs_repo`` at it, with an empty cache."""
    kit_path = Path(__file__).resolve().parents[2] / "coordination" / "tests" / "coordination_testkit.py"
    loader_spec = importlib.util.spec_from_file_location("coordination_testkit", kit_path)
    assert loader_spec and loader_spec.loader
    kit = importlib.util.module_from_spec(loader_spec)
    loader_spec.loader.exec_module(kit)
    kit.create_tables(resource, {"PipelineSpecsTable": SPECS_TABLE})
    import boto3
    import config
    mocker.patch.object(config, "_dynamodb_client", boto3.client("dynamodb", region_name="us-east-1"))
    repo_module = importlib.import_module("dal.specs_repo")
    mocker.patch.object(repo_module.specs_repo, "_table_name", SPECS_TABLE)
    mocker.patch.object(repo_module.specs_repo, "_versions", type(repo_module.specs_repo._versions)())


def deploy_specs(resource, pipeline: str, specs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Write ``specs`` as ``pipeline``'s version; the registry fields that name it."""
    version = task_spec.build_version(pipeline, list(specs))
    import config
    spec_store.write(config.dynamodb_client, SPECS_TABLE, version, sleep=lambda _: None)
    return {"spec_id": version.spec_id, "task_count": version.task_count,
            task_spec.REGISTRY_FORMAT_FIELD: task_spec.FORMAT_VERSION}
