"""
Integration tests for the auth gate as wired into main.handler (ADR #65).

Unlike test_auth.py (which unit-tests authenticate() in isolation), these pin
the *contract* that matters before flipping AUTH_ENABLED on:
  - enabled + no token -> 401 (and the route handler is never reached)
  - enabled + public path (/api/health) -> NOT 401 (gate bypassed)
  - enabled + valid PAT -> route reached with the principal attached
  - disabled -> no token required (current default behavior, unchanged)

This is the "test the enforcement, not just the verifier" gap called out in #13.
Route handlers are stubbed via ROUTES so no AWS/DDB is touched.
"""


import main
from response import cors_response
from auth import hash_token


def _event(method: str, path: str, token: str | None = None) -> dict:
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return {
        'rawPath': path,
        'headers': headers,
        'requestContext': {'http': {'method': method}, 'requestId': 'r1', 'stage': '$default'},
        'queryStringParameters': {},
    }


def test_enabled_without_token_returns_401(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    reached = mocker.MagicMock(return_value=cors_response(200, {}))
    mocker.patch.dict(main.ROUTES, {('GET', '/api/pipelines'): (reached, None)})
    resp = main.handler(_event('GET', '/api/pipelines'), None)
    assert resp['statusCode'] == 401
    reached.assert_not_called()  # gate blocks before the route


def test_enabled_health_is_public(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    stub = mocker.MagicMock(return_value=cors_response(200, {'ok': True}))
    mocker.patch.dict(main.ROUTES, {('GET', '/api/health'): (stub, None)})
    resp = main.handler(_event('GET', '/api/health'), None)  # no token
    assert resp['statusCode'] == 200
    stub.assert_called_once()


def test_enabled_valid_pat_reaches_route(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    repo = mocker.MagicMock()
    repo.get_by_hash.return_value = {
        'token_id': 'tok_1', 'token_hash': hash_token('plrs_good'), 'revoked': False,
    }
    mocker.patch.object(main, 'api_tokens_repo', repo)

    captured = {}

    def route(event):
        captured['principal'] = event.get('principal')
        return cors_response(200, {'ok': True})

    mocker.patch.dict(main.ROUTES, {('GET', '/api/pipelines'): (route, None)})
    resp = main.handler(_event('GET', '/api/pipelines', token='plrs_good'), None)
    assert resp['statusCode'] == 200
    assert captured['principal'].kind == 'service'
    assert captured['principal'].subject == 'tok_1'


def test_disabled_allows_without_token(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    stub = mocker.MagicMock(return_value=cors_response(200, {'ok': True}))
    mocker.patch.dict(main.ROUTES, {('GET', '/api/pipelines'): (stub, None)})
    resp = main.handler(_event('GET', '/api/pipelines'), None)  # no token, but auth off
    assert resp['statusCode'] == 200
    stub.assert_called_once()


def _pat_repo(mocker, scope=None):
    repo = mocker.MagicMock()
    rec = {'token_id': 'tok_1', 'token_hash': hash_token('plrs_good'), 'revoked': False}
    if scope is not None:
        rec['scope'] = scope
    repo.get_by_hash.return_value = rec
    mocker.patch.object(main, 'api_tokens_repo', repo)
    return repo


def test_enabled_read_token_forbidden_on_write_route(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    _pat_repo(mocker, scope='read')
    reached = mocker.MagicMock(return_value=cors_response(200, {}))
    mocker.patch.dict(main.ROUTES, {('POST', '/api/pipeline-run'): (reached, 'name')})
    resp = main.handler(_event('POST', '/api/pipeline-run', token='plrs_good'), None)
    assert resp['statusCode'] == 403  # authenticated but scope too low
    reached.assert_not_called()


def test_enabled_write_token_allowed_on_write_route(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    _pat_repo(mocker, scope='write')
    reached = mocker.MagicMock(return_value=cors_response(200, {'ok': True}))
    mocker.patch.dict(main.ROUTES, {('POST', '/api/backfill'): (reached, None)})
    resp = main.handler(_event('POST', '/api/backfill', token='plrs_good'), None)
    assert resp['statusCode'] == 200
    reached.assert_called_once()


def test_enabled_slack_callback_is_public(mocker, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    stub = mocker.MagicMock(return_value=cors_response(200, {'ok': True}))
    mocker.patch.dict(main.ROUTES, {('GET', '/api/action/skip'): (stub, None)})
    resp = main.handler(_event('GET', '/api/action/skip'), None)  # no token (Slack click)
    assert resp['statusCode'] == 200
    stub.assert_called_once()


def test_options_preflight_bypasses_gate_even_when_enabled(monkeypatch):
    # CORS preflight must NOT be blocked by auth, or the whole UI breaks when
    # AUTH_ENABLED=true. OPTIONS is handled before the gate. Regression guard.
    monkeypatch.setenv('AUTH_ENABLED', 'true')
    ev = _event('OPTIONS', '/api/pipelines')  # no token
    resp = main.handler(ev, None)
    assert resp['statusCode'] == 200


def test_disabled_unknown_route_still_404(monkeypatch):
    # Auth off must be fully transparent: unknown route still 404s (not altered
    # by the gate being skipped).
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    resp = main.handler(_event('GET', '/api/does-not-exist'), None)
    assert resp['statusCode'] == 404


def test_only_the_documented_routes_are_public():
    """Guard against a future route accidentally becoming public.

    is_public_path() matches by PREFIX (path.startswith), not exact path —
    correct for /api/health/simple and /api/action/{skip,fail,...}, but it
    means any NEW route registered under one of these prefixes silently
    bypasses auth too, with no error or warning. This test walks every
    currently-registered route in main.ROUTES and asserts that any route
    is_public_path() considers public is one of the routes explicitly
    reviewed and intended to be public — a new addition here must update
    this allowlist consciously, not slip through unnoticed.
    """
    from auth import is_public_path

    EXPECTED_PUBLIC_ROUTES = {
        ('GET', '/api/health'),
        ('GET', '/api/health/simple'),
        ('GET', '/api/metrics'),
        ('GET', '/api/action/skip'),
        ('GET', '/api/action/fail'),
        ('GET', '/api/action/success'),
        ('GET', '/api/action/restart'),
    }

    actual_public_routes = {
        (method, path) for (method, path) in main.ROUTES
        if is_public_path(path)
    }

    unexpected = actual_public_routes - EXPECTED_PUBLIC_ROUTES
    assert not unexpected, (
        f"Route(s) {unexpected} are newly public via prefix match in "
        f"is_public_path — if intentional, add them to EXPECTED_PUBLIC_ROUTES "
        f"in this test; if not, they need a distinct path outside the "
        f"public-path prefixes."
    )
