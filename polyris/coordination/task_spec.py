"""A task's spec as ``pipeline-specs`` stores it, and the version it belongs to (ADR #137 §8).

A spec is stored as one string, its canonical JSON, and every reader checks the
text's sha256. DynamoDB stores the number ``1.0`` as ``1`` and keeps no order
in a map, so a hash of attributes read back would not match the deploy's; a
string keeps every byte. A version's fingerprint is the sha256 over its
``(task_id, text hash)`` pairs in key order, and names the version:
``spec_id = {pipeline}#{fingerprint}``. ``polyris-deploy``, ``Start_Run``, the
dispatcher and the console all call these functions, so no parity test is needed.

``parse`` reads a text back: it checks the hash, then rejects a key that
``TaskSpecKey`` does not know, as the parser of asset conditions does, and a
field the dispatcher decides on that has the wrong type. ``Start_Run`` writes a
task's ``decision_fields`` on its row (§8).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Tuple

from ..constants import TaskSpecKey
from . import asset_spec, run_row

K = TaskSpecKey

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


#: The task row's field that names its spec text in ``pipeline-specs``; ``run_row.SPEC_ID`` names the version.
ROW_TEXT_HASH = "spec_hash"
#: The task row's field that holds its decision fields as canonical JSON text: as attributes, boto3's
#: resource would give their numbers back as ``Decimal``, which the parser of asset conditions refuses.
ROW_DECISION = "decision"
#: Every spec names these; the other keys of ``TaskSpecKey`` may be absent.
REQUIRED = frozenset({K.TASK_ID, K.TASK_TYPE, K.DEPENDENCIES, K.TRIGGER_RULE})
#: What a task row carries for the dispatcher, which decides on these alone (§8).
DECISION_KEYS = (K.DEPENDENCIES, K.TRIGGER_RULE, K.ORCHESTRATION_TIMEOUT, K.WAIT_BEFORE, K.WAIT_FOR)
_KNOWN = frozenset(k.value for k in TaskSpecKey)


class SpecError(ValueError):
    """A spec that cannot be run; its message is the cause that ``Start_Run``'s warning shows."""


@dataclass(frozen=True)
class TaskSpec:
    """A spec read back and checked: its fields keyed by ``TaskSpecKey`` values, and its text's hash."""
    fields: Mapping[str, Any]
    text_hash: str

    @property
    def task_id(self) -> str:
        return str(self.fields[K.TASK_ID])

    def decision_fields(self) -> Dict[str, Any]:
        """The fields ``Start_Run`` writes on the task's row: those of ``DECISION_KEYS`` that the spec has."""
        return {key.value: self.fields[key] for key in DECISION_KEYS if key in self.fields}


def parse(text: str, *, expected_hash: str) -> TaskSpec:
    """A spec text from ``pipeline-specs``, checked against the hash it was stored under."""
    actual = text_hash(text)
    if actual != expected_hash:
        raise SpecError(f"a spec text does not match its hash: stored under {expected_hash}, hashes to {actual}")
    try:
        fields = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(f"a spec text is not JSON: {exc}") from exc
    if not isinstance(fields, dict):
        raise SpecError(f"a spec must be a JSON object, got {type(fields).__name__}")
    name = fields.get(K.TASK_ID, "?")
    unknown = set(fields) - _KNOWN
    if unknown:
        raise SpecError(f"spec of {name!r}: unknown keys {sorted(unknown)}")
    missing = REQUIRED - set(fields)
    if missing:
        raise SpecError(f"spec of {name!r}: missing keys {sorted(k.value for k in missing)}")
    _check(fields, name)
    return TaskSpec(fields, actual)


def _check(fields: Mapping[str, Any], name: Any) -> None:
    def fail(problem: str) -> SpecError:
        return SpecError(f"spec of {name!r}: {problem}")

    for key in (K.TASK_ID, K.TASK_TYPE):
        if not isinstance(fields[key], str) or not fields[key].strip():
            raise fail(f"{key.value} must be a non-empty string")
    dependencies = fields[K.DEPENDENCIES]
    if not isinstance(dependencies, list) or not all(isinstance(d, str) and d for d in dependencies):
        raise fail("dependencies must be a list of task names")
    if len(set(dependencies)) != len(dependencies):
        raise fail(f"dependencies name a task twice: {dependencies}")
    if not isinstance(fields[K.TRIGGER_RULE], str) or not fields[K.TRIGGER_RULE].strip():
        # A name this platform does not know is no error here: the dispatcher fails that task alone, with
        # the cause, so a pipeline deployed by a newer SDK fails only where it must (ADR #138 §2).
        raise fail("trigger_rule must be a non-empty string")
    for key, least in ((K.EXECUTION_TIMEOUT, 1), (K.ORCHESTRATION_TIMEOUT, 1), (K.WAIT_BEFORE, 0)):
        value = fields.get(key, least)
        if isinstance(value, bool) or not isinstance(value, int) or value < least:
            raise fail(f"{key.value} must be a whole number of seconds, at least {least}")
    if not isinstance(fields.get(K.SKIP_ON_BACKFILL, False), bool):
        raise fail("skip_on_backfill must be true or false")
    if K.WAIT_FOR in fields:
        try:
            asset_spec.parse(fields[K.WAIT_FOR])
        except ValueError as exc:
            raise fail(f"wait_for: {exc}") from exc


def decision_text(spec: TaskSpec) -> str:
    """What ``Start_Run`` writes in the task row's ``ROW_DECISION``."""
    return canonical_json(spec.decision_fields())


def decision_from_text(text: str) -> Dict[str, Any]:
    """The decision fields a task row holds, checked as ``parse`` checks a spec's."""
    try:
        fields = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SpecError(f"a task row's decision fields are not JSON: {exc}") from exc
    if not isinstance(fields, dict):
        raise SpecError(f"a task row's decision fields must be a JSON object, got {type(fields).__name__}")
    unknown = set(fields) - {k.value for k in DECISION_KEYS}
    missing = {K.DEPENDENCIES.value, K.TRIGGER_RULE.value} - set(fields)
    if unknown or missing:
        raise SpecError(f"a task row's decision fields: unknown {sorted(unknown)}, missing {sorted(missing)}")
    _check({**fields, K.TASK_ID.value: "row", K.TASK_TYPE.value: "row"}, "row")
    return fields
