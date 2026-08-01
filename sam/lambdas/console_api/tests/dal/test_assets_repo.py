"""
Unit tests for dal.assets_repo.AssetEventsRepo.

Focus on delete_by_asset (full pagination) and list_asset_names
(scan with projection across all pages) — both rewritten in v0.70.18
to use the shared scan_all/query_all helpers and avoid silent truncation.
"""



# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_repo_with_table(mocker, table_mock):
    """Build an AssetEventsRepo whose .table property returns our mock."""
    from dal.assets_repo import AssetEventsRepo
    repo = AssetEventsRepo()
    # Patch the .table property to short-circuit dynamodb.Table(...) lookup.
    mocker.patch.object(
        AssetEventsRepo, 'table',
        new_callable=mocker.PropertyMock,
        return_value=table_mock,
    )
    return repo


def _query_pages(*pages):
    """Build a side_effect that returns successive query/scan response dicts.

    Each `page` is a list of items; LastEvaluatedKey is auto-set on every
    page except the last to mimic real DDB pagination.
    """
    responses = []
    for i, items in enumerate(pages):
        resp = {'Items': items}
        if i < len(pages) - 1:
            # Use the last item's PK as a synthetic continuation key.
            resp['LastEvaluatedKey'] = {'asset_name': f'__page_{i}__'}
        responses.append(resp)
    return responses


# ──────────────────────────────────────────────────────────────────────────────
# delete_by_asset
# ──────────────────────────────────────────────────────────────────────────────

class TestDeleteByAsset:
    """Tests for AssetEventsRepo.delete_by_asset()."""

    def test_no_events_returns_zero(self, mocker):
        table = mocker.MagicMock()
        table.query.side_effect = _query_pages([])
        # batch_writer is a context manager
        batch_ctx = mocker.MagicMock()
        table.batch_writer.return_value.__enter__.return_value = batch_ctx

        repo = _make_repo_with_table(mocker, table)
        count = repo.delete_by_asset('missing/asset')

        assert count == 0
        batch_ctx.delete_item.assert_not_called()

    def test_single_page_deletes_all(self, mocker):
        items = [
            {'asset_name': 'retail/orders', 'event_time': f'2026-05-01T00:0{i}:00Z'}
            for i in range(5)
        ]
        table = mocker.MagicMock()
        table.query.side_effect = _query_pages(items)
        batch_ctx = mocker.MagicMock()
        table.batch_writer.return_value.__enter__.return_value = batch_ctx

        repo = _make_repo_with_table(mocker, table)
        count = repo.delete_by_asset('retail/orders')

        assert count == 5
        assert batch_ctx.delete_item.call_count == 5
        # Verify keys passed are correct shape
        for call in batch_ctx.delete_item.call_args_list:
            key = call.kwargs['Key']
            assert key['asset_name'] == 'retail/orders'
            assert 'event_time' in key

    def test_multi_page_paginates_fully(self, mocker):
        """Regression: pre-v0.70.18 truncated at 1000.

        Three pages of 600 events each (1800 total) must all be deleted.
        """
        page1 = [{'asset_name': 'retail/orders', 'event_time': f't1-{i}'} for i in range(600)]
        page2 = [{'asset_name': 'retail/orders', 'event_time': f't2-{i}'} for i in range(600)]
        page3 = [{'asset_name': 'retail/orders', 'event_time': f't3-{i}'} for i in range(600)]

        table = mocker.MagicMock()
        table.query.side_effect = _query_pages(page1, page2, page3)
        batch_ctx = mocker.MagicMock()
        table.batch_writer.return_value.__enter__.return_value = batch_ctx

        repo = _make_repo_with_table(mocker, table)
        count = repo.delete_by_asset('retail/orders')

        assert count == 1800
        assert batch_ctx.delete_item.call_count == 1800
        # Verify pagination: 3 query calls (one per page)
        assert table.query.call_count == 3

    def test_query_uses_correct_key_condition(self, mocker):
        table = mocker.MagicMock()
        table.query.side_effect = _query_pages([])
        batch_ctx = mocker.MagicMock()
        table.batch_writer.return_value.__enter__.return_value = batch_ctx

        repo = _make_repo_with_table(mocker, table)
        repo.delete_by_asset('foo/bar')

        # Inspect the query call: KeyConditionExpression must filter by asset_name
        kwargs = table.query.call_args.kwargs
        # boto3 Key().eq() is a ConditionBase; just check it's present
        assert 'KeyConditionExpression' in kwargs
        # Ascending scan (oldest-first) — chosen for batch_writer ordering predictability
        assert kwargs['ScanIndexForward'] is True


