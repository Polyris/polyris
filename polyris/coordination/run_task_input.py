"""``run_task``'s input, which the dispatcher builds in Python (ADR #137 §1, §9).

The dispatcher builds it when it starts a task, from the task's row, its spec
(one ``GetItem`` on ``pipeline-specs``), the run's input item and the statuses
of the task's direct upstreams as its pass read them. ``run_task`` reads only
the keys of ``Key``: today it reads 39 keys of the wrapper's state, and the
rewrite of ``run_task`` (TR-5) adds the parity test with its template. The
lifetimes come in the input, so the template keeps no copy of them.

An input over Step Functions' 256 KB refuses the start for good: the row fails
with the cause instead of starting (§3). A pass that did not read every
upstream starts nothing (invariant 10).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from ..constants import TaskSpecKey
from . import retention
from .task_spec import TaskSpec, canonical_json

#: Step Functions' quota for an execution's input, as UTF-8 JSON.
PAYLOAD_LIMIT = 262_144


class Key:
    """The keys of ``run_task``'s input."""
    EXECUTION_NAME = "execution_name"            # the task row's key
    GENERATION = "generation"                    # run_task writes only under it (invariant 5)
    TASK_NAME = "task_name"
    PIPELINE_NAME = "pipeline_name"
    PIPELINE_EXECUTION = "pipeline_execution"
    DATE = "date"
    TRIGGERED_BY = "triggered_by"
    BACKFILL_ID = "backfill_id"
    TASK_TYPE = "task_type"
    TASK_ARN = "task_arn"
    TASK_CONFIG = "task_config"
    CROSS_ACCOUNT_ROLE = "cross_account_role"
    EXECUTION_TIMEOUT = "execution_timeout"      # seconds per attempt; absent: no limit
    OUTLETS = "outlets"
    VARIABLES = "variables"
    CASCADE = "cascade"                          # a BackfillCascade value
    UPSTREAMS = "upstreams"                      # {name: {"status": ..., "skip_origin": ...}}
    OUTPUT_TTL = "output_ttl_seconds"
    INPUT_TTL = "input_ttl_seconds"
    HISTORY_TTL = "history_ttl_seconds"


KEYS = frozenset(v for k, v in vars(Key).items() if not k.startswith("_"))


class InputTooLarge(ValueError):
    """``run_task``'s input passes Step Functions' 256 KB: the start is refused for good."""


def build(*, execution_name: str, generation: int, task_name: str, pipeline_name: str,
          pipeline_execution: str, date: str, triggered_by: str, backfill_id: Optional[str],
          spec: TaskSpec, variables: Mapping[str, Any], cascade: str,
          upstreams: Mapping[str, Tuple[str, Optional[str]]]) -> Dict[str, Any]:
    """The input of the task's ``run_task``; ``upstreams`` maps each direct upstream to its status and skip origin."""
    dependencies = spec.fields[TaskSpecKey.DEPENDENCIES]
    if set(upstreams) != set(dependencies):
        raise ValueError(f"{task_name}: the upstreams read {sorted(upstreams)} are not its dependencies "
                         f"{sorted(dependencies)}")
    if spec.task_id != task_name:
        raise ValueError(f"the spec of {spec.task_id!r} cannot start {task_name!r}")
    fields = spec.fields
    payload: Dict[str, Any] = {
        Key.EXECUTION_NAME: execution_name,
        Key.GENERATION: generation,
        Key.TASK_NAME: task_name,
        Key.PIPELINE_NAME: pipeline_name,
        Key.PIPELINE_EXECUTION: pipeline_execution,
        Key.DATE: date,
        Key.TRIGGERED_BY: triggered_by,
        Key.TASK_TYPE: fields[TaskSpecKey.TASK_TYPE],
        Key.TASK_CONFIG: fields.get(TaskSpecKey.TASK_CONFIG, {}),
        Key.OUTLETS: fields.get(TaskSpecKey.OUTLETS, []),
        Key.VARIABLES: dict(variables),
        Key.CASCADE: cascade,
        Key.UPSTREAMS: upstream_statuses(upstreams),
        Key.OUTPUT_TTL: int(retention.OUTPUT.total_seconds()),
        Key.INPUT_TTL: int(retention.INPUT.total_seconds()),
        Key.HISTORY_TTL: int(retention.HISTORY.total_seconds()),
    }
    for key, spec_key in ((Key.TASK_ARN, TaskSpecKey.TASK_ARN), (Key.CROSS_ACCOUNT_ROLE, TaskSpecKey.CROSS_ACCOUNT_ROLE),
                          (Key.EXECUTION_TIMEOUT, TaskSpecKey.EXECUTION_TIMEOUT)):
        if spec_key in fields:
            payload[key] = fields[spec_key]
    if backfill_id is not None:
        payload[Key.BACKFILL_ID] = backfill_id
    size = len(canonical_json(payload).encode("utf-8"))
    if size > PAYLOAD_LIMIT:
        raise InputTooLarge(f"{task_name}: run_task's input takes {size} bytes, over Step Functions' {PAYLOAD_LIMIT}")
    return payload


def upstream_statuses(upstreams: Mapping[str, Tuple[str, Optional[str]]]) -> Dict[str, Dict[str, Optional[str]]]:
    """Each direct upstream's status and skip origin, as ``run_task`` gives them to the task (§9)."""
    return {name: {"status": status, "skip_origin": origin} for name, (status, origin) in sorted(upstreams.items())}


def upstream_statuses_env(upstreams: Mapping[str, Tuple[str, Optional[str]]]) -> str:
    """The value of ``POLYRIS_UPSTREAM_STATUSES`` (``xcom.ENV_UPSTREAM_STATUSES``) for a Glue, ECS or Batch task."""
    return canonical_json(upstream_statuses(upstreams))
