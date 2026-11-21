"""Lambda's event filtering of stream records, as an event source mapping's FilterCriteria applies it.

The patterns follow EventBridge's syntax: a nested object matches a nested
object, and a list at a leaf matches when the record's value equals one of its
values or passes one of its filters. `anything-but` and `prefix` need the
field present; `exists` tests for it. A field that is missing matches nothing
else: an insert, which has no `OldImage`, matches no pattern on the old image. `test_stream_filters.py` checks this against what
AWS delivered for the same records in TR-0 (C1, C2).
"""

from typing import Any, Dict, Iterable, List


def matches(pattern: Dict[str, Any], record: Any) -> bool:
    """Whether `record` passes `pattern`."""
    for key, rule in pattern.items():
        if isinstance(rule, dict):
            value = record.get(key) if isinstance(record, dict) else None
            if not isinstance(value, dict) or not matches(rule, value):
                return False
        elif isinstance(rule, list):
            if not _leaf(rule, record, key):
                return False
        else:
            raise ValueError(f"a pattern's leaf must be a list, not {rule!r}")
    return True


def matches_any(patterns: Iterable[Dict[str, Any]], record: Any) -> bool:
    """Whether `record` passes the mapping's FilterCriteria: one pattern of several suffices."""
    return any(matches(p, record) for p in patterns)


def _leaf(rule: List[Any], record: Any, key: str) -> bool:
    present = isinstance(record, dict) and key in record
    value = record.get(key) if present else None
    values = value if isinstance(value, list) else [value]
    for option in rule:
        if not isinstance(option, dict):
            if present and option in values:
                return True
            continue
        [(operator, argument)] = option.items()
        if operator == "exists":
            if bool(argument) == present:
                return True
        elif not present:
            continue
        elif operator == "anything-but":
            excluded = argument if isinstance(argument, list) else [argument]
            if all(v not in excluded for v in values):
                return True
        elif operator == "prefix":
            if any(isinstance(v, str) and v.startswith(argument) for v in values):
                return True
        else:
            raise ValueError(f"filter operator {operator!r} is not modelled")
    return False
