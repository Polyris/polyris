"""The run's input item (ADR #137 §3, D96).

``Start_Run`` writes it once, under ``run_row.input_key``: the run's variables,
its skip list and the cascade of its facts (``records.cascade_of`` of the
starter's flags). The dispatcher reads it to build ``run_task``'s input, Mark
Successful for the cascade, and the console for what it shows. The variables
and the skip list are canonical JSON text, as 1.2 stores its starter input: as
attributes, boto3's resource would give numbers back as ``Decimal``, and
DynamoDB keeps ``1.0`` as ``1``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple

from ..constants import BackfillCascade
from . import run_row
from .task_spec import canonical_json

VARIABLES = "variables"
SKIP_TASKS = "skip_tasks"
CASCADE = "cascade"
#: The run's partition key as 1.2 gave it to each task (``PARTITION_ARG``): its variables', its input's, or its date.
PARTITION_ARG = "partition_arg"
_CASCADES = frozenset(v for k, v in vars(BackfillCascade).items() if not k.startswith("_"))


@dataclass(frozen=True)
class RunInput:
    variables: Mapping[str, Any] = field(default_factory=dict)
    skip_tasks: Tuple[str, ...] = ()
    cascade: str = BackfillCascade.AUTO
    partition_arg: str = ""            # empty in an item written before it was kept: the run's date stands in


def to_item(run_input: RunInput, *, pipeline_execution: str, ttl: int) -> Dict[str, Any]:
    """The item ``Start_Run`` writes."""
    return {run_row.TABLE_KEY: run_row.input_key(pipeline_execution),
            VARIABLES: canonical_json(dict(run_input.variables)),
            SKIP_TASKS: canonical_json(list(run_input.skip_tasks)),
            CASCADE: run_input.cascade,
            PARTITION_ARG: run_input.partition_arg,
            run_row.TTL: ttl}


def from_item(item: Mapping[str, Any]) -> RunInput:
    """The run's input from its item as DynamoDB gives it back; a malformed item is refused."""
    variables = _json(item, VARIABLES)
    skip_tasks = _json(item, SKIP_TASKS)
    if not isinstance(variables, dict):
        raise ValueError(f"a run's variables must be a JSON object, got {type(variables).__name__}")
    if not isinstance(skip_tasks, list) or not all(isinstance(t, str) for t in skip_tasks):
        raise ValueError("a run's skip_tasks must be a JSON list of task names")
    cascade = item.get(CASCADE)
    if cascade not in _CASCADES:
        raise ValueError(f"a run's cascade must be one of {sorted(_CASCADES)}, got {cascade!r}")
    partition_arg = item.get(PARTITION_ARG, "")
    if not isinstance(partition_arg, str):
        raise ValueError(f"a run's partition_arg must be text, got {type(partition_arg).__name__}")
    return RunInput(variables, tuple(skip_tasks), str(cascade), partition_arg)


def _json(item: Mapping[str, Any], name: str) -> Any:
    text = item.get(name)
    if not isinstance(text, str):
        raise ValueError(f"a run's {name} must be JSON text, got {type(text).__name__}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"a run's {name} is not JSON: {exc}") from exc
