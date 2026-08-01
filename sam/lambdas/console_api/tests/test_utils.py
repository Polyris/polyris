"""
Unit tests for console_api.utils module.
Tests utility functions that are used across all route modules.
"""

from decimal import Decimal


class TestIsExecutionName:
    """Tests for is_execution_name() function."""
    
    def test_valid_execution_name(self):
        from console_api.utils import is_execution_name
        assert is_execution_name('extract-2024-01-15-abc123xy') is True
    
    def test_valid_execution_name_long_task(self):
        from console_api.utils import is_execution_name
        assert is_execution_name('my-long-task-name-2024-01-15-abc123xy') is True
    
    def test_task_name_with_date_in_name(self):
        from console_api.utils import is_execution_name
        assert is_execution_name('daily-2024-report-2024-01-15-abc123xy') is True
    
    def test_plain_task_name(self):
        from console_api.utils import is_execution_name
        assert is_execution_name('extract') is False
    
    def test_no_short_id(self):
        from console_api.utils import is_execution_name
        assert is_execution_name('extract-2024-01-15') is False
    
    def test_short_id_too_short(self):
        from console_api.utils import is_execution_name
        assert is_execution_name('extract-2024-01-15-abc') is False


class TestSafeInt:
    """Tests for safe_int() function."""
    
    def test_decimal_conversion(self):
        from console_api.utils import safe_int
        assert safe_int(Decimal('42')) == 42
    
    def test_int_passthrough(self):
        from console_api.utils import safe_int
        assert safe_int(42) == 42
    
    def test_float_truncation(self):
        from console_api.utils import safe_int
        assert safe_int(42.9) == 42
    
    def test_string_conversion(self):
        from console_api.utils import safe_int
        assert safe_int('42') == 42
    
    def test_none_returns_default(self):
        from console_api.utils import safe_int
        assert safe_int(None) == 0
    
    def test_empty_string_returns_default(self):
        from console_api.utils import safe_int
        assert safe_int('') == 0
    
    def test_invalid_string_returns_default(self):
        from console_api.utils import safe_int
        assert safe_int('abc', default=-1) == -1


class TestSafeParamInt:
    """Tests for safe_param_int() function."""
    
    def test_valid_param(self):
        from console_api.utils import safe_param_int
        assert safe_param_int({'limit': '10'}, 'limit', 5) == 10
    
    def test_missing_param_returns_default(self):
        from console_api.utils import safe_param_int
        assert safe_param_int({}, 'limit', 5) == 5
    
    def test_none_params_returns_default(self):
        from console_api.utils import safe_param_int
        assert safe_param_int(None, 'limit', 5) == 5
    
    def test_max_val_capping(self):
        from console_api.utils import safe_param_int
        assert safe_param_int({'limit': '1000'}, 'limit', 5, max_val=100) == 100
    
    def test_invalid_param_returns_default(self):
        from console_api.utils import safe_param_int
        assert safe_param_int({'limit': 'abc'}, 'limit', 5) == 5


class TestParseWaitBefore:
    """Tests for parse_wait_before() function."""
    
    def test_none_returns_zero(self):
        from console_api.utils import parse_wait_before
        assert parse_wait_before(None) == 0
    
    def test_decimal_conversion(self):
        from console_api.utils import parse_wait_before
        assert parse_wait_before(Decimal('300')) == 300
    
    def test_int_passthrough(self):
        from console_api.utils import parse_wait_before
        assert parse_wait_before(300) == 300
    
    def test_float_truncation(self):
        from console_api.utils import parse_wait_before
        assert parse_wait_before(300.7) == 300
    
    def test_string_conversion(self):
        from console_api.utils import parse_wait_before
        assert parse_wait_before('300') == 300
    
    def test_empty_string_returns_zero(self):
        from console_api.utils import parse_wait_before
        assert parse_wait_before('') == 0


