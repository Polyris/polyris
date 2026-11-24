"""get_all_tasks: the Tasks tab (D139, D140).

Without a date it reads what the Runs tab reads, run rows by start day, and then the task
rows of the runs on its page only; a page holds whole runs, and its cursor is the start of
its last run. A date filter reads the task rows of every run date that holds the day.

pytest-mock (ADR #26), boundary mocks only: the repository's reads are faked, the walk,
the filtering and the paging run for real.
"""
import json
from datetime import datetime, timezone
from typing import Dict, List

import pytest

from constants import Limits
import feed
from polyris.coordination import run_row
from routes.tasks import get_all_tasks


TODAY = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_today(mocker):
    """Pin the day the walk of start days begins at (CLAUDE.md #14: time is a boundary, so mock it there)."""
    mocker.patch.object(feed, 'datetime', mocker.Mock(now=lambda tz=None: TODAY))


@pytest.fixture(autouse=True)
def no_scan(mocker):
    """Nothing in this route may Scan the tokens table."""
    return mocker.patch("routes.tasks.executions_repo.discover_pipeline_names",
                        side_effect=AssertionError("get_all_tasks must never Scan"))


def _event(pipeline=None, date=None, status=None, before=None, limit=None):
    q = {}
    for key, value in (("pipeline", pipeline), ("date", date), ("status", status),
                       ("before", before), ("limit", limit)):
        if value:
            q[key] = str(value)
    return {"queryStringParameters": q}


def _body(resp):
    return json.loads(resp["body"])


def _task_row(name="extract", run="sales-run-2026-07-16-aaaaaaaa", date="2026-07-16",
              started="2026-07-16T10:00:05Z", pipeline="sales", status="success"):
    return {"execution_name": f"{name}-{date}-{run[-8:]}", "task_name": name, "pipeline_name": pipeline,
            "status": status, "date": date, "started_at": started, "pipeline_execution": run}


class World:
    """Run rows by start day, as run-start-index answers, and each run's task rows."""

    def __init__(self):
        self.runs: List[Dict] = []
        self.tasks: Dict[str, List[Dict]] = {}
        self.read_tasks_of: List[str] = []
        self.days_read: List[str] = []

    def run(self, execution, started, *, pipeline="sales", date=None, tasks=("extract", "load"), statuses=None,
            summary=True, stamped=None):
        """A run that started at ``started``; its task rows are stamped at ``stamped`` (Start_Run, seconds later)."""
        statuses = statuses or {name: "success" for name in tasks}
        date = date or started[:10]
        item = {run_row.TABLE_KEY: run_row.key(execution), run_row.STARTED_AT: started,
                run_row.START_DAY: started[:10], run_row.PIPELINE: pipeline, run_row.RUN_DATE: date}
        if summary:
            counts: Dict[str, int] = {}
            for status in statuses.values():
                counts[status] = counts.get(status, 0) + 1
            item[run_row.SUMMARY] = counts
        self.runs.append(item)
        self.tasks[execution] = [_task_row(name, execution, date, stamped or started, pipeline, statuses[name])
                                 for name in tasks]
        return item

    def runs_started(self, start_day, *, before=None, pipeline=None, run_dates=None, want=None):
        self.days_read.append(start_day)
        rows = [r for r in self.runs if r[run_row.START_DAY] == start_day
                and (not before or r[run_row.STARTED_AT] < before)
                and (not pipeline or r[run_row.PIPELINE] == pipeline)]
        return sorted(rows, key=lambda r: r[run_row.STARTED_AT], reverse=True)

    def query_by_pipeline_execution(self, execution, **_):
        self.read_tasks_of.append(execution)
        return list(self.tasks.get(execution, []))


@pytest.fixture
def world(mocker):
    w = World()
    mocker.patch("routes.tasks.executions_repo.runs_started", side_effect=w.runs_started)
    mocker.patch("routes.tasks.executions_repo.query_by_pipeline_execution", side_effect=w.query_by_pipeline_execution)
    mocker.patch("routes.tasks.executions_repo.query_runs_by_date",
                 side_effect=AssertionError("without a date the tab reads run rows, never a date's task rows"))
    return w


def _names(body):
    return [(t["pipeline_execution"][-1], t["task_name"]) for t in body["tasks"]]


# ── Without a date: run rows first, then the tasks of the page's runs (D140) ──────


