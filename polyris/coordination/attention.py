"""Keys and due times of the attention index (ADR #130 §7, §8).

``pipeline-tokens`` carries a sparse GSI keyed by ``attention`` (partition)
and ``attention_at`` (sort). The partition value is ``{kind}#{h}``, where
``h`` is the first hex digit of the SHA-256 of the row's key, so writes
spread over 16 partitions. The registration SFN computes the same digit with
JSONata ``$hash(execution_name, 'SHA-256')``; a parity test pins both.

Kinds: ``waiting`` (a reconcilable row the sweeper re-checks), ``paused`` (a
task held in run_task's pause wait, released by the sweeper once its run is
no longer paused), ``retry`` (a push start to retry), ``warn`` (a warning).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Tuple

#: The GSI on ``pipeline-tokens`` and its key attributes (template.yaml).
INDEX = "attention-index"
KEY = "attention"
DUE = "attention_at"
#: Attributes the index projects: what the bell and the sweeper read from it.
PROJECTION = ("status", "task_name", "pipeline_name", "pipeline_execution", "task_date",
              "kind", "error", "finished_at", "started_at", "attempts")

WAITING = "waiting"
PAUSED = "paused"
RETRY = "retry"
WARN = "warn"
KINDS = frozenset({WAITING, PAUSED, RETRY, WARN})
SHARDS = "0123456789abcdef"

#: A newly registered task, or a task entering its pause wait, is first due for a sweeper check after this long.
FIRST_DUE = timedelta(minutes=10)
#: Later checks back off up to every 30 minutes (≤ 48 checks per task per day).
BACKOFF = (timedelta(minutes=10), timedelta(minutes=20), timedelta(minutes=30))


def shard(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[0]


def attention(kind: str, key: str) -> str:
    """The ``attention`` value for a row of ``kind`` keyed by ``key``."""
    if kind not in KINDS:
        raise ValueError(f"unknown attention kind {kind!r}; expected one of {sorted(KINDS)}")
    return f"{kind}#{shard(key)}"


def partitions(kind: str) -> Tuple[str, ...]:
    """Every partition value of ``kind``; the sweeper queries each one."""
    if kind not in KINDS:
        raise ValueError(f"unknown attention kind {kind!r}; expected one of {sorted(KINDS)}")
    return tuple(f"{kind}#{h}" for h in SHARDS)


def next_due(now: datetime, attempts: int) -> datetime:
    """When a still-waiting row is due again after ``attempts`` checks."""
    if attempts < 0:
        raise ValueError("attempts cannot be negative")
    return now + BACKOFF[min(attempts, len(BACKOFF) - 1)]
