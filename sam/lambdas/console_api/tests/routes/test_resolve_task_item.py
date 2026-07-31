"""Tests for routes.tasks.resolve_task_item's own pagination logic.

Every OTHER test in this suite mocks resolve_task_item at the call site
(e.g. tests/routes/test_task_output.py) — none exercised this function's
own GSI-pagination correctness. That gap is exactly where a real bug was
found in a code-review pass: the pagination loop broke as soon as ANY page
yielded FilterExpression-matched items, on the mistaken assumption that a
per-page filter match meant no further pages needed checking. Since the
`date` partition spans every pipeline's tasks that day (not just one
task_name), a task_name match can land on any page — including a MORE
RECENT retry/restart attempt landing on a LATER page than an older attempt.
The early exit meant `resolve_task_item` could silently resolve to an OLDER
execution while a newer one sat unexamined — and every task action route
(retry/skip/fail/mark_success/stop) depends on this function to pick the
right execution to mutate.
"""
from routes.tasks import resolve_task_item


def _page(items, last_key=None):
    resp = {"Items": items}
    if last_key is not None:
        resp["LastEvaluatedKey"] = last_key
    return resp


class TestResolveTaskItemPagination:
    def test_direct_execution_name_lookup_short_circuits_gsi(self, mocker):
        """A full execution_name resolves via direct get(), no GSI query at all."""
        mocker.patch("routes.tasks.executions_repo.get",
                      return_value={"execution_name": "extract-2024-01-15-abc12345"})
        query_mock = mocker.patch("routes.tasks.executions_repo.query_by_date_raw")

        item, execution_name = resolve_task_item("extract-2024-01-15-abc12345", "2024-01-15")

        assert execution_name == "extract-2024-01-15-abc12345"
        query_mock.assert_not_called()

    def test_more_recent_match_on_a_later_page_is_found_not_missed(self, mocker):
        """Regression test for the exact bug: an older attempt on page 1 must
        not hide a newer attempt on page 2."""
        page1 = _page(
            [{"execution_name": "extract-2024-01-15-OLDATTEMPT",
              "started_at": "2024-01-15T08:00:00Z", "task_name": "extract",
              "pipeline_execution": "run1"}],
            last_key={"date": "2024-01-15", "execution_name": "cursor1"},
        )
        page2 = _page(
            [{"execution_name": "extract-2024-01-15-NEWATTEMPT",
              "started_at": "2024-01-15T14:00:00Z", "task_name": "extract",
              "pipeline_execution": "run1"}],
        )
        calls = {"n": 0}

        def fake_query(**kwargs):
            calls["n"] += 1
            return page1 if calls["n"] == 1 else page2

        get_results = {
            "extract-2024-01-15-OLDATTEMPT": {"execution_name": "extract-2024-01-15-OLDATTEMPT", "status": "failed"},
            "extract-2024-01-15-NEWATTEMPT": {"execution_name": "extract-2024-01-15-NEWATTEMPT", "status": "running"},
        }
        mocker.patch("routes.tasks.executions_repo.query_by_date_raw", side_effect=fake_query)
        mocker.patch("routes.tasks.executions_repo.get", side_effect=lambda name: get_results.get(name))

        item, execution_name = resolve_task_item("extract", "2024-01-15", "run1")

        assert execution_name == "extract-2024-01-15-NEWATTEMPT"
        assert calls["n"] == 2  # both pages were actually checked

    def test_match_only_on_a_later_page_is_still_found(self, mocker):
        """A page with NO matches (empty after filter) must not stop
        pagination — the match may simply be further along."""
        page1 = _page([], last_key={"date": "2024-01-15", "execution_name": "cursor1"})
        page2 = _page([{"execution_name": "extract-2024-01-15-onlymatch",
                         "started_at": "2024-01-15T14:00:00Z", "task_name": "extract",
                         "pipeline_execution": "run1"}])
        calls = {"n": 0}

        def fake_query(**kwargs):
            calls["n"] += 1
            return page1 if calls["n"] == 1 else page2

        mocker.patch("routes.tasks.executions_repo.query_by_date_raw", side_effect=fake_query)
        mocker.patch("routes.tasks.executions_repo.get",
                      return_value={"execution_name": "extract-2024-01-15-onlymatch"})

        item, execution_name = resolve_task_item("extract", "2024-01-15", "run1")

        assert execution_name == "extract-2024-01-15-onlymatch"
        assert calls["n"] == 2

    def test_no_match_across_all_pages_returns_none(self, mocker):
        page1 = _page([])  # no LastEvaluatedKey -> exhausted after one page
        mocker.patch("routes.tasks.executions_repo.query_by_date_raw", return_value=page1)
        mocker.patch("routes.tasks.executions_repo.get", return_value=None)

        item, execution_name = resolve_task_item("nope", "2024-01-15")

        assert item is None
        assert execution_name is None

    def test_pagination_stops_at_max_pages_safety_limit(self, mocker):
        """Even with an endless LastEvaluatedKey, the loop must terminate."""
        def fake_query(**kwargs):
            return _page([], last_key={"date": "2024-01-15", "execution_name": "forever"})

        query_mock = mocker.patch("routes.tasks.executions_repo.query_by_date_raw", side_effect=fake_query)
        mocker.patch("routes.tasks.executions_repo.get", return_value=None)

        item, execution_name = resolve_task_item("nope", "2024-01-15")

        assert item is None
        assert query_mock.call_count == 10  # max_pages
