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
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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


def needs_redeploy(registered_format: Any) -> bool:
    """A pipeline deployed with an older format than this one, or with none (before 1.3).

    ``registered_format`` is the registry's ``REGISTRY_FORMAT_FIELD`` as read,
    ``None`` when absent; the console marks such a pipeline "redeploy needed".
    """
    if isinstance(registered_format, bool):
        return True
    try:
        return int(registered_format) < FORMAT_VERSION
    except (TypeError, ValueError):
        return True


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

# ── The items of ``pipeline-specs`` (§8) ─────────────────────────────────────
# Both kinds share the table's two string keys. A text: ``PK`` = ``text_partition(pipeline)``,
# ``SK`` = the text's hash, ``TEXT`` = the text. A version: ``PK`` = the ``spec_id``,
# ``SK`` = the task_id, ``VERSION_HASH`` = the hash of that task's text.
#: The table's partition key, and its sort key.
PK = "pk"
SK = "sk"
#: A text item's attribute: the task's spec as its canonical JSON.
TEXT = "spec_text"
#: A version item's attribute: the hash of its task's text, the field a task row names it by.
VERSION_HASH = ROW_TEXT_HASH

# ── The limits of a pipeline (§8), which polyris-deploy and polyris-validate check ──
#: The declared maximum of tasks per pipeline. TR-7 measures the real number; Start_Run's
#: timeout and the sweeper's window for a ``creating`` run row derive from it (§1).
MAX_TASKS = 2000
#: A pipeline's specs together, in UTF-8 bytes: Start_Run reads them all into one Lambda at a run's start (D117).
MAX_VERSION_BYTES = 16 * 1024 * 1024
#: A task's outlets: run_task's success is one transaction of at most 100 items, the
#: status, the output and one fact per outlet; Mark Successful writes the same.
MAX_OUTLETS = 98
#: Every spec names these; the other keys of ``TaskSpecKey`` may be absent.
REQUIRED = frozenset({K.TASK_ID, K.TASK_TYPE, K.DEPENDENCIES, K.TRIGGER_RULE})
#: What a task row carries for the dispatcher, which decides on these alone (§8).
DECISION_KEYS = (K.DEPENDENCIES, K.TRIGGER_RULE, K.ORCHESTRATION_TIMEOUT, K.WAIT_BEFORE, K.WAIT_FOR)
_KNOWN = frozenset(k.value for k in TaskSpecKey)


class SpecError(ValueError):
    """A spec that cannot be run; its message is the cause that ``Start_Run``'s warning shows."""


@dataclass(frozen=True)
class Version:
    """A pipeline's spec version as ``polyris-deploy`` writes it: each task's text, keyed by task_id."""
    pipeline: str
    texts: Mapping[str, str]

    @property
    def hashes(self) -> Dict[str, str]:
        """Each task's text hash, keyed by task_id."""
        return {task_id: text_hash(text) for task_id, text in self.texts.items()}

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.hashes.items())

    @property
    def spec_id(self) -> str:
        return spec_id(self.pipeline, self.fingerprint)

    @property
    def task_count(self) -> int:
        return len(self.texts)

    def text_items(self) -> List[Dict[str, str]]:
        """One item per distinct text: a text that two tasks share is stored once."""
        partition = text_partition(self.pipeline)
        unique = {text_hash(text): text for text in self.texts.values()}
        return [{PK: partition, SK: digest, TEXT: text} for digest, text in sorted(unique.items())]

    def version_items(self) -> List[Dict[str, str]]:
        """One item per task, naming its text by hash."""
        version = self.spec_id
        return [{PK: version, SK: task_id, VERSION_HASH: digest} for task_id, digest in sorted(self.hashes.items())]


def build_version(pipeline: str, specs: Iterable[Mapping[str, Any]]) -> Version:
    """The version of ``specs``, each turned into its canonical text; a task_id named twice is refused."""
    texts: Dict[str, str] = {}
    for spec in specs:
        task_id = str(spec[K.TASK_ID])
        if task_id in texts:
            raise ValueError(f"a spec version names a task twice: {task_id!r}")
        texts[task_id] = canonical_json(dict(spec))
    return Version(pipeline, texts)


def check_names(version_id: str, items: Sequence[Mapping[str, Any]], *,
                expected_count: Optional[int] = None) -> Dict[str, str]:
    """A version's task ids and their texts' hashes, checked against its count and its fingerprint.

    ``items`` are the version's items as read (``SK`` and ``VERSION_HASH``). This is all a pass of the
    dispatcher keeps of a version; a start reads its task's text by the hash (D117). Raises ``SpecError``
    with the cause on any mismatch.
    """
    if expected_count is not None and len(items) != expected_count:
        raise SpecError(f"spec version {version_id} has {len(items)} task(s), expected {expected_count}")
    pairs = []
    for item in items:
        task_id, digest = item.get(SK), item.get(VERSION_HASH)
        if not isinstance(task_id, str) or not isinstance(digest, str):
            raise SpecError(f"spec version {version_id} has an item without a task_id or a text hash: {dict(item)}")
        pairs.append((task_id, digest))
    _, _, expected_fingerprint = version_id.rpartition("#")
    try:
        actual = fingerprint(pairs)
    except ValueError as exc:
        raise SpecError(f"spec version {version_id}: {exc}") from exc
    if actual != expected_fingerprint:
        raise SpecError(f"spec version {version_id} does not match its fingerprint: its items hash to {actual}")
    return dict(pairs)


def check_version(version_id: str, items: Sequence[Mapping[str, Any]], texts: Mapping[str, str], *,
                  expected_count: Optional[int] = None) -> Dict[str, "TaskSpec"]:
    """Every spec of a version read back, checked: its count, each text's hash and the fingerprint.

    ``items`` are the version's items as read (``SK`` and ``VERSION_HASH``), ``texts`` the texts
    read for them, by hash. Raises ``SpecError`` with the cause on any mismatch, so a reader
    never runs or draws a version that is not the one that was written.
    """
    names = check_names(version_id, items, expected_count=expected_count)
    specs: Dict[str, TaskSpec] = {}
    for task_id, digest in names.items():
        if digest not in texts:
            raise SpecError(f"spec version {version_id}: the text {digest} of task {task_id!r} is missing")
        spec = parse(texts[digest], expected_hash=digest)
        if spec.task_id != task_id:
            raise SpecError(f"spec version {version_id}: the item of {task_id!r} names the spec of {spec.task_id!r}")
        specs[task_id] = spec
    return specs


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