class TestResponseHelpers:
    """Tests for response helper functions."""
    
    def test_cors_response_structure(self):
        from console_api.response import cors_response
        
        response = cors_response(200, {'key': 'value'})
        
        assert response['statusCode'] == 200
        assert 'Access-Control-Allow-Origin' in response['headers']
        assert response['headers']['Content-Type'] == 'application/json'
    
    def test_cors_response_serializes_dates(self):
        from console_api.response import cors_response
        from datetime import datetime, timezone
        
        data = {'created': datetime(2024, 1, 15, tzinfo=timezone.utc)}
        response = cors_response(200, data)
        
        assert response['statusCode'] == 200
        assert '2024-01-15' in response['body']
    
    def test_html_response_success(self):
        from console_api.response import html_response
        
        response = html_response(200, 'Success', 'Task completed')
        
        assert response['statusCode'] == 200
        assert 'text/html' in response['headers']['Content-Type']
        assert 'Task completed' in response['body']
    
    def test_html_response_error(self):
        from console_api.response import html_response
        
        response = html_response(400, 'Error', 'Task failed', icon='❌', success=False)
        
        assert response['statusCode'] == 400
        assert '#fef2f2' in response['body']  # Red background
        assert '❌' in response['body']


class TestQueryAll:
    """Tests for query_all() function."""
    
    def test_query_all_single_page(self, mocker):
        """Single page of results should return all items."""
        from console_api.utils import query_all
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [{'id': '1'}, {'id': '2'}],
            'LastEvaluatedKey': None
        }
        
        result = query_all(mock_table, KeyConditionExpression='test')
        
        assert len(result) == 2
        assert result[0]['id'] == '1'
        mock_table.query.assert_called_once()
    
    def test_query_all_multiple_pages(self, mocker):
        """Multiple pages should be fetched and combined."""
        from console_api.utils import query_all
        
        mock_table = mocker.MagicMock()
        mock_table.query.side_effect = [
            {'Items': [{'id': '1'}], 'LastEvaluatedKey': {'pk': 'next'}},
            {'Items': [{'id': '2'}], 'LastEvaluatedKey': None}
        ]
        
        result = query_all(mock_table, KeyConditionExpression='test')
        
        assert len(result) == 2
        assert mock_table.query.call_count == 2
    
    def test_query_all_respects_max_items(self, mocker):
        """Should stop fetching when max_items is reached."""
        from console_api.utils import query_all
        
        mock_table = mocker.MagicMock()
        mock_table.query.side_effect = [
            {'Items': [{'id': '1'}, {'id': '2'}], 'LastEvaluatedKey': {'pk': 'next'}},
            {'Items': [{'id': '3'}], 'LastEvaluatedKey': None}
        ]
        
        result = query_all(mock_table, max_items=2, KeyConditionExpression='test')
        
        assert len(result) == 2
        mock_table.query.assert_called_once()


class TestScanAll:
    """Tests for scan_all() function."""
    
    def test_scan_all_single_page(self, mocker):
        """Single page of results should return all items."""
        from console_api.utils import scan_all
        
        mock_table = mocker.MagicMock()
        mock_table.scan.return_value = {
            'Items': [{'id': '1'}, {'id': '2'}],
            'LastEvaluatedKey': None
        }
        
        result = scan_all(mock_table)
        
        assert len(result) == 2
        mock_table.scan.assert_called_once()
    
    def test_scan_all_respects_max_items(self, mocker):
        """Should stop fetching when max_items is reached."""
        from console_api.utils import scan_all
        
        mock_table = mocker.MagicMock()
        mock_table.scan.side_effect = [
            {'Items': [{'id': '1'}, {'id': '2'}], 'LastEvaluatedKey': {'pk': 'next'}},
            {'Items': [{'id': '3'}], 'LastEvaluatedKey': None}
        ]
        
        result = scan_all(mock_table, max_items=2)
        
        assert len(result) == 2
        mock_table.scan.assert_called_once()