class TestRunsFirst:
    def test_a_page_holds_whole_runs_newest_first_and_reads_only_their_tasks(self, world):
        world.run("sales-run-a1", "2026-07-16T08:00:00Z")
        world.run("sales-run-b2", "2026-07-16T09:00:00Z")
        world.run("sales-run-c3", "2026-07-16T10:00:00Z")

        body = _body(get_all_tasks(_event(limit=3)))

        assert _names(body) == [("3", "extract"), ("3", "load"), ("2", "extract"), ("2", "load")]
        assert body["next"] == "2026-07-16T09:00:00Z"         # the start of the page's last run
        assert world.read_tasks_of == ["sales-run-c3", "sales-run-b2"]

    def test_the_next_page_reads_the_runs_that_started_before_the_cursor(self, world):
        world.run("sales-run-a1", "2026-07-15T08:00:00Z")
        world.run("sales-run-b2", "2026-07-16T09:00:00Z")
        world.run("sales-run-c3", "2026-07-16T10:00:00Z")

        first = _body(get_all_tasks(_event(limit=2)))
        second = _body(get_all_tasks(_event(limit=2, before=first["next"])))
        third = _body(get_all_tasks(_event(limit=2, before=second["next"])))

        assert [_names(page) for page in (first, second, third)] == [
            [("3", "extract"), ("3", "load")], [("2", "extract"), ("2", "load")], [("1", "extract"), ("1", "load")]]
        assert third["next"] is None                          # the window ended: nothing older exists

    def test_a_run_whose_tasks_were_stamped_after_a_newer_runs_is_not_skipped(self, world):
        """Start_Run stamps a run's tasks seconds after the run started: two runs that started close together sort
        one way by their starts and the other by their tasks. Paged on the tasks, the older run was skipped."""
        world.run("sales-run-a1", "2026-07-16T10:00:00Z", stamped="2026-07-16T10:00:09Z")
        world.run("sales-run-b2", "2026-07-16T10:00:05Z", stamped="2026-07-16T10:00:06Z")

        first = _body(get_all_tasks(_event(limit=2)))
        second = _body(get_all_tasks(_event(limit=2, before=first["next"])))

        assert _names(first) + _names(second) == [("2", "extract"), ("2", "load"), ("1", "extract"), ("1", "load")]

    def test_runs_that_started_at_the_same_instant_go_on_one_page(self, world):
        world.run("sales-run-a1", "2026-07-16T10:00:00Z", pipeline="sales")
        world.run("orders-run-b2", "2026-07-16T10:00:00Z", pipeline="orders")

        body = _body(get_all_tasks(_event(limit=1)))

        assert len(body["tasks"]) == 4 and body["next"] in (None, "2026-07-16T10:00:00Z")
        if body["next"]:
            assert _body(get_all_tasks(_event(limit=1, before=body["next"])))["tasks"] == []

    def test_an_hourly_pipelines_runs_page_through_every_run(self, world):
        """A run's date takes the form of its granularity: a cursor cut to a day showed one hourly run and said that
        was all (TR-10, D-1). Run rows carry every form."""
        for hour in ("08", "09", "10"):
            world.run(f"hourly-run-{hour}", f"2026-07-16T{hour}:00:00Z", pipeline="hourly",
                      date=f"2026-07-16T{hour}")
        seen: List = []
        before = None
        for _ in range(4):
            body = _body(get_all_tasks(_event(pipeline="hourly", limit=1, before=before)))
            seen += [t["date"] for t in body["tasks"]]
            before = body["next"]
            if before is None:
                break
        assert seen == ["2026-07-16T10"] * 2 + ["2026-07-16T09"] * 2 + ["2026-07-16T08"] * 2

    def test_a_pipeline_filter_reads_that_pipelines_runs(self, world):
        world.run("sales-run-a1", "2026-07-16T08:00:00Z", pipeline="sales")
        world.run("orders-run-b2", "2026-07-16T09:00:00Z", pipeline="orders")

        body = _body(get_all_tasks(_event(pipeline="sales")))

        assert {t["pipeline_name"] for t in body["tasks"]} == {"sales"}
        assert world.read_tasks_of == ["sales-run-a1"]

    def test_the_walk_stops_once_the_page_is_full(self, world):
        world.run("sales-run-c3", "2026-07-16T10:00:00Z",
                  tasks=tuple(f"t{i:03d}" for i in range(Limits.TASKS_FEED_LIMIT + 1)))
        world.run("sales-run-a1", "2026-06-01T10:00:00Z")
        body = _body(get_all_tasks(_event()))

        assert len(body["tasks"]) == Limits.TASKS_FEED_LIMIT + 1 and body["next"] == "2026-07-16T10:00:00Z"
        assert world.days_read == ["2026-07-16"]             # one start day, not the whole window

    def test_a_task_row_is_shown_and_no_other_row_of_the_run(self, world):
        world.run("sales-run-a1", "2026-07-16T08:00:00Z", tasks=("extract",))
        world.tasks["sales-run-a1"] += [
            {"execution_name": "output#sales#extract#2026-07-16", "task_name": "extract", "status": "success",
             "pipeline_execution": "sales-run-a1"},                          # the output store
            {"execution_name": "sales-registration", "pipeline_name": "sales", "pipeline_execution": "sales-run-a1"},
        ]
        assert _names(_body(get_all_tasks(_event()))) == [("1", "extract")]