# ──────────────────────────────────────────────────────────────────────────────
# list_asset_names
# ──────────────────────────────────────────────────────────────────────────────

class TestListAssetNames:
    """Tests for AssetEventsRepo.list_asset_names()."""

    def test_empty_table_returns_empty_list(self, mocker):
        table = mocker.MagicMock()
        table.scan.side_effect = _query_pages([])

        repo = _make_repo_with_table(mocker, table)
        names = repo.list_asset_names()

        assert names == []

    def test_dedupes_repeated_asset_names(self, mocker):
        """Multiple events for same asset → one unique name."""
        items = [
            {'asset_name': 'retail/orders'},
            {'asset_name': 'retail/orders'},
            {'asset_name': 'retail/orders'},
            {'asset_name': 'retail/customers'},
        ]
        table = mocker.MagicMock()
        table.scan.side_effect = _query_pages(items)

        repo = _make_repo_with_table(mocker, table)
        names = repo.list_asset_names()

        assert sorted(names) == ['retail/customers', 'retail/orders']

    def test_paginates_across_multiple_pages(self, mocker):
        """Regression: pre-v0.70.18 stopped early when len(names) >= max_items.

        With 600 unique assets across 2 pages of 300 each, we must get all 600.
        """
        page1 = [{'asset_name': f'group_a/asset_{i}'} for i in range(300)]
        page2 = [{'asset_name': f'group_b/asset_{i}'} for i in range(300)]

        table = mocker.MagicMock()
        table.scan.side_effect = _query_pages(page1, page2)

        repo = _make_repo_with_table(mocker, table)
        names = repo.list_asset_names(max_items=10000)

        assert len(names) == 600
        # Verify pagination happened
        assert table.scan.call_count == 2

    def test_discards_empty_strings(self, mocker):
        items = [
            {'asset_name': 'real_asset'},
            {'asset_name': ''},
            {},  # missing asset_name → defaults to ''
            {'asset_name': 'another_real'},
        ]
        table = mocker.MagicMock()
        table.scan.side_effect = _query_pages(items)

        repo = _make_repo_with_table(mocker, table)
        names = repo.list_asset_names()

        assert sorted(names) == ['another_real', 'real_asset']

    def test_uses_projection_expression(self, mocker):
        """Verify scan uses ProjectionExpression to keep payload small."""
        table = mocker.MagicMock()
        table.scan.side_effect = _query_pages([])

        repo = _make_repo_with_table(mocker, table)
        repo.list_asset_names()

        kwargs = table.scan.call_args.kwargs
        assert kwargs.get('ProjectionExpression') == 'asset_name'

    def test_respects_max_items_safety_cap(self, mocker):
        """If max_items is hit, scan stops and returns what it has.

        scan_all logs a warning when it hits the cap (see utils.scan_all).
        """
        # 5 pages of 100 items each = 500 total, but cap at 300.
        pages = [
            [{'asset_name': f'page{p}/asset_{i}'} for i in range(100)]
            for p in range(5)
        ]
        table = mocker.MagicMock()
        table.scan.side_effect = _query_pages(*pages)

        repo = _make_repo_with_table(mocker, table)
        names = repo.list_asset_names(max_items=300)

        # Cap is on items scanned, not unique names. Since names are all unique,
        # we should see exactly 300 (cap), not 500 (full).
        assert len(names) == 300