class TestRecordManualDecision:
    """Tests for record_manual_decision utility."""
    
    def test_emits_task_finished_before_manual_decision_when_error(self, mocker):
        """When task has error, TASK_FINISHED(failed) should be emitted before MANUAL_DECISION."""
        from console_api.utils import record_manual_decision

        # Mock the DAL repo's put method — utils.py now goes through task_events_repo
        # instead of touching dynamodb directly (CLAUDE.md Principle #2).
        mock_put = mocker.patch('console_api.utils.task_events_repo.put')

        item = {
            'task_run_id': 'extract-2024-01-15-abc123',
            'parent_execution_id': 'pipeline-2024-01-15-abc123',
            'task_name': 'extract',
            'error': 'Lambda timeout after 300s'
        }

        record_manual_decision('extract-2024-01-15-abc123', 'skip', 'Skipped by operator', item)

        # Should have 2 put calls: TASK_FINISHED + MANUAL_DECISION
        assert mock_put.call_count == 2

        # First call: TASK_FINISHED
        first_item = mock_put.call_args_list[0][0][0]
        assert first_item['event_type'] == 'TASK_FINISHED'
        assert first_item['status'] == 'failed'
        assert 'Lambda timeout' in first_item['error_summary']
        assert '#25#' in first_item['event_time']

        # Second call: MANUAL_DECISION
        second_item = mock_put.call_args_list[1][0][0]
        assert second_item['event_type'] == 'MANUAL_DECISION'
        assert second_item['decision'] == 'skip'
        assert '#30#' in second_item['event_time']
    
    def test_no_task_finished_when_no_error(self, mocker):
        """When task has no error, only MANUAL_DECISION should be emitted."""
        from console_api.utils import record_manual_decision

        mock_put = mocker.patch('console_api.utils.task_events_repo.put')

        item = {
            'task_run_id': 'extract-2024-01-15-abc123',
            'parent_execution_id': 'pipeline-2024-01-15-abc123',
            'task_name': 'extract',
            'error': ''  # No error
        }

        record_manual_decision('extract-2024-01-15-abc123', 'stop', '', item)

        # Should have only 1 put call: MANUAL_DECISION
        assert mock_put.call_count == 1
        call_item = mock_put.call_args[0][0]
        assert call_item['event_type'] == 'MANUAL_DECISION'
    
    def test_error_truncated_to_500_chars(self, mocker):
        """Long errors should be truncated to 500 chars in error_summary."""
        from console_api.utils import record_manual_decision

        mock_put = mocker.patch('console_api.utils.task_events_repo.put')

        item = {
            'task_run_id': 'extract-2024-01-15-abc123',
            'parent_execution_id': 'pipeline-2024-01-15-abc123',
            'task_name': 'extract',
            'error': 'x' * 1000  # 1000 chars
        }

        record_manual_decision('extract-2024-01-15-abc123', 'skip', '', item)

        first_item = mock_put.call_args_list[0][0][0]
        assert len(first_item['error_summary']) == 503  # 500 + '...'
        assert first_item['error_summary'].endswith('...')
    
    def test_event_time_ordering(self, mocker):
        """TASK_FINISHED (#25) should sort before MANUAL_DECISION (#30)."""
        from console_api.utils import record_manual_decision

        mock_put = mocker.patch('console_api.utils.task_events_repo.put')

        item = {
            'task_run_id': 'extract-2024-01-15-abc123',
            'parent_execution_id': 'pipeline-2024-01-15-abc123',
            'task_name': 'extract',
            'error': 'Some error'
        }

        record_manual_decision('extract-2024-01-15-abc123', 'skip', '', item)

        finished_time = mock_put.call_args_list[0][0][0]['event_time']
        decision_time = mock_put.call_args_list[1][0][0]['event_time']

        # String sort: #25 < #30
        assert finished_time < decision_time, f"TASK_FINISHED ({finished_time}) should sort before MANUAL_DECISION ({decision_time})"


