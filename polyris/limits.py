"""The limits of a pipeline (ADR #137 §8), which ``polyris-deploy`` and ``polyris-validate`` check.

The limits are ours, not those of a Step Functions state: each refusal names
the limit and the size it measured. Everything is measured from what is sent:
a task's spec as ``polyris-deploy`` writes it to ``pipeline-specs``, and the
definition as ``polyris-deploy`` sends it (``generate_step_function_json``,
indented). A duplicate outlet is refused earlier, by the task decorator.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .constants import SkipOrigin, TaskConfigKey, TaskSpecKey, TaskStatus
from .coordination import deadlines, run_row, run_task_input, task_row, task_spec

#: Step Functions' limit on a definition, with a warning before it.
DEFINITION_MAX = 1_000_000
DEFINITION_WARN = 900_000

#: ECS's limit on a RunTask's overrides, as the characters of their JSON.
ECS_OVERRIDES_MAX = 8192
#: The longest values a run gives the fields of run_task's input that the spec does not: a partition key (an
#: hour), a run's execution name, a backfill id and a generation.
_LONGEST_DATE = "2026-11-21T23"
_LONGEST_EXECUTION = "x" * 80
_LONGEST_BACKFILL_ID = "x" * 64
_LARGEST_GENERATION = 10 ** 9
#: What run_task adds to each container override of an ECS task (its template's Run_Task_ECS), with the most
#: characters each value can take; None: the value is known here. A parity test compares the names with the
#: template.
PLATFORM_ENV: Mapping[str, Optional[int]] = {
    "POLYRIS_PIPELINE_NAME": None,
    "POLYRIS_RUN_DATE": len(_LONGEST_DATE),
    "POLYRIS_TOKENS_TABLE": 255,                        # a DynamoDB table name
    "POLYRIS_TASK_NAME": None,
    "POLYRIS_WRAPPER_RUN_ID": 256,                      # run_task's execution ARN
    "POLYRIS_UPSTREAM_STATUSES": None,
}
#: What an upstream may be when its dependant starts: a final status, a skip with its origin.
_START_OUTCOMES: Tuple[Tuple[str, Optional[str]], ...] = (
    *((s.value, None) for s in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED,
                                TaskStatus.STOPPED)),
    *((TaskStatus.SKIPPED.value, o) for o in (SkipOrigin.MANUAL, SkipOrigin.RUN, SkipOrigin.RULE)),
)


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
    """The tasks per pipeline, the specs' bytes together, each task_id's bytes, each task's outlets and each
    spec's bytes."""
    errors: List[str] = []
    if version.task_count > task_spec.MAX_TASKS:
        errors.append(f"Pipeline '{version.pipeline}' has {version.task_count} tasks, over the limit of "
                      f"{task_spec.MAX_TASKS} tasks per pipeline")
    total = sum(len(text.encode("utf-8")) for text in version.texts.values())
    if total > task_spec.MAX_VERSION_BYTES:
        errors.append(f"Pipeline '{version.pipeline}': its task specs take {total:,} bytes together, over the limit "
                      f"of {task_spec.MAX_VERSION_BYTES:,} bytes (16 MB) that Start_Run reads at a run's start")
    for task_id, text in sorted(version.texts.items()):
        if task_id.startswith("_"):
            errors.append(f"Task '{task_id}': a task_id may not start with '_', which the platform's own rows use "
                          f"(a run row starts with '_run_')")
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
            continue
        errors += _start_errors(version.pipeline, task_id, text)
    return errors


def worst_upstreams(dependencies: List[str]) -> Dict[str, Tuple[str, Optional[str]]]:
    """Each dependency at the status and skip origin that take the most bytes in the statuses run_task passes
    on (``run_task_input.upstream_statuses``)."""
    worst = max(_START_OUTCOMES, key=lambda outcome: len(run_task_input.upstream_statuses_env({"u": outcome})))
    return {name: worst for name in dependencies}


def _start_errors(pipeline: str, task_id: str, text: str) -> List[str]:
    """D118: run_task's input with the statuses of every upstream against Step Functions' 256 KB, and an ECS
    task's overrides with the environment run_task adds against ECS's 8,192 characters. A wide fan-in passes
    one status per upstream; the run's variables, which the deploy does not know, are left to the start."""
    spec = task_spec.parse(text, expected_hash=task_spec.text_hash(text))
    dependencies = list(spec.fields.get(TaskSpecKey.DEPENDENCIES, []))
    upstreams = worst_upstreams(dependencies)
    errors: List[str] = []
    try:
        run_task_input.build(
            execution_name=f"{task_id}-{_LONGEST_DATE}-{'x' * task_row.SHORT_LENGTH}",
            generation=_LARGEST_GENERATION, task_name=task_id, pipeline_name=pipeline,
            pipeline_execution=_LONGEST_EXECUTION, date=_LONGEST_DATE, partition_arg=_LONGEST_DATE,
            backfill_id=_LONGEST_BACKFILL_ID, spec=spec, variables={}, cascade="auto", upstreams=upstreams)
    except run_task_input.InputTooLarge as exc:
        errors.append(f"Task '{task_id}': its run_task input takes {exc.size:,} bytes with the statuses of its "
                      f"{len(dependencies)} upstreams, over the limit of {run_task_input.PAYLOAD_LIMIT:,} bytes of a "
                      f"task's input; split the fan-in")
    if spec.fields[TaskSpecKey.TASK_TYPE] == "ecs":
        size = len(json.dumps(ecs_overrides(spec.fields, pipeline, task_id, upstreams), separators=(",", ":"),
                              ensure_ascii=False))
        if size > ECS_OVERRIDES_MAX:
            errors.append(f"Task '{task_id}': its container overrides take {size:,} characters with the environment "
                          f"run_task adds, the statuses of its {len(dependencies)} upstreams included, over ECS's "
                          f"{ECS_OVERRIDES_MAX:,}")
    return errors


def ecs_overrides(fields: Mapping[str, Any], pipeline: str, task_id: str,
                  upstreams: Mapping[str, Tuple[str, Optional[str]]]) -> Dict[str, Any]:
    """The overrides run_task sends an ECS task, at the longest values a run can give them: each container
    override gets the platform's environment, as Run_Task_ECS builds it."""
    overrides = dict((fields.get(TaskSpecKey.TASK_CONFIG) or {}).get(TaskConfigKey.OVERRIDES) or {})
    containers = overrides.get("ContainerOverrides")
    if not isinstance(containers, list):
        return overrides
    known = {"POLYRIS_PIPELINE_NAME": pipeline, "POLYRIS_TASK_NAME": task_id,
             "POLYRIS_UPSTREAM_STATUSES": run_task_input.upstream_statuses_env(upstreams)}
    env = [{"Name": name, "Value": known[name] if longest is None else "x" * longest}
           for name, longest in PLATFORM_ENV.items()]
    overrides["ContainerOverrides"] = [{**c, "Environment": list(c.get("Environment") or []) + env}
                                       for c in containers]
    return overrides


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
