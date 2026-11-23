"""How long each row of a run lives, and until when a Restart may renew them (ADR #137 §5, D108, D109).

A run writes rows with three lifetimes, each counted from the row's latest write:

- its history (task rows, run row, input, ``wait_for`` pointers): a year and 90
  days, longer than a run can live;
- a date's data (``output#``, operator markers included): a year, so a rerun of
  any date of the past year that skips tasks still finds their outputs;
- a task's input (``input#``): 90 days, the console's window of runs.

Nothing shortens them at a run's end (D108). Timeline events keep 90 days and
backfill records the history's lifetime while they run, 90 days once ended.
Warnings and push retries keep theirs (``records.WARNING_TTL``, ``RETRY_TTL``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: The longest a Standard execution runs, and so a run.
YEAR = timedelta(days=365)
HISTORY = YEAR + timedelta(days=90)
OUTPUT = YEAR
INPUT = timedelta(days=90)
TIMELINE = timedelta(days=90)
BACKFILL_RUNNING = HISTORY
BACKFILL_ENDED = timedelta(days=90)
#: A Restart is refused once its run started more than a year ago, less this margin: about nine months. The
#: rerun has these 90 days to read what its run wrote before it expires; nothing renews it (ADR #137 §3, §5).
#: An older date runs again instead.
RESTART_TTL_MARGIN = timedelta(days=90)


def ttl(now: datetime, lifetime: timedelta) -> int:
    """The ``ttl`` attribute of a row written at ``now``: the epoch second after which DynamoDB may delete it."""
    if now.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int((now + lifetime).timestamp())


def restart_allowed(run_started_at: datetime, now: datetime) -> bool:
    """Whether a Restart in a run that started at ``run_started_at`` is accepted at ``now``.

    Every row the Restart needs outlives the margin: the upstreams' ``output#``
    lives a year from its last write, after the run started, and the history a
    year and 90 days. DynamoDB may delete a row at any time after its TTL, so a
    renewal that came later could find rows of the run gone (§3).
    """
    if run_started_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return now < run_started_at + YEAR - RESTART_TTL_MARGIN