class TestResolvePagerduty:
    """Tests for resolve_pagerduty() — non-blocking PD resolve on human decisions.

    Stage 2 (ADR #103): invokes the notify Lambda's resolve_pagerduty action
    instead of the pagerduty_resolver SFN. The Lambda reads the routing key from
    alert_config by pipeline_name, so this no longer inspects alerts_json — it
    just needs NOTIFY_FUNCTION_ARN + a pipeline_name.
    """

    def test_happy_path_invokes_notify(self, mocker):
        """When env set and pipeline_name present, invokes the notify Lambda with
        the resolve_pagerduty action."""
        from console_api.utils import resolve_pagerduty

        mocker.patch.dict('os.environ', {'NOTIFY_FUNCTION_ARN': 'arn:aws:lambda:us-east-1:123:function:notify'})
        mock_lambda = mocker.patch('console_api.utils.lambda_client')

        item = {
            'execution_name': 'extract-2024-01-15-abc123',
            'task_name': 'extract',
            'pipeline_name': 'test-pipeline',
            'date': '2024-01-15',
        }
        resolve_pagerduty(item)

        mock_lambda.invoke.assert_called_once()
        kwargs = mock_lambda.invoke.call_args[1]
        assert kwargs['FunctionName'] == 'arn:aws:lambda:us-east-1:123:function:notify'
        assert kwargs['InvocationType'] == 'Event'   # fire-and-forget
        import json
        payload = json.loads(kwargs['Payload'])
        assert payload['action'] == 'resolve_pagerduty'
        assert payload['pipeline_name'] == 'test-pipeline'
        assert payload['failure']['task_name'] == 'extract'
        assert payload['failure']['date'] == '2024-01-15'
        assert payload['failure']['execution_name'] == 'extract-2024-01-15-abc123'

    def test_no_env_var_skips(self, mocker):
        """Without NOTIFY_FUNCTION_ARN, does nothing."""
        from console_api.utils import resolve_pagerduty

        mocker.patch.dict('os.environ', {}, clear=True)
        import os
        os.environ.pop('NOTIFY_FUNCTION_ARN', None)
        mock_lambda = mocker.patch('console_api.utils.lambda_client')

        resolve_pagerduty({'pipeline_name': 'test'})
        mock_lambda.invoke.assert_not_called()

    def test_no_pipeline_name_skips(self, mocker):
        """Without a pipeline_name, can't read alert_config — does nothing."""
        from console_api.utils import resolve_pagerduty

        mocker.patch.dict('os.environ', {'NOTIFY_FUNCTION_ARN': 'arn:notify'})
        mock_lambda = mocker.patch('console_api.utils.lambda_client')

        resolve_pagerduty({'execution_name': 'test-123'})
        mock_lambda.invoke.assert_not_called()

    def test_invoke_failure_non_blocking(self, mocker):
        """If the Lambda invoke fails, no exception raised — the action continues."""
        from console_api.utils import resolve_pagerduty

        mocker.patch.dict('os.environ', {'NOTIFY_FUNCTION_ARN': 'arn:notify'})
        mock_lambda = mocker.patch('console_api.utils.lambda_client')
        mock_lambda.invoke.side_effect = Exception("Lambda unavailable")

        item = {
            'execution_name': 'test-123',
            'task_name': 'extract',
            'pipeline_name': 'test',
            'date': '2024-01-15',
        }
        # Should NOT raise
        resolve_pagerduty(item)


