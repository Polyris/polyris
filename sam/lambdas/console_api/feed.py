"""Shared paging for the History feeds (``/api/runs`` + ``/api/tasks``) and the run lists.

Both feeds answer the same question — *"what happened, newest first"* — so they page
the same way: an opaque ``before`` cursor carrying a ``started_at``, meaning **"give me
what is older than T"**. ``next`` in the response is the cursor for the older page, or
``None`` when nothing older exists.

The run lists of 1.3 (the Runs tab, a pipeline's runs, the pipelines list) read run
rows from ``run-start-index`` (ADR #137 §5): :func:`run_rows_before` walks the start
days newest first, and :func:`run_entry` gives each run its status from its summary.

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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from polyris.coordination import run_end, run_row, task_row
from dal import executions_repo
from constants import Limits
from logger import log
from utils import should_skip_token_row


def is_older(row: Dict, before: str) -> bool:
    """Cursor predicate: was this row already served by an earlier page?

    No cursor means the first page, where everything qualifies. Rows without a
    ``started_at`` compare as the oldest possible value, which is where they sort.
    """
    return not before or (row.get('started_at') or '') < before


def feed_dates(before: str = '', days: int = Limits.SLA_DAYS) -> List[str]:
    """Dates one page of a cross-pipeline feed fans out over, newest first.

    ``date`` is the natural shard key for a time-ordered feed across all pipelines, so
    the fan-out is the correct access pattern here rather than debt (ADR #108). The
    page starts at the cursor's date and walks back to the edge of the window of
    ``days`` days: everything newer than the cursor was already served. A row whose
    ``started_at`` is earlier than its logical ``date`` hides on a newer date: a run
    can start before its date (ADR #137 §5), and the Tasks feed, which walks logical
    dates, shows such a run's tasks once its date is walked. Without a cursor the walk
    starts today. Bounded by ``days``
    queries, and shorter the deeper you page. The run lists walk start days the same
    way (:func:`run_rows_before`).

    An unparseable cursor degrades to today rather than 4xx — same contract as
    ``safe_param_int``: a bad query param costs you the cursor, not the feed.
    """
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=days - 1)

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


def page_by_started_at(rows: List[Dict], before: str, limit: int, *, more: bool = False) -> Tuple[List[Dict], Optional[str]]:
    """Cut one page out of a ``started_at``-ordered feed.

    Drops what the cursor already served, sorts newest-first, and takes ``limit``.
    Returns ``(page, next_cursor)``; the cursor is the last row's ``started_at``, to be
    passed back as ``before``. ``None`` means nothing older exists — which is the point
    of the exercise: the caller can now say "that is all" honestly instead of silently
    dropping every row past ``limit``.

    A page never ends *inside* a group of rows sharing one ``started_at``. The cursor is
    a strict ``<``, so a twin left behind would be filtered out by the very cursor that
    was supposed to fetch it — gone for good. Absorbing the twins instead (the page runs
    long: every task row a ``Start_Run`` writes carries its one ``started_at``, so a page
    of tasks holds whole runs, whatever ``limit`` says) is what makes a
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
        # The twins were the tail of what was read: the feed's end only if the read reached it (``more``).
        return page, cursor if more else None
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


def run_rows_before(before: str, want: Optional[int], *, days: int, pipeline: str = '',
                    run_dates: Optional[Tuple[str, str]] = None,
                    keep: Optional[Callable[[Mapping[str, Any]], bool]] = None) -> Tuple[List[Dict], bool]:
    """Run rows older than the cursor, newest first, from the start days of a window of ``days`` days (ADR #137 §5).

    The shard key is the day a run started, not its logical date, so the walk stops
    once ``want`` rows are read: no run on an older start day started later. Pass
    ``want`` one above the page, for the reason :func:`pipeline_rows_before` gives,
    and None to read the whole window: a list keyed by logical dates (``run_dates``)
    must, since a run may start before its date or long after it. ``pipeline`` keeps
    one pipeline's runs, and ``keep`` the rows it accepts, which alone count toward
    ``want``. Raises ``executions_repo.IndexBuilding`` while DynamoDB builds the index.

    Returns the rows and whether the walk stopped at ``want`` before the window's end: older rows may exist, which
    :func:`page_by_started_at` needs when the twins of its last row were the tail of the read (review of TR-6d).
    """
    rows: List[Dict] = []
    for day in feed_dates(before, days):
        read = executions_repo.runs_started(
            day, before=before or None, pipeline=pipeline or None, run_dates=run_dates,
            want=None if want is None or keep is not None else want - len(rows))
        rows.extend(item for item in read if keep is None or keep(item))
        if want is not None and len(rows) >= want:
            return rows, True
    return rows, False


def run_counts(item: Mapping[str, Any]) -> Optional[Dict[str, int]]:
    """A run row's summary: its rows per status, or None before the first full pass wrote one (ADR #137 §4)."""
    summary = item.get(run_row.SUMMARY)
    if not isinstance(summary, Mapping):
        return None
    return {str(status): int(count) for status, count in summary.items()}


def run_entry(item: Mapping[str, Any]) -> Dict[str, Any]:
    """A run row as the run lists show it (ADR #137 §4): its status from its summary and step mark, and while it
    runs the count of its failed tasks; its finish and duration once it has ended."""
    pipeline_execution = str(item[run_row.TABLE_KEY])[len(run_row.KEY_PREFIX):]
    counts = run_counts(item)
    status = run_end.shown_status(counts, step_failed=bool(item.get(run_row.STEP_FAILURE)))
    started_at = item.get(run_row.STARTED_AT) or None
    finished_at = (item.get(run_row.FINISHED_AT) or None) if status != 'running' else None
    return {
        'pipeline_name': item.get(run_row.PIPELINE, ''),
        'pipeline_execution': pipeline_execution,
        'pipeline_execution_short': task_row.short(pipeline_execution),
        'status': status,
        'failed_tasks': run_end.failed_tasks(counts),
        'date': item.get(run_row.RUN_DATE),
        'started_at': started_at,
        'finished_at': finished_at,
        'duration_ms': duration_ms(started_at, finished_at),
        'triggered_by': item.get(run_row.TRIGGERED_BY) or None,
        # A failed direct step (a `wait`, `sns`, `s3` step and the like) has no row: the run row's mark names it and
        # its error, which fail the run once its rows are final (ADR #137 §1).
        'failed_step': _failed_step(item.get(run_row.STEP_FAILURE)),
    }


def _failed_step(mark: Any) -> Optional[Dict[str, str]]:
    if not isinstance(mark, Mapping):
        return None
    return {'step': str(mark.get(run_row.STEP_MARK_STEP, '')), 'error': str(mark.get(run_row.STEP_MARK_ERROR, ''))}


def duration_ms(started_at: Optional[str], finished_at: Optional[str]) -> Optional[int]:
    """Milliseconds from ``started_at`` to ``finished_at``, or None without both or with one unreadable."""
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        end = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
    except (ValueError, TypeError) as e:
        log.warn("feed", "Bad timestamp on a run; duration_ms left None",
                 started_at=started_at, finished_at=finished_at, error=str(e))
        return None
    return int((end - start).total_seconds() * 1000)


#: What a date's runs read of their task rows: date-pipeline-index projects every field (template.yaml).
_DATE_PROJECTION = ('pipeline_execution, pipeline_execution_short, pipeline_name, '
                    '#d, #s, started_at, finished_at, execution_name')
_DATE_NAMES = {'#d': 'date', '#s': 'status'}
#: What a run list reads of a run row it fetches by key: what run-start-index projects, and its start.
RUN_ROW_FIELDS = (*run_row.INDEX_FIELDS, run_row.STARTED_AT)


def runs_of_date(date: str, pipeline: str = '') -> List[Dict[str, Any]]:
    """The runs of one logical date, found through its task rows (ADR #137 §4, §5), with no window.

    A 1.3 run shows as its run row says, read in one ``BatchGetItem`` for the date. A
    run from before 1.3 has no run row: its status is derived from its task rows, and
    its trigger read from one of them. Reads the whole date: a cut on a pipeline
    boundary (ADR #113) kept every run's task set whole but dropped, without a word,
    the runs of every pipeline past it once a date outgrew one DynamoDB page (review
    of TR-6d).
    """
    key: Any = Key('date').eq(date)
    if pipeline:
        key = key & Key('pipeline_name').eq(pipeline)
    items = executions_repo.query_runs_by_date(date, min_rows=None, key_condition=key,
                                               projection=_DATE_PROJECTION, expr_names=_DATE_NAMES)
    groups: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if should_skip_token_row(item):
            continue
        pipeline_execution, name = item.get('pipeline_execution'), item.get('pipeline_name', '')
        if not pipeline_execution or pipeline_execution == 'unknown' or not name or name == 'unknown':
            continue
        group = groups.setdefault(pipeline_execution, {
            'pipeline_name': name, 'date': item.get('date'), 'statuses': [], 'started_at': '', 'finished_at': '',
            'short': item.get('pipeline_execution_short') or task_row.short(pipeline_execution),
            'sample': item.get('execution_name', '')})
        group['statuses'].append(item.get('status', 'waiting'))
        started, finished = item.get('started_at') or '', item.get('finished_at') or ''
        if started and (not group['started_at'] or started < group['started_at']):
            group['started_at'] = started
        if finished > group['finished_at']:
            group['finished_at'] = finished
    if not groups:
        return []
    run_items = executions_repo.batch_get_run_meta([run_row.key(pe) for pe in groups], RUN_ROW_FIELDS)
    runs: List[Dict[str, Any]] = []
    legacy: List[Dict[str, Any]] = []
    for pipeline_execution, group in groups.items():
        run_item = run_items.get(run_row.key(pipeline_execution))
        if run_item is not None:
            runs.append(run_entry(run_item))
            continue
        counts = run_end.summary(group['statuses'])
        status = run_end.shown_status(counts, step_failed=False)
        started_at = group['started_at'] or None
        finished_at = (group['finished_at'] or None) if status != 'running' else None
        legacy.append({
            'pipeline_name': group['pipeline_name'], 'pipeline_execution': pipeline_execution,
            'pipeline_execution_short': group['short'], 'status': status,
            'failed_tasks': run_end.failed_tasks(counts), 'date': group['date'],
            'started_at': started_at, 'finished_at': finished_at,
            'duration_ms': duration_ms(started_at, finished_at), 'triggered_by': None,
            '_sample_exec_name': group['sample'],
        })
    fill_triggered_by(legacy)
    return runs + legacy


def fill_triggered_by(executions: list) -> None:
    """Back-fill triggered_by on runs from before 1.3, read through an index that does not project it.

    Each entry names one of its task rows in ``_sample_exec_name``, which this pops.
    """
    samples = {}  # pipeline_execution → sample execution_name
    for e in executions:
        name = e.pop('_sample_exec_name', None)
        if name and not e.get('triggered_by'):
            samples[e['pipeline_execution']] = name

    if not samples:
        return

    try:
        meta = executions_repo.batch_get_run_meta(list(samples.values()))
        for e in executions:
            if e.get('triggered_by'):
                continue
            sample = samples.get(e['pipeline_execution'])
            if sample:
                e['triggered_by'] = (meta.get(sample) or {}).get('triggered_by') or None
    except (ClientError, BotoCoreError) as err:
        log.warn("fill_triggered_by", "batch_get failed; triggered_by left null",
                 error=str(err))


def latest_run_of_date(pipeline: str, date: str) -> Optional[str]:
    """The run of ``pipeline`` for logical ``date`` that started last, or None when the date has none.

    Found through the pipeline's task rows of the date (``rows_of_pipeline_date``, a
    query where a scan of the table stood, ADR #137 §5); a run starts with its first
    task row, so the run whose earliest row started last is the latest.
    """
    starts: Dict[str, str] = {}
    for row in executions_repo.rows_of_pipeline_date(pipeline, date):
        pipeline_execution = row.get('pipeline_execution')
        if not pipeline_execution:
            continue
        started = row.get('started_at') or ''
        if pipeline_execution not in starts or (started and (not starts[pipeline_execution]
                                                             or started < starts[pipeline_execution])):
            starts[pipeline_execution] = started
    return max(starts, key=lambda run: (starts[run], run)) if starts else None
