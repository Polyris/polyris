"""
Unit tests for dal.api_tokens_repo.ApiTokensRepo (ADR #65).

Pattern follows test_backfills_repo.py — mock the .table property to short-
circuit the dynamodb.Table(...) lookup. No moto, no AWS calls.
"""



def _repo(mocker, table_mock):
    from dal.api_tokens_repo import ApiTokensRepo
    repo = ApiTokensRepo()
    mocker.patch.object(ApiTokensRepo, 'table',
                        new_callable=mocker.PropertyMock,
                        return_value=table_mock)
    return repo


class TestReads:
    def test_get_by_id(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {'Item': {'token_id': 'tok_1', 'name': 'ci'}}
        repo = _repo(mocker, table)
        assert repo.get_by_id('tok_1')['name'] == 'ci'

    def test_get_by_id_missing(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {}
        repo = _repo(mocker, table)
        assert repo.get_by_id('tok_x') is None

    def test_get_by_hash_returns_first(self, mocker):
        table = mocker.MagicMock()
        table.query.return_value = {'Items': [{'token_id': 'tok_1', 'token_hash': 'h'}]}
        repo = _repo(mocker, table)
        rec = repo.get_by_hash('h')
        assert rec['token_id'] == 'tok_1'
        # queried the hash-index, not a scan
        assert table.query.call_args.kwargs['IndexName'] == 'hash-index'

    def test_get_by_hash_none(self, mocker):
        table = mocker.MagicMock()
        table.query.return_value = {'Items': []}
        repo = _repo(mocker, table)
        assert repo.get_by_hash('missing') is None

    def test_list_by_owner_sorted_desc(self, mocker):
        table = mocker.MagicMock()
        table.query.return_value = {'Items': [
            {'token_id': 'a', 'created_at': '2026-01-01T00:00:00'},
            {'token_id': 'b', 'created_at': '2026-02-01T00:00:00'},
        ]}
        repo = _repo(mocker, table)
        out = repo.list_by_owner('user-1')
        assert [t['token_id'] for t in out] == ['b', 'a']  # newest first
        assert table.query.call_args.kwargs['IndexName'] == 'owner-index'


class TestWrites:
    def test_put_sets_defaults(self, mocker):
        table = mocker.MagicMock()
        repo = _repo(mocker, table)
        repo.put({'token_id': 'tok_1', 'token_hash': 'h', 'name': 'x'})
        item = table.put_item.call_args.kwargs['Item']
        assert item['revoked'] is False
        assert 'created_at' in item

    def test_revoke_sets_flag_with_condition(self, mocker):
        table = mocker.MagicMock()
        repo = _repo(mocker, table)
        repo.revoke('tok_1')
        kwargs = table.update_item.call_args.kwargs
        assert kwargs['Key'] == {'token_id': 'tok_1'}
        assert ':t' in kwargs['ExpressionAttributeValues']
        assert kwargs['ExpressionAttributeValues'][':t'] is True
        assert 'attribute_exists' in kwargs['ConditionExpression']

    def test_touch_last_used_swallows_errors(self, mocker):
        from botocore.exceptions import ClientError
        table = mocker.MagicMock()
        table.update_item.side_effect = ClientError({'Error': {'Code': 'X'}}, 'UpdateItem')
        repo = _repo(mocker, table)
        # Must not raise — telemetry write is best-effort.
        repo.touch_last_used('tok_1')

    def test_delete(self, mocker):
        table = mocker.MagicMock()
        repo = _repo(mocker, table)
        repo.delete('tok_1')
        assert table.delete_item.call_args.kwargs['Key'] == {'token_id': 'tok_1'}
