"""EventBridge's event patterns, as an event source mapping's FilterCriteria and an event bus rule apply them.

The patterns follow EventBridge's syntax: a nested object matches a nested
object, and a list at a leaf matches when the record's value equals one of its
values or passes one of its filters. `anything-but`, `prefix` and `wildcard`
need the field present; `exists` tests for it. A field that is missing matches
nothing else: an insert, which has no `OldImage`, matches no pattern on the old
image. `test_stream_filters.py` checks this against what AWS delivered for the
same records in TR-0 (C1, C2).

A JSON `null` matches `[null]`. EventBridge's documentation does not say whether
`anything-but` matches it, so the stand takes the strict reading, that it does
not: a pattern that must let a null through names `null` itself, and holds
under either reading. The rule on the end of executions (`ExecutionEndRule`)
does, for an execution stopped without an error (ADR #137 §7).
"""

import re
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
            if all(v is not None and v not in excluded for v in values):
                return True
        elif operator == "prefix":
            if any(isinstance(v, str) and v.startswith(argument) for v in values):
                return True
        elif operator == "wildcard":
            if any(isinstance(v, str) and _wildcard(argument).fullmatch(v) for v in values):
                return True
        else:
            raise ValueError(f"filter operator {operator!r} is not modelled")
    return False


def _wildcard(pattern: str) -> "re.Pattern[str]":
    """A `wildcard` value as EventBridge reads it: `*` matches any run of characters, `\\*` and `\\\\` are the
    characters themselves, and two `*` in a row are refused, as EventBridge refuses them."""
    parts: List[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            escaped = pattern[index + 1:index + 2]
            if escaped not in ("*", "\\"):
                raise ValueError(f"a backslash escapes only * or \\ in a wildcard: {pattern!r}")
            parts.append(re.escape(escaped))
            index += 2
            continue
        if char == "*" and parts and parts[-1] == ".*":
            raise ValueError(f"consecutive wildcard characters are not supported: {pattern!r}")
        parts.append(".*" if char == "*" else re.escape(char))
        index += 1
    return re.compile("".join(parts), re.DOTALL)
