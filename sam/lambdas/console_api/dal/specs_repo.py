"""
Data Access Layer: task specs (pipeline-specs, ADR #137 §8).

The one accessor of the specs in the console. Every reader of a pipeline's tasks
(their dependencies, outlets, inlets, task_config, skip_on_backfill) or of its
graph reads them here, never from the registry: the registry names a pipeline's
spec version (``spec_id``) and its ``task_count``, and the specs live in
``pipeline-specs``. ``tests/sdk/test_registry_readers.py`` fails for any code
that reads the registry's ``tasks`` or ``dag``.

Table schema (``task_spec.PK`` / ``task_spec.SK``, both strings):
    texts:    {pipeline}#text  / sha256 of the text  -> spec_text
    versions: {pipeline}#{fingerprint} (= spec_id) / task_id -> spec_hash

Every version is read with ``spec_store.read``, which checks the count, each
text's hash and the fingerprint. A version never changes, so a checked read is
kept for the life of the Lambda container.
"""

from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Tuple

from config import dynamodb_client, SPECS_TABLE
from logger import log
from polyris.coordination import spec_store, task_spec
from polyris.coordination.task_spec import TaskSpec

#: How many versions a container keeps; a version is a few KB per task.
CACHE_VERSIONS = 64


class SpecsMissing(LookupError):
    """The registry entry names no spec version: the pipeline was deployed before 1.3 and needs a redeploy."""


def needs_redeploy(registry_item: Mapping[str, Any]) -> bool:
    """The pipeline was deployed with an older format than the console reads, or with none."""
    return task_spec.needs_redeploy(registry_item.get(task_spec.REGISTRY_FORMAT_FIELD))


class SpecsRepo:
    """Repository for the pipeline-specs table."""

    def __init__(self):
        self._table_name = SPECS_TABLE
        self._versions: "OrderedDict[str, Dict[str, TaskSpec]]" = OrderedDict()

    def version(self, spec_id: str, task_count: int) -> Dict[str, TaskSpec]:
        """Every spec of a version, by task_id, checked; raises ``SpecError`` on a mismatch."""
        if spec_id in self._versions:
            self._versions.move_to_end(spec_id)
            return self._versions[spec_id]
        specs = spec_store.read(dynamodb_client, self._table_name, spec_id, expected_count=task_count)
        self._versions[spec_id] = specs
        while len(self._versions) > CACHE_VERSIONS:
            self._versions.popitem(last=False)
        return specs

    def for_pipeline(self, registry_item: Mapping[str, Any]) -> Dict[str, TaskSpec]:
        """The specs of the version a registry entry names; ``SpecsMissing`` when it names none."""
        spec_id, task_count = _named_version(registry_item)
        return self.version(spec_id, task_count)

    def deployed_tasks(self, registry_item: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """``tasks_of`` for a reader over every pipeline: a pipeline deployed before 1.3 has none.

        It is logged, and the pipelines list marks it "redeploy needed". Use ``tasks_of``
        where a missing version must refuse the request, e.g. a backfill of that pipeline,
        which would otherwise run the tasks its user excluded with ``skip_on_backfill``.
        A version that cannot be read raises either way.
        """
        try:
            return self.tasks_of(registry_item)
        except SpecsMissing as e:
            log.warn("specs_repo", "Pipeline names no task specs; it counts as having no tasks",
                     pipeline_name=registry_item.get('pipeline_name', ''), error=str(e))
            return []

    def tasks_of(self, registry_item: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Each task's spec fields (``TaskSpecKey`` values as keys), in task_id order."""
        specs = self.for_pipeline(registry_item)
        return [dict(specs[task_id].fields) for task_id in sorted(specs)]

    def task_of(self, registry_item: Mapping[str, Any], task_id: str) -> Optional[Dict[str, Any]]:
        """One task's spec fields, or None when the pipeline's version has no such task."""
        spec = self.for_pipeline(registry_item).get(task_id)
        return dict(spec.fields) if spec is not None else None


def _named_version(registry_item: Mapping[str, Any]) -> Tuple[str, int]:
    spec_id = registry_item.get('spec_id')
    name = registry_item.get('pipeline_name', '?')
    if not spec_id:
        raise SpecsMissing(f"pipeline {name} names no task specs: it was deployed before 1.3; redeploy it")
    try:
        task_count = int(registry_item.get('task_count'))
    except (TypeError, ValueError) as e:
        raise task_spec.SpecError(f"pipeline {name} names spec version {spec_id} without a task count: {e}") from e
    return str(spec_id), task_count


specs_repo = SpecsRepo()
