"""Shared paging for the History feeds (``/api/runs`` + ``/api/tasks``).

Both feeds answer the same question — *"what happened, newest first"* — over the same
task rows, so they page the same way: an opaque ``before`` cursor carrying a
``started_at``, meaning **"give me what is older than T"**. ``next`` in the response
is the cursor for the older page, or ``None`` when nothing older exists.

Why ``started_at`` and not a DynamoDB key: the runs feed merges two sources whose only
shared ordering attribute is ``started_at`` (executions and Backfills, ADR #95), and it
is what both feeds already sort by. A key-based cursor would have to encode both
sources; a timestamp encodes neither and works for both.

**Where** the rows come from stays per-route (ADR #108) — a cross-pipeline feed shards
on ``date`` and fans the window out one query per day, a pipeline-filtered feed reads
``pipeline-date-index`` and has no window at all — but every path hands its rows to
:func:`page_by_started_at`, so there is one paging dialect regardless of the source.
"""

from datetime import datetime, date as date_cls, timezone, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from dal import executions_repo
from constants import Limits
from logger import log


def is_older(row: Dict, before: str) -> bool:
    """Cursor predicate: was this row already served by an earlier page?

    No cursor means the first page, where everything qualifies. Rows without a
    ``started_at`` compare as the oldest possible value, which is where they sort.
    """
    return not before or (row.get('started_at') or '') < before


def feed_dates(before: str = '') -> List[str]:
    """Dates one page of a cross-pipeline feed fans out over, newest first.

    ``date`` is the natural shard key for a time-ordered feed across all pipelines, so
    the fan-out is the correct access pattern here rather than debt (ADR #108). The
    page starts at the cursor's date and walks back to the edge of the SLA window:
    everything newer than the cursor was already served, and a row's ``started_at`` is
    never earlier than its logical ``date``, so no older row can hide on a newer date.
    Without a cursor the walk starts today. Bounded by ``Limits.SLA_DAYS`` queries, and
    shorter the deeper you page.

    An unparseable cursor degrades to today rather than 4xx — same contract as
    ``safe_param_int``: a bad query param costs you the cursor, not the feed.
    """
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=Limits.SLA_DAYS - 1)

    start = today
    if before:
        try:
            start = min(date_cls.fromisoformat(before[:10]), today)
        except ValueError:
            log.warn("feed_dates", "Unparseable cursor; starting the page at today",
                     before=before)

    dates: List[str] = []
    day = start
    while day >= oldest:
        dates.append(day.strftime('%Y-%m-%d'))
        day -= timedelta(days=1)
    return dates


def page_by_started_at(rows: List[Dict], before: str, limit: int) -> Tuple[List[Dict], Optional[str]]:
    """Cut one page out of a ``started_at``-ordered feed.

    Drops what the cursor already served, sorts newest-first, and takes ``limit``.
    Returns ``(page, next_cursor)``; the cursor is the last row's ``started_at``, to be
    passed back as ``before``. ``None`` means nothing older exists — which is the point
    of the exercise: the caller can now say "that is all" honestly instead of silently
    dropping every row past ``limit``.

    A page never ends *inside* a group of rows sharing one ``started_at``. The cursor is
    a strict ``<``, so a twin left behind would be filtered out by the very cursor that
    was supposed to fetch it — gone for good. Absorbing the twins instead (the page runs
    a little long; ties are millisecond-precision, so in practice never) is what makes a
    plain timestamp sufficient here: it is the same rule the reads below it follow —
    ``query_runs_by_pipeline`` cuts on whole dates, ``query_runs_by_date`` on whole
    pipelines, and this on whole instants. A composite cursor would only be buying back
    what the boundary already guarantees.

    Rows without a ``started_at`` sort last and cannot be a cursor, so a page ending on
    one stops paging. Every task row is stamped at registration and every Backfill
    record at start, so that is a broken-data guard, not a supported mode.
    """
    kept = [r for r in rows if is_older(r, before)]
    kept.sort(key=lambda r: r.get('started_at') or '', reverse=True)

    page = kept[:limit]
    if not page or len(kept) <= limit:
        return page, None

    cursor = page[-1].get('started_at') or None
    if cursor is None:
        return page, None

    for row in kept[limit:]:
        if (row.get('started_at') or '') != cursor:
            break
        page.append(row)

    if len(kept) <= len(page):
        return page, None       # the twins were the tail of the feed
    return page, cursor


def pipeline_rows_before(
    pipeline_name: str,
    before: str,
    want: int,
    count_fn: Callable[[List[Dict]], int],
    projection: str = None,
    expr_names: dict = None,
) -> List[Dict]:
    """Task rows for one pipeline, newest first — one page's worth **plus one**.

    Reads ``pipeline-date-index`` (ADR #108), so there is no date window — depth is
    bounded by the row TTL alone.

    The extra row is not slack: it is how the page learns it is not the last one.
    :func:`page_by_started_at` infers "there is more" from having more rows than it
    can fit, so stopping at exactly ``want`` would make a full page indistinguishable
    from the end of the feed — and the rows behind it unreachable. Every other source
    reads a whole window or date and gets that for free; only this one is bounded by
    what we ask for, so only this one has to ask for one more than it needs.

    The cursor otherwise only says *where to start*. The rows on its own date that are
    newer than it were already served, so the first read can come back almost entirely
    consumed; ``count_fn`` counts what a page would actually get out of the buffer, and
    we keep reading (whole dates at a time — an execution's task set is never split
    across a read) until the page can be filled or the index runs out.

    Without that loop a busy cut date would stall paging outright: the repo's cursor is
    a date, so every call would re-read the same already-served rows, hand back a short
    page, and never reach the older dates behind them.
    """
    rows: List[Dict] = []
    cursor = before[:10] if before else None
    while True:
        chunk, cursor = executions_repo.query_runs_by_pipeline(
            pipeline_name,
            min_runs=want + 1,
            before_date=cursor,
            projection=projection,
            expr_names=expr_names,
        )
        rows.extend(chunk)
        # The repo cuts on a date boundary and only ever hands back a *strictly older*
        # cursor, so this terminates: either we overshoot the page or the index is
        # exhausted — and exhausted is the only honest way to end up with exactly
        # `want` rows.
        if cursor is None or count_fn(rows) > want:
            return rows
