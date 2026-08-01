"""Unit tests for response.py — CORS/error/body-parsing helpers shared by
every route handler in console_api. No dedicated test file existed for this
module before, despite it being used everywhere.
"""
import json

from response import (
    cors_response, error_response, safe_parse_body,
    validation_error, not_found_error,
)


class TestCorsResponse:
    def test_sets_status_and_serializes_body(self):
        resp = cors_response(200, {'ok': True})
        assert resp['statusCode'] == 200
        assert json.loads(resp['body']) == {'ok': True}

    def test_cors_and_cache_headers_present(self):
        resp = cors_response(200, {})
        headers = resp['headers']
        assert headers['Access-Control-Allow-Origin'] == '*'
        assert 'no-cache' in headers['Cache-Control']

    def test_non_json_native_values_use_str_fallback(self):
        """default=str lets datetimes/Decimals etc. through instead of raising."""
        from datetime import datetime, timezone
        resp = cors_response(200, {'ts': datetime(2026, 1, 1, tzinfo=timezone.utc)})
        assert '2026-01-01' in resp['body']


class TestErrorResponse:
    def test_minimal_error_has_only_error_key(self):
        resp = error_response(400, 'BAD_REQUEST')
        body = json.loads(resp['body'])
        assert body == {'error': 'BAD_REQUEST'}

    def test_message_details_and_request_id_included_when_given(self):
        resp = error_response(500, 'INTERNAL', message='oops', details={'x': 1}, request_id='req-1')
        body = json.loads(resp['body'])
        assert body == {
            'error': 'INTERNAL', 'message': 'oops', 'details': {'x': 1}, 'request_id': 'req-1',
        }


class TestSafeParseBody:
    """Regression tests for a real bug found in a code-review pass: every
    caller of safe_parse_body immediately does `body.get(...)` right after
    checking `if err:`, with no isinstance check. A syntactically valid but
    non-dict JSON body (array, string, number, null) previously passed
    straight through as `(parsed_value, None)`, so the very next line in the
    route handler raised an unhandled AttributeError — caught only by
    main.py's generic top-level exception handler as an unhelpful 500,
    instead of the clean 400 this function's own docstring promises for
    "malformed" input.
    """

    def test_missing_body_returns_empty_dict(self):
        body, err = safe_parse_body({})
        assert body == {}
        assert err is None

    def test_valid_object_body_parses_through(self):
        body, err = safe_parse_body({'body': json.dumps({'name': 'x'})})
        assert body == {'name': 'x'}
        assert err is None

    def test_malformed_json_returns_400(self):
        body, err = safe_parse_body({'body': '{not valid json'})
        assert body is None
        assert err['statusCode'] == 400
        assert json.loads(err['body'])['error'] == 'INVALID_JSON'

    def test_json_array_body_returns_400_not_a_crash(self):
        body, err = safe_parse_body({'body': json.dumps([1, 2, 3])})
        assert body is None
        assert err is not None
        assert err['statusCode'] == 400
        assert json.loads(err['body'])['error'] == 'INVALID_JSON'

    def test_json_string_body_returns_400(self):
        body, err = safe_parse_body({'body': json.dumps("just a string")})
        assert body is None
        assert err['statusCode'] == 400

    def test_json_number_body_returns_400(self):
        body, err = safe_parse_body({'body': '42'})
        assert body is None
        assert err['statusCode'] == 400

    def test_json_null_body_returns_400(self):
        """'null' is valid JSON (parses to None) and is falsy, but it's
        already caught by the `if not raw` empty-body check upstream of
        json.loads — this documents that path stays the "empty body" shape,
        not an error, since the RAW STRING 'null' only reaches json.loads
        when non-empty; confirm behavior explicitly either way."""
        body, err = safe_parse_body({'body': 'null'})
        assert body is None
        assert err['statusCode'] == 400


class TestValidationAndNotFoundErrors:
    def test_validation_error_shape(self):
        resp = validation_error('date', 'must be YYYY-MM-DD', value='not-a-date')
        body = json.loads(resp['body'])
        assert resp['statusCode'] == 400
        assert body['error'] == 'VALIDATION_ERROR'
        assert body['details']['field'] == 'date'
        assert body['details']['received'] == 'not-a-date'

    def test_validation_error_truncates_long_value(self):
        resp = validation_error('x', 'too long', value='y' * 500)
        body = json.loads(resp['body'])
        assert len(body['details']['received']) == 100

    def test_not_found_error_shape(self):
        resp = not_found_error('pipeline', 'my-pipeline')
        body = json.loads(resp['body'])
        assert resp['statusCode'] == 404
        assert 'my-pipeline' in body['message']
        assert body['details'] == {'resource_type': 'pipeline', 'resource_id': 'my-pipeline'}
