"""The write of a task row by any writer the transition table names (ADR #137 §3).

The dispatcher's decisions, stops and resets and the console's actions build
their update here, so each is conditional on the status and the generation its
writer read (invariant 5), moves the row only as the table gives that writer the
move (invariant 7), raises the generation and sets the skip origin as the table
says, and keeps the row's entry in ``attention-index`` by its new status
(invariant 9). ``run_task`` writes its own rows from its template, which the
census of writers compares with the same table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from boto3.dynamodb.types import TypeSerializer

from . import attention, ownership, retention, task_row, transitions
from .times import format_utc

_serializer = TypeSerializer()
#: A task row's write is conditional on what its writer read of it.
ROW_CONDITION = f"#status = :read AND {task_row.FENCE}"
#: The same for a row that a 1.2 run left with no generation or with 0, which 1.2's templates write when their
#: input has none (``ownership.is_legacy``); only its stop writes such a row.
LEGACY_ROW_CONDITION = "#status = :read AND (attribute_not_exists(#g) OR #g = :legacy)"


def task_update(*, key: str, read_status: str, read_generation: int, writer: str, transition: Optional[str],
                fields: Mapping[str, Any], removed: Sequence[str], due: Optional[datetime], table: str,
                now: datetime) -> Dict[str, Any]:
    """The row's ``Update``.

    It is conditional on the status and the generation its writer read. A transition must be one the table gives
    that writer from that status; it sets the table's target, raises the generation and sets the skip origin as
    the table says. ``fields`` are set and ``removed`` removed; the history TTL is renewed; the row's entry in
    ``attention-index`` follows its new status: its kind at ``due``, or the time of a failure (§6).
    """
    status = read_status
    move = None
    if transition:
        move = transitions.task_transition(transition)
        if read_status not in move.sources or writer not in move.writers:
            # Invariant 7: a writer moves a row only as the table gives it the move.
            raise ValueError(f"{key}: the transition table gives the {writer} no {transition!r} from {read_status!r}")
        status = move.target
    names = {"#status": task_row.STATUS, "#g": task_row.GENERATION, "#ttl": task_row.TTL,
             "#att": attention.KEY, "#due": attention.DUE}
    legacy = read_generation == ownership.LEGACY
    values: Dict[str, Any] = {":read": read_status, ":ttl": retention.ttl(now, retention.HISTORY),
                              **({":legacy": ownership.LEGACY} if legacy else {":g": read_generation})}
    sets = ["#ttl = :ttl"]
    removes: List[str] = []
    fields = dict(fields)
    if move is not None:
        sets.append("#status = :status")
        values[":status"] = status
        if move.raises_generation:
            sets.append("#g = :g_next")
            values[":g_next"] = read_generation + 1
        if move.skip_origin is not None:
            fields[task_row.SKIP_ORIGIN] = move.skip_origin           # from the table, not from the writer
    for i, (name, value) in enumerate(sorted(fields.items())):
        names[f"#f{i}"] = name
        values[f":f{i}"] = value
        sets.append(f"#f{i} = :f{i}")
    for j, name in enumerate(removed):
        names[f"#r{j}"] = name
        removes.append(f"#r{j}")
    entry = entry_after(status, due, now, key)
    if entry is None:
        removes += ["#att", "#due"]
    else:
        values[":att"], values[":due"] = entry
        sets += ["#att = :att", "#due = :due"]
    return {"TableName": table, "Key": {task_row.TABLE_KEY: {"S": key}},
            "UpdateExpression": "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else ""),
            "ConditionExpression": LEGACY_ROW_CONDITION if legacy else ROW_CONDITION,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": typed(values)}


def entry_after(status: str, due: Optional[datetime], now: datetime, key: str) -> Optional[Tuple[str, str]]:
    """A row's entry in ``attention-index`` after a write: its kind by status, at its due time, or when it failed."""
    kind = transitions.attention_kind(status)
    if kind == attention.FAILED:
        return attention.attention(kind, key), format_utc(now)
    if kind is None or due is None:
        return None
    return attention.attention(kind, key), format_utc(due)


def typed(values: Mapping[str, Any]) -> Dict[str, Any]:
    """Values as DynamoDB's low-level API takes them."""
    return {name: _serializer.serialize(value) for name, value in values.items()}
