"""get_metrics must aggregate ALL of today's tasks, not just one DynamoDB page.

Regression test for a real bug found in a code-review pass: get_metrics called
executions_repo.query_by_date(today, projection=..., expr_names=...) with no
max_items. query_by_date's own implementation only invokes the paginated
query_all() helper when max_items is truthy — omitting it silently returns a
SINGLE DynamoDB query page (whatever fits under DynamoDB's own response-size
cap), not the full day's items. On a busy day where today's tasks exceed one
page, /api/metrics' status_counts and total would silently undercount, with
no error, warning, or truncation flag anywhere in the response.
"""


def _paginated_table(mocker, pages):
    """A fake DynamoDB Table whose .query() returns successive pages,
    exposing LastEvaluatedKey exactly like the real boto3 resource does."""
    table = mocker.MagicMock()
    call_state = {"i": 0}

    def _query(**kwargs):
        i = call_state["i"]
        call_state["i"] += 1
        page = pages[i]
        resp = {"Items": page}
        if i < len(pages) - 1:
            resp["LastEvaluatedKey"] = {"execution_name": f"cursor-{i}"}
        return resp

    table.query.side_effect = _query
    return table


class TestGetMetricsPagination:
    def test_counts_span_multiple_dynamodb_pages(self, mocker):
        """Two pages of today's tasks (simulating a busy day exceeding one
        DynamoDB response page) must both be counted — not just the first."""
        from routes import health

        page1 = [{"execution_name": f"a-{i}", "status": "success"} for i in range(3)]
        page2 = [{"execution_name": f"b-{i}", "status": "failed"} for i in range(2)]
        fake_table = _paginated_table(mocker, [page1, page2])

        mocker.patch.object(
            type(health.executions_repo), "table",
            new_callable=mocker.PropertyMock, return_value=fake_table,
        )

        result = health.get_metrics({})

        assert result["statusCode"] == 200
        import json
        body = json.loads(result["body"])
        # 3 success + 2 failed from BOTH pages, not just page 1's 3 successes.
        assert body["metrics"]["tasks"]["total"] == 5
        assert body["metrics"]["tasks"]["success"] == 3
        assert body["metrics"]["tasks"]["failed"] == 2
        # Proves pagination actually ran (both pages fetched).
        assert fake_table.query.call_count == 2
