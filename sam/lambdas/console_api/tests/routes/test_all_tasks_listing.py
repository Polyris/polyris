"""get_all_tasks: where the rows come from (ADR #108 — never a Scan), what must never
appear as a task instance (output-store / task_name-less rows), and the paging contract
it shares with /api/runs (an opaque started_at cursor — see feed.py).

pytest-mock (ADR #26), boundary mocks only: the repo reads are patched, the source
selection / filtering / paging runs for real.
"""
import json
from datetime import datetime, timezone

import pytest

from constants import Limits
import feed
from routes.tasks import get_all_tasks


TODAY = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_today(mocker):
    """Pin the day the fan-out walks back from. Without this, any test that asserts
    *which* dates were queried rots silently the moment its hardcoded cursor falls
    out of the SLA window. (CLAUDE.md #14 — time is a boundary, so mock it there.)"""
    mocker.patch.object(feed, 'datetime', mocker.Mock(now=lambda tz=None: TODAY))


def _event(pipeline=None, date=None, status=None, before=None, limit=None):
    q = {}
    for key, value in (("pipeline", pipeline), ("date", date), ("status", status),
                       ("before", before), ("limit", limit)):
        if value:
            q[key] = str(value)
    return {"queryStringParameters": q}


def _task_row(name="extract", date="2026-07-08", started="2026-07-08T10:00:00Z",
              pipeline="sales", status="success"):
    return {"execution_name": f"{name}-{date}-abc", "task_name": name,
            "pipeline_name": pipeline, "status": status, "date": date,
            "started_at": started, "pipeline_execution": f"{pipeline}-{date}-abc"}


def _mixed_rows():
    return [
        _task_row(),
        {"execution_name": "output#sales#extract#2026-07-08", "task_name": "extract",
         "status": "success"},                      # canonical output store — must be skipped
        {"execution_name": "sales-registration", "pipeline_name": "sales"},  # no task_name — skip
    ]


@pytest.fixture
def no_reconcile(mocker):
    """_reconcile_orphaned_tasks describes live SFN executions; not under test here."""
    mocker.patch("routes.tasks._reconcile_orphaned_tasks", side_effect=lambda t: t)


@pytest.fixture
def no_scan(mocker):
    """Nothing in this route may Scan the tokens table any more (see TestSource)."""
    return mocker.patch("routes.tasks.executions_repo.scan",
                        side_effect=AssertionError("get_all_tasks must never Scan"))


def _body(resp):
    return json.loads(resp["body"])


# ──────────────────────────────────────────────────────────────────────────────
# Which read answers which filter (ADR #108)
# ──────────────────────────────────────────────────────────────────────────────

class TestSource:
    def test_no_filter_fans_out_over_the_window(self, mocker, no_scan, no_reconcile):
        """No pipeline to hash on, so `date` is the shard key and the feed walks it —
        one query per day, never a Scan of the whole table."""
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date",
                               return_value=_mixed_rows())
        by_pipeline = mocker.patch("routes.tasks.executions_repo.query_runs_by_pipeline")

        body = _body(get_all_tasks(_event()))

        assert [t["task_name"] for t in body["tasks"]] == ["extract"] * Limits.SLA_DAYS
        assert by_date.call_count == Limits.SLA_DAYS
        by_pipeline.assert_not_called()

    def test_pipeline_filter_uses_the_index_not_a_scan(self, mocker, no_scan, no_reconcile):
        """One pipeline, no date: pipeline-date-index, so the tasks half of History
        reaches as far back as the runs half instead of stopping at the window."""
        by_pipeline = mocker.patch("routes.tasks.executions_repo.query_runs_by_pipeline",
                                   return_value=(_mixed_rows(), None))
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date")

        body = _body(get_all_tasks(_event(pipeline="sales")))

        assert [t["task_name"] for t in body["tasks"]] == ["extract"]
        by_pipeline.assert_called_once()
        assert by_pipeline.call_args.args[0] == "sales"
        by_date.assert_not_called()

    def test_explicit_date_is_one_indexed_query(self, mocker, no_scan, no_reconcile):
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date",
                               return_value=_mixed_rows())
        by_pipeline = mocker.patch("routes.tasks.executions_repo.query_runs_by_pipeline")

        get_all_tasks(_event(date="2026-07-08"))

        by_date.assert_called_once()
        by_pipeline.assert_not_called()

    def test_date_and_pipeline_narrow_the_same_key_condition(self, mocker, no_scan, no_reconcile):
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date",
                               return_value=_mixed_rows())

        get_all_tasks(_event(date="2026-07-08", pipeline="sales"))

        expr = by_date.call_args.kwargs["key_condition"].get_expression()
        assert expr["operator"] == "AND"           # date = D AND pipeline_name = X
        assert [v.get_expression()["values"][1] for v in expr["values"]] == ["2026-07-08", "sales"]


# ──────────────────────────────────────────────────────────────────────────────
# Cursor paging — the contract shared with /api/runs
# ──────────────────────────────────────────────────────────────────────────────

