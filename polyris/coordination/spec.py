"""Asset condition specs: ``wait_for=[...]`` and ``schedule=[...]``.

Parses the JSON that ``generators._serialize_wait_for`` writes (keys
``asset_name``, ``freshness_hours``, ``consecutive_days``, ``operator``,
``assets``). Unknown keys are rejected rather than ignored: a fixture or a
writer using another shape (for example ``name`` instead of ``asset_name``)
must fail loudly, not evaluate to "no conditions".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Sequence, Tuple, Union

AND = "AND"
OR = "OR"

_LEAF_KEYS = frozenset({"asset_name", "freshness_hours", "consecutive_days"})
_GROUP_KEYS = frozenset({"operator", "assets"})


@dataclass(frozen=True)
class AssetLeaf:
    asset_name: str
    #: ``.within(hours=N)``: the newest fact must be at most N hours old.
    freshness_hours: Union[int, float, None] = None
    #: ``.consecutive(days=N)``: facts for each of the N dates ending at the target.
    consecutive_days: Union[int, None] = None


@dataclass(frozen=True)
class AssetGroup:
    operator: str
    members: Tuple["SpecNode", ...]


SpecNode = Union[AssetLeaf, AssetGroup]


def parse(items: Sequence[Mapping[str, Any]]) -> AssetGroup:
    """Parse a serialised spec. The top-level list is an AND of its items.

    An empty list parses to an empty AND group: the task has no asset
    conditions. Nested groups must not be empty.
    """
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"asset spec must be a list, got {type(items).__name__}")
    return AssetGroup(AND, tuple(_parse_item(item) for item in items))


def asset_names(node: SpecNode) -> FrozenSet[str]:
    """Every asset name the spec mentions, at any nesting depth."""
    if isinstance(node, AssetLeaf):
        return frozenset({node.asset_name})
    names: set[str] = set()
    for member in node.members:
        names |= asset_names(member)
    return frozenset(names)


def _parse_item(item: Mapping[str, Any]) -> SpecNode:
    # Recursion depth is the nesting depth of the user's `&`/`|` expression,
    # not the size of a task graph, so the iterative-traversal rule for graphs
    # (polyris/CLAUDE.md) does not apply here.
    if not isinstance(item, Mapping):
        raise ValueError(f"asset spec item must be an object, got {type(item).__name__}")
    keys = set(item)
    if "operator" in keys:
        unknown = keys - _GROUP_KEYS
        if unknown:
            raise ValueError(f"unknown keys in asset group: {sorted(unknown)}")
        operator = item["operator"]
        if operator not in (AND, OR):
            raise ValueError(f"asset group operator must be AND or OR, got {operator!r}")
        members = item.get("assets")
        if not isinstance(members, (list, tuple)) or not members:
            raise ValueError("asset group must list at least one member in 'assets'")
        return AssetGroup(operator, tuple(_parse_item(m) for m in members))
    if "asset_name" in keys:
        unknown = keys - _LEAF_KEYS
        if unknown:
            raise ValueError(f"unknown keys in asset condition: {sorted(unknown)}")
        name = item["asset_name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("asset condition needs a non-empty 'asset_name'")
        freshness = item.get("freshness_hours")
        consecutive = item.get("consecutive_days")
        if freshness is not None and (
            isinstance(freshness, bool) or not isinstance(freshness, (int, float)) or freshness <= 0
        ):
            raise ValueError(f"freshness_hours must be a positive number, got {freshness!r}")
        if consecutive is not None and (
            isinstance(consecutive, bool) or not isinstance(consecutive, int) or consecutive < 1
        ):
            raise ValueError(f"consecutive_days must be an integer >= 1, got {consecutive!r}")
        if freshness is not None and consecutive is not None:
            raise ValueError("an asset condition cannot use both freshness_hours and consecutive_days")
        return AssetLeaf(name, freshness, consecutive)
    raise ValueError(f"asset spec item needs 'asset_name' or 'operator', got keys {sorted(keys)}")