class TestComputePipelineExecutionShort:
    """compute_pipeline_execution_short's own docstring documents it as a
    hand-maintained mirror of the JSONata in dependency_wrapper
    (JSONATA_EXEC_SHORT in polyris/generators.py: last 20 chars, strip '.'
    and ':'). Neither this function nor ensure_pipeline_execution_short had
    any direct test anywhere in either repo before this — only indirect
    coverage via task_actions.py's extract_pipeline_execution_short test,
    for a single input shape, not the boundary cases (exactly 20 chars,
    empty input, dots/colons) that a JSONata-mirroring function is most
    likely to silently drift on."""

    def test_empty_returns_empty(self):
        from console_api.utils import compute_pipeline_execution_short
        assert compute_pipeline_execution_short('') == ''

    def test_shorter_than_20_is_unchanged_apart_from_stripping(self):
        from console_api.utils import compute_pipeline_execution_short
        assert compute_pipeline_execution_short('a.b:c') == 'abc'

    def test_exactly_20_chars_boundary(self):
        from console_api.utils import compute_pipeline_execution_short
        s = 'x' * 20
        assert compute_pipeline_execution_short(s) == s

    def test_over_20_chars_takes_last_20(self):
        from console_api.utils import compute_pipeline_execution_short
        s = '2026-07-21T12:34:56.789Z-abc123def456ghi789jkl'
        # Mirrors JSONATA_EXEC_SHORT: last 20 chars of the RAW string, THEN
        # strip '.'/':' — not "take 20 chars after stripping".
        expected = s[-20:].replace('.', '').replace(':', '')
        assert compute_pipeline_execution_short(s) == expected

    def test_dots_and_colons_stripped_after_truncation_not_before(self):
        """Order matters: truncate to the last 20 raw characters FIRST, then
        strip '.'/':' — stripping first would shift which characters end up
        in the final 20, silently diverging from JSONATA_EXEC_SHORT (which
        truncates $exec itself, before any $replace)."""
        from console_api.utils import compute_pipeline_execution_short
        s = 'arn:aws:states:us-east-1:123456789012:execution:x:y'
        assert compute_pipeline_execution_short(s) == '789012executionxy'


class TestEnsurePipelineExecutionShort:
    def test_existing_short_wins_over_computing(self):
        from console_api.utils import ensure_pipeline_execution_short
        assert ensure_pipeline_execution_short('irrelevant:full.value', 'kept-as-is') == 'kept-as-is'

    def test_falls_back_to_compute_when_no_existing(self):
        from console_api.utils import ensure_pipeline_execution_short, compute_pipeline_execution_short
        full = 'sales-hourly-2026-07-16-abc123def456'
        assert ensure_pipeline_execution_short(full, '') == compute_pipeline_execution_short(full)

    def test_empty_both_yields_empty(self):
        from console_api.utils import ensure_pipeline_execution_short
        assert ensure_pipeline_execution_short('', '') == ''


class TestDictSchemaRichness:
    def test_empty_returns_zero(self):
        from utils import dict_schema_richness
        assert dict_schema_richness([]) == 0
        assert dict_schema_richness(None) == 0

    def test_minimal_column_scores_one(self):
        from utils import dict_schema_richness
        assert dict_schema_richness([{'name': 'x', 'type': 'bigint'}]) == 1

    def test_each_non_default_constraint_adds_one(self):
        from utils import dict_schema_richness
        # 1 (col) + 1 (PK) + 1 (NOT NULL) + 1 (description) = 4
        col = {'name': 'x', 'type': 'bigint',
               'primary_key': True, 'nullable': False, 'description': 'id'}
        assert dict_schema_richness([col]) == 4

    def test_default_value_does_not_count(self):
        from utils import dict_schema_richness
        # nullable=True is the default; explicit-but-default must not inflate.
        without = [{'name': 'x', 'type': 'bigint'}]
        with_default = [{'name': 'x', 'type': 'bigint', 'nullable': True}]
        assert dict_schema_richness(without) == dict_schema_richness(with_default)

    def test_richer_beats_longer_when_constraint_count_dominates(self):
        from utils import dict_schema_richness
        # 5 plain columns = 5; 1 column with 4 constraints = 5 — equal.
        # Add a 5th constraint and the rich-one column wins.
        many = [{'name': f'c{i}', 'type': 'bigint'} for i in range(5)]
        rich_one = [{'name': 'x', 'type': 'bigint',
                     'primary_key': True, 'nullable': False,
                     'unique': True, 'description': 'd', 'partition_key': True}]
        assert dict_schema_richness(many) == 5
        # 1 + 5 constraints = 6
        assert dict_schema_richness(rich_one) == 6
        assert dict_schema_richness(rich_one) > dict_schema_richness(many)

    def test_malformed_entries_skipped(self):
        from utils import dict_schema_richness
        schema = [
            {'name': 'ok', 'type': 'bigint', 'primary_key': True},
            'not-a-dict',
            None,
            {'name': 'ok2', 'type': 'string'},
        ]
        # 2 valid columns + 1 PK constraint = 3
        assert dict_schema_richness(schema) == 3