class TestPaging:
    def test_full_page_hands_back_a_cursor(self, mocker, no_scan, no_reconcile):
        rows = [_task_row(name=f"t{i}", started=f"2026-07-08T10:{i:02d}:00Z") for i in range(5)]
        mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=rows)

        body = _body(get_all_tasks(_event(date="2026-07-08", limit=3)))

        assert body["count"] == 3
        assert [t["task_name"] for t in body["tasks"]] == ["t4", "t3", "t2"]
        assert body["next"] == "2026-07-08T10:02:00Z"

    def test_no_cursor_when_nothing_older_exists(self, mocker, no_scan, no_reconcile):
        """`next: null` is the honest "that is all" the old [:limit] slice could not say."""
        mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=[_task_row()])
        body = _body(get_all_tasks(_event(date="2026-07-08", limit=3)))
        assert body["next"] is None

    def test_cursor_serves_the_next_page_without_repeats(self, mocker, no_scan, no_reconcile):
        rows = [_task_row(name=f"t{i}", started=f"2026-07-08T10:{i:02d}:00Z") for i in range(5)]
        mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=rows)

        first = _body(get_all_tasks(_event(date="2026-07-08", limit=3)))
        second = _body(get_all_tasks(_event(date="2026-07-08", limit=3, before=first["next"])))

        assert [t["task_name"] for t in second["tasks"]] == ["t1", "t0"]
        assert second["next"] is None
        assert not {t["task_name"] for t in first["tasks"]} & {t["task_name"] for t in second["tasks"]}

    def test_cursor_starts_the_fan_out_at_its_own_date(self, mocker, no_scan, no_reconcile,
                                                       frozen_today):
        """Paging deeper must not re-query the days it already served — and so costs
        fewer queries, not more."""
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=[])

        get_all_tasks(_event(before="2026-07-14T10:00:00Z"))

        queried = [c.args[0] for c in by_date.call_args_list]
        assert queried[0] == "2026-07-14"           # the cursor's date, not today
        assert "2026-07-15" not in queried          # already served
        assert len(queried) == Limits.SLA_DAYS - 2

    def test_a_cursor_older_than_the_window_queries_nothing(self, mocker, no_scan,
                                                            no_reconcile, frozen_today):
        """The walk never leaves the window, whatever cursor a client sends back."""
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=[])

        body = _body(get_all_tasks(_event(before="2024-01-15T10:00:00Z")))

        assert by_date.call_count == 0
        assert body["next"] is None

    def test_cursor_seeds_the_index_read_for_a_pipeline(self, mocker, no_scan, no_reconcile):
        by_pipeline = mocker.patch("routes.tasks.executions_repo.query_runs_by_pipeline",
                                   return_value=([], None))

        get_all_tasks(_event(pipeline="sales", before="2026-07-08T10:00:00Z"))

        assert by_pipeline.call_args.kwargs["before_date"] == "2026-07-08"


# ──────────────────────────────────────────────────────────────────────────────
# Filters
# ──────────────────────────────────────────────────────────────────────────────

class TestFilters:
    def test_status_is_pushed_into_the_query_when_indexed_by_date(self, mocker, no_scan, no_reconcile):
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=[])

        get_all_tasks(_event(date="2026-07-08", status="failed"))

        expr = by_date.call_args.kwargs["filter_expr"].get_expression()
        assert expr["operator"] == "="
        assert expr["values"][1] == "failed"

    def test_no_status_filter_means_no_filter_expression(self, mocker, no_scan, no_reconcile):
        """DynamoDB rejects an empty/unused expression — omit it entirely."""
        by_date = mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=[])
        get_all_tasks(_event(date="2026-07-08"))
        assert by_date.call_args.kwargs["filter_expr"] is None

    def test_status_is_applied_in_python_on_the_index_path(self, mocker, no_scan, no_reconcile):
        """The index read takes no FilterExpression (its whole-date accounting counts
        returned rows), so the route must filter — or ?status= would silently do nothing."""
        mocker.patch(
            "routes.tasks.executions_repo.query_runs_by_pipeline",
            return_value=([_task_row(name="ok", status="failed"),
                           _task_row(name="nope", status="success")], None),
        )
        body = _body(get_all_tasks(_event(pipeline="sales", status="failed")))
        assert [t["task_name"] for t in body["tasks"]] == ["ok"]

    def test_filters_are_echoed_back(self, mocker, no_scan, no_reconcile):
        mocker.patch("routes.tasks.executions_repo.query_runs_by_date", return_value=[])
        body = _body(get_all_tasks(_event(date="2026-07-08", pipeline="sales", status="failed")))
        assert body["filters"] == {"status": "failed", "date": "2026-07-08", "pipeline": "sales"}


# ──────────────────────────────────────────────────────────────────────────────
# Degradation
# ──────────────────────────────────────────────────────────────────────────────

def test_one_bad_day_does_not_lose_the_feed(mocker, no_scan, no_reconcile):
    """A failing day is logged and skipped — the other 13 still answer (ADR #38)."""
    mocker.patch("routes.tasks.executions_repo.query_runs_by_date",
                 side_effect=[Exception("throttled")] + [[_task_row()]] * (Limits.SLA_DAYS - 1))
    err = mocker.patch("routes.tasks.log.error")

    resp = get_all_tasks(_event())

    assert resp["statusCode"] == 200
    assert _body(resp)["count"] == Limits.SLA_DAYS - 1
    err.assert_called_once()
