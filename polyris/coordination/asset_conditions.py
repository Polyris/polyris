"""Evaluate asset conditions from facts (ADR #130 §4, rule B).

For a target date D:

* plain ``a``: the freshest fact of ``a`` that existed for D. An hourly or
  daily fact must be dated exactly D; a weekly or monthly fact must be dated
  no later than D, in D's ISO week/month or the one before.
* ``a.within(hours=N)``: the newest fact of the latest partition date up to D
  must be at most N hours old at ``now``.
* ``a.consecutive(days=N)``: facts exist for each of the N dates ending at D.

Groups combine members with AND / OR. Facts come from a ``FactReader``; the
reconciler's DAL implements it over the partition index and recent writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, Iterator, List, Optional, Protocol, Tuple

from .partition import (
    EXACT_GRANULARITIES,
    PERIOD_GRANULARITIES,
    in_current_or_previous_period,
    oldest_useful_date,
)
from .spec import AND, AssetGroup, AssetLeaf, SpecNode

OK = "ok"
NO_FACT = "no_fact"
STALE = "stale"
MISSING_DATES = "missing_dates"


@dataclass(frozen=True)
class Fact:
    """One asset materialisation, as the evaluator sees it."""

    asset_name: str
    partition_date: date
    #: Timezone-aware UTC write time.
    event_time: datetime
    #: Declared granularity of the asset; the DAL fills a default when unknown.
    granularity: str


class FactReader(Protocol):
    def facts(self, asset_name: str, *, last: date, first: Optional[date]) -> Iterable[Fact]:
        """Facts with ``first <= partition_date <= last`` (no lower bound when
        ``first`` is None), newest partition date first. All facts of one
        partition date must be yielded consecutively."""
        ...


@dataclass(frozen=True)
class AssetResult:
    asset_name: str
    #: ``latest``, ``within`` or ``consecutive``.
    condition: str
    ready: bool
    reason: str
    fact: Optional[Fact] = None
    missing_dates: Tuple[date, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Evaluation:
    ready: bool
    assets: Tuple[AssetResult, ...]


def evaluate(
    spec: AssetGroup,
    *,
    target: date,
    now: datetime,
    reader: FactReader,
    known_facts: Iterable[Fact] = (),
) -> Evaluation:
    """Evaluate ``spec`` for target date ``target``.

    ``known_facts`` are facts the caller already holds, typically the stream
    record that triggered the call; they are merged with what the reader
    returns, so read lag cannot hide them. Every leaf is evaluated, even
    after the outcome is known, so the result explains every asset.
    """
    known: Dict[str, List[Fact]] = {}
    for fact in known_facts:
        known.setdefault(fact.asset_name, []).append(fact)
    results: List[AssetResult] = []
    ready = _evaluate_node(spec, target, now, reader, known, results)
    return Evaluation(ready, tuple(results))


def satisfies(fact: Fact, target: date) -> bool:
    """Whether ``fact`` serves ``target`` under rule B."""
    if fact.granularity in EXACT_GRANULARITIES:
        return fact.partition_date == target
    if fact.granularity in PERIOD_GRANULARITIES:
        return fact.partition_date <= target and in_current_or_previous_period(
            fact.partition_date, target, fact.granularity
        )
    raise ValueError(f"asset {fact.asset_name!r} has unknown granularity {fact.granularity!r}")


def _evaluate_node(
    node: SpecNode,
    target: date,
    now: datetime,
    reader: FactReader,
    known: Dict[str, List[Fact]],
    results: List[AssetResult],
) -> bool:
    if isinstance(node, AssetLeaf):
        result = _evaluate_leaf(node, target, now, reader, known.get(node.asset_name, []))
        results.append(result)
        return result.ready
    outcomes = [_evaluate_node(m, target, now, reader, known, results) for m in node.members]
    return all(outcomes) if node.operator == AND else any(outcomes)


def _evaluate_leaf(
    leaf: AssetLeaf, target: date, now: datetime, reader: FactReader, known: List[Fact]
) -> AssetResult:
    if leaf.consecutive_days is not None:
        return _consecutive(leaf.asset_name, leaf.consecutive_days, target, reader, known)
    if leaf.freshness_hours is not None:
        return _within(leaf.asset_name, leaf.freshness_hours, target, now, reader, known)
    return _latest(leaf.asset_name, target, reader, known)


def _latest(name: str, target: date, reader: FactReader, known: List[Fact]) -> AssetResult:
    first = oldest_useful_date(target)
    seen_any = False
    for fact in merge_facts(reader.facts(name, last=target, first=first), known, first, target):
        seen_any = True
        if satisfies(fact, target):
            return AssetResult(name, "latest", True, OK, fact)
    return AssetResult(name, "latest", False, STALE if seen_any else NO_FACT)


def _within(
    name: str, hours: float, target: date, now: datetime, reader: FactReader, known: List[Fact]
) -> AssetResult:
    newest: Optional[Fact] = None
    for fact in merge_facts(reader.facts(name, last=target, first=None), known, None, target):
        if newest is not None and fact.partition_date != newest.partition_date:
            break
        if newest is None or fact.event_time > newest.event_time:
            newest = fact
    if newest is None:
        return AssetResult(name, "within", False, NO_FACT)
    fresh = newest.event_time >= now - timedelta(hours=hours)
    return AssetResult(name, "within", fresh, OK if fresh else STALE, newest)


def _consecutive(
    name: str, days: int, target: date, reader: FactReader, known: List[Fact]
) -> AssetResult:
    first = target - timedelta(days=days - 1)
    present = {f.partition_date for f in merge_facts(reader.facts(name, last=target, first=first), known, first, target)}
    missing = tuple(d for d in (first + timedelta(days=i) for i in range(days)) if d not in present)
    if missing:
        return AssetResult(name, "consecutive", False, MISSING_DATES, missing_dates=missing)
    return AssetResult(name, "consecutive", True, OK)


def merge_facts(
    stream: Iterable[Fact], extra: List[Fact], first: Optional[date], last: date
) -> Iterator[Fact]:
    """Merge a newest-first fact stream with extra facts, newest partition first.

    Keeps only extras with ``first <= partition_date <= last`` and drops
    duplicates (same partition date and event time). The DAL uses it to merge
    index reads with recent base-table reads; the evaluator to add known facts.
    """
    pending = sorted(
        (f for f in extra if f.partition_date <= last and (first is None or f.partition_date >= first)),
        key=lambda f: f.partition_date,
        reverse=True,
    )
    seen: set[Tuple[date, datetime]] = set()

    def fresh(fact: Fact) -> bool:
        key = (fact.partition_date, fact.event_time)
        if key in seen:
            return False
        seen.add(key)
        return True

    i = 0
    for fact in stream:
        while i < len(pending) and pending[i].partition_date >= fact.partition_date:
            if fresh(pending[i]):
                yield pending[i]
            i += 1
        if fresh(fact):
            yield fact
    for fact in pending[i:]:
        if fresh(fact):
            yield fact