# ──────────────────────────────────────────────────────────────────────────────
# Filter helpers: is_internal_record / is_backfill_record / should_skip_token_row
# ──────────────────────────────────────────────────────────────────────────────

class TestIsInternalRecord:
    def test_pause_record(self):
        from utils import is_internal_record
        assert is_internal_record('_pause_my-pipeline-exec') is True

    def test_notify_warn_record(self):
        from utils import is_internal_record
        assert is_internal_record('_notify_warn_my-task') is True

    def test_regular_execution_name(self):
        from utils import is_internal_record
        assert is_internal_record('extract-2024-01-15-abc123') is False

    def test_empty_string(self):
        from utils import is_internal_record
        assert is_internal_record('') is False

    def test_backfill_id_not_caught_by_prefix(self):
        """Backfill IDs use 'bf-' prefix without leading underscore — they
        should NOT be caught by is_internal_record. is_backfill_record
        handles them via the item-based check."""
        from utils import is_internal_record
        assert is_internal_record('bf-abc123def') is False


class TestIsBackfillRecord:
    def test_by_record_type(self):
        from utils import is_backfill_record
        assert is_backfill_record({'record_type': 'backfill'}) is True

    def test_by_sentinel_pipeline_name(self):
        from utils import is_backfill_record
        assert is_backfill_record({'pipeline_name': '_polyris_bulk_backfill'}) is True

    def test_either_marker_sufficient(self):
        """Defense in depth: either field marks the row."""
        from utils import is_backfill_record
        # Only record_type, no pipeline_name
        assert is_backfill_record({'record_type': 'backfill'}) is True
        # Only pipeline_name, no record_type
        assert is_backfill_record({'pipeline_name': '_polyris_bulk_backfill'}) is True

    def test_regular_execution_not_backfill(self):
        from utils import is_backfill_record
        assert is_backfill_record({
            'execution_name': 'extract-2024-01-15-abc',
            'pipeline_name': 'my-pipeline',
        }) is False

    def test_empty_dict(self):
        from utils import is_backfill_record
        assert is_backfill_record({}) is False


class TestShouldSkipTokenRow:
    def test_internal_underscore_record_skipped(self):
        from utils import should_skip_token_row
        assert should_skip_token_row({'execution_name': '_pause_foo'}) is True

    def test_backfill_record_skipped(self):
        from utils import should_skip_token_row
        assert should_skip_token_row({
            'execution_name': 'bf-abc123',
            'record_type': 'backfill',
            'pipeline_name': '_polyris_bulk_backfill',
        }) is True

    def test_regular_execution_not_skipped(self):
        from utils import should_skip_token_row
        assert should_skip_token_row({
            'execution_name': 'extract-2024-01-15-abc',
            'pipeline_name': 'my-pipeline',
            'status': 'success',
        }) is False

    def test_empty_dict_not_skipped(self):
        """No keys = no markers = don't skip. Safer default."""
        from utils import should_skip_token_row
        assert should_skip_token_row({}) is False

    def test_internal_with_backfill_record_type(self):
        """Both markers — still skipped."""
        from utils import should_skip_token_row
        assert should_skip_token_row({
            'execution_name': '_internal_bf',
            'record_type': 'backfill',
        }) is True


def test_is_internal_record_catches_output_store():
    from utils import is_internal_record
    assert is_internal_record("output#sales#extract#2026-07-08") is True
    assert is_internal_record("extract-2026-07-08-abc123") is False
