"""A task's spec as ``pipeline-specs`` stores it, and the version it belongs to (ADR #137 §8).

A spec is stored as one string, its canonical JSON, and every reader checks the
text's sha256. DynamoDB stores the number ``1.0`` as ``1`` and keeps no order
in a map, so a hash of attributes read back would not match the deploy's; a
string keeps every byte. A version's fingerprint is the sha256 over its
``(task_id, text hash)`` pairs in key order, and names the version:
``spec_id = {pipeline}#{fingerprint}``. ``polyris-deploy``, ``Start_Run``, the
dispatcher and the console all call these functions, so no parity test is needed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Tuple

from . import run_row

#: The format of what ``Start_Run`` receives and reads: the specs and its arguments. ``polyris-deploy``
#: writes it to the registry and into ``Start_Run``'s arguments; ``Start_Run`` refuses a format it does
#: not support. Golden spec files show a change of the specs' shape, which needs a new format.
FORMAT_VERSION = 1
#: The registry field that names the format a pipeline was deployed with; a pipeline deployed before 1.3
#: has none. The run row stamps the same field.
REGISTRY_FORMAT_FIELD = run_row.FORMAT


def canonical_json(value: Any) -> str:
    """The one text of ``value``: sorted keys, no spaces, characters as they are, no NaN or infinity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def text_hash(text: str) -> str:
    """The sha256 of a spec's text, which names the text in ``pipeline-specs``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(pairs: Iterable[Tuple[str, str]]) -> str:
    """The sha256 over a version's ``(task_id, text hash)`` pairs in key order."""
    ordered = sorted(pairs)
    task_ids = [task_id for task_id, _ in ordered]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError(f"a spec version names a task twice: {sorted({t for t in task_ids if task_ids.count(t) > 1})}")
    return text_hash(canonical_json([list(pair) for pair in ordered]))


def spec_id(pipeline: str, version_fingerprint: str) -> str:
    """The partition of a spec version's items, which the run row and the registry name."""
    return f"{pipeline}#{version_fingerprint}"


def text_partition(pipeline: str) -> str:
    """The partition of a pipeline's spec texts; each text's hash is its sort key."""
    return f"{pipeline}#text"
