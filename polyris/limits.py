"""The limits of a pipeline (ADR #137 §8), which ``polyris-deploy`` and ``polyris-validate`` check.

The limits are ours, not those of a Step Functions state: each refusal names
the limit and the size it measured. Everything is measured from what is sent:
a task's spec as ``polyris-deploy`` writes it to ``pipeline-specs``, and the
definition as ``polyris-deploy`` sends it (``generate_step_function_json``,
indented). A duplicate outlet is refused earlier, by the task decorator.
"""

from __future__ import annotations

import json
from typing import List

from .constants import TaskSpecKey
from .coordination import deadlines, run_row, run_task_input, task_spec

#: Step Functions' limit on a definition, with a warning before it.
DEFINITION_MAX = 1_000_000
DEFINITION_WARN = 900_000


def definition_errors(definition: str) -> List[str]:
    """The definition against Step Functions' 1 MB, measured as UTF-8 bytes of the text sent."""
    size = len(definition.encode("utf-8"))
    if size >= DEFINITION_MAX:
        return [f"Definition size {size:,} bytes exceeds the AWS 1 MB limit ({DEFINITION_MAX:,} bytes). "
                f"Reduce the number of tasks or schema column count."]
    return []


def definition_warnings(definition: str) -> List[str]:
    size = len(definition.encode("utf-8"))
    if DEFINITION_WARN <= size < DEFINITION_MAX:
        return [f"Definition size {size:,} bytes is approaching the AWS 1 MB limit ({DEFINITION_MAX:,} bytes)."]
    return []


def spec_errors(version: task_spec.Version) -> List[str]:
    """The tasks per pipeline, each task_id's bytes, each task's outlets and each spec's bytes."""
    errors: List[str] = []
    if version.task_count > task_spec.MAX_TASKS:
        errors.append(f"Pipeline '{version.pipeline}' has {version.task_count} tasks, over the limit of "
                      f"{task_spec.MAX_TASKS} tasks per pipeline")
    for task_id, text in sorted(version.texts.items()):
        id_bytes = len(task_id.encode("utf-8"))
        if id_bytes > run_row.TASK_ID_MAX:
            errors.append(f"Task '{task_id}': its task_id takes {id_bytes} bytes, over the limit of "
                          f"{run_row.TASK_ID_MAX} bytes")
        outlets = json.loads(text).get(TaskSpecKey.OUTLETS.value, [])
        if len(outlets) > task_spec.MAX_OUTLETS:
            errors.append(f"Task '{task_id}' has {len(outlets)} outlets, over the limit of "
                          f"{task_spec.MAX_OUTLETS} outlets per task")
        text_bytes = len(text.encode("utf-8"))
        if text_bytes > run_task_input.PAYLOAD_LIMIT:
            errors.append(f"Task '{task_id}': its spec takes {text_bytes:,} bytes, over the limit of "
                          f"{run_task_input.PAYLOAD_LIMIT:,} bytes of a task's input")
    return errors


def spec_warnings(version: task_spec.Version) -> List[str]:
    """A task with ``wait_for`` whose ``orchestration_timeout`` ends its wait before the 7 days of a wait_for."""
    limit = int(deadlines.WAIT_FOR_LIMIT.total_seconds())
    warnings: List[str] = []
    for task_id, text in sorted(version.texts.items()):
        spec = json.loads(text)
        cap = spec.get(TaskSpecKey.ORCHESTRATION_TIMEOUT.value)
        if spec.get(TaskSpecKey.WAIT_FOR.value) and cap is not None and cap < limit:
            warnings.append(f"Task '{task_id}': orchestration_timeout of {cap} seconds ends its wait_for wait "
                            f"before the {limit} seconds (7 days) a wait_for waits for its assets")
    return warnings