# ── A status: only the runs whose summary has it are read (D139) ───────────────


class TestStatus:
    def test_a_run_whose_summary_has_no_row_of_the_status_is_not_read(self, world):
        world.run("sales-run-a1", "2026-07-16T08:00:00Z", statuses={"extract": "failed", "load": "upstream_failed"})
        world.run("sales-run-b2", "2026-07-16T09:00:00Z")                      # all success

        body = _body(get_all_tasks(_event(status="failed")))

        assert _names(body) == [("1", "extract")]
        assert world.read_tasks_of == ["sales-run-a1"]

    def test_a_status_no_run_has_reads_no_task_row(self, world):
        """A rare status read the whole history of a pipeline's task rows, past API Gateway's 30 seconds once a
        year of history was kept (TR-10, B-1)."""
        for day in range(1, 16):
            world.run(f"sales-run-{day:02d}", f"2026-07-{day:02d}T08:00:00Z")

        body = _body(get_all_tasks(_event(pipeline="sales", status="failed")))

        assert body["tasks"] == [] and body["next"] is None and world.read_tasks_of == []

    def test_a_run_without_a_summary_yet_is_read_to_find_out(self, world):
        world.run("sales-run-a1", "2026-07-16T08:00:00Z", statuses={"extract": "failed", "load": "waiting"},
                  summary=False)
        assert _names(_body(get_all_tasks(_event(status="failed")))) == [("1", "extract")]

    def test_filters_are_echoed_back(self, world):
        body = _body(get_all_tasks(_event(pipeline="sales", status="failed")))
        assert body["filters"] == {"status": "failed", "date": None, "pipeline": "sales"}


# ── A date: the task rows of every run date that holds the day (D140) ──────────


class TestDate:
    @pytest.fixture
    def by_date(self, mocker):
        rows = {"2026-07-16": [_task_row("daily", date="2026-07-16")],
                "2026-07-16T05": [_task_row("hourly", date="2026-07-16T05", started="2026-07-16T05:00:03Z")],
                "2026-W29": [_task_row("weekly", date="2026-W29", started="2026-07-13T00:00:03Z")],
                "2026-07": [_task_row("monthly", date="2026-07", started="2026-07-01T00:00:03Z")]}
        mocker.patch("routes.tasks.executions_repo.runs_started",
                     side_effect=AssertionError("a date reads that date's task rows, no window"))
        return mocker.patch("routes.tasks.executions_repo.query_runs_by_date",
                            side_effect=lambda date, **_: rows.get(date, []))

    def test_a_day_finds_the_runs_of_every_granularity_that_holds_it(self, by_date):
        body = _body(get_all_tasks(_event(date="2026-07-16")))

        assert sorted(t["task_name"] for t in body["tasks"]) == ["daily", "hourly", "monthly", "weekly"]
        assert {c.args[0] for c in by_date.call_args_list} >= {"2026-07-16", "2026-07-16T00", "2026-07-16T23",
                                                               "2026-W29", "2026-07"}
        assert by_date.call_count == 27                     # the day, its 24 hours, its week and its month

    def test_another_key_finds_itself_alone(self, by_date):
        body = _body(get_all_tasks(_event(date="2026-07-16T05")))
        assert [t["task_name"] for t in body["tasks"]] == ["hourly"] and by_date.call_count == 1

    def test_a_pipeline_and_a_status_narrow_each_query(self, by_date):
        get_all_tasks(_event(date="2026-07-16", pipeline="sales", status="failed"))
        call = by_date.call_args_list[0].kwargs
        expr = call["key_condition"].get_expression()
        assert expr["operator"] == "AND"
        assert [v.get_expression()["values"][1] for v in expr["values"]] == ["2026-07-16T00", "sales"]
        assert call["filter_expr"].get_expression()["values"][1] == "failed" and call["min_rows"] is None


# ── Failures ────────────────────────────────────────────────────────────────


def test_a_failed_read_fails_the_request_rather_than_answer_part_of_the_list(mocker):
    """A day that could not be read was skipped, and the list looked whole (CLAUDE.md: a list says it is complete
    only when its read reached the end)."""
    mocker.patch("routes.tasks.executions_repo.runs_started", side_effect=RuntimeError("throttled"))
    assert get_all_tasks(_event())["statusCode"] == 500


def test_an_index_still_being_built_answers_as_the_runs_tab_does(mocker):
    from dal import executions_repo
    mocker.patch("routes.tasks.executions_repo.runs_started", side_effect=executions_repo.IndexBuilding("building"))
    resp = get_all_tasks(_event())
    assert resp["statusCode"] == 503 and _body(resp)["code"] == "INDEX_BUILDING"
