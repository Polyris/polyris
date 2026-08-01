"""
Unit tests for the per-token scope model (ADR #66) in auth.py.

Covers required_level() derivation across the real route table, authorize()
allow/deny, and the principal scope defaults (JWT -> admin, legacy PAT -> admin).
"""

import pytest

from auth import (
    required_level, authorize, Principal, AuthzError,
    SCOPE_LEVELS, VALID_SCOPES, DEFAULT_NEW_TOKEN_SCOPE,
)


class TestRequiredLevel:
    def test_get_is_read(self):
        assert required_level('GET', '/api/pipelines') == 'read'

    def test_mutation_is_write(self):
        assert required_level('POST', '/api/pipeline-run') == 'write'
        assert required_level('PUT', '/api/task-config') == 'write'
        assert required_level('POST', '/api/backfill') == 'write'

    def test_destructive_and_token_mgmt_are_admin(self):
        assert required_level('DELETE', '/api/asset-delete') == 'admin'
        assert required_level('POST', '/api/assets/delete-orphaned') == 'admin'
        assert required_level('POST', '/api/tokens') == 'admin'
        assert required_level('GET', '/api/tokens') == 'admin'
        assert required_level('DELETE', '/api/tokens') == 'admin'

    def test_covers_whole_route_table(self):
        # Every real route resolves to a valid level with no hand-maintenance.
        from main import ROUTES
        for (method, path) in ROUTES:
            assert required_level(method, path) in VALID_SCOPES

    def test_default_scope_is_least_privilege(self):
        assert DEFAULT_NEW_TOKEN_SCOPE == 'read'
        assert SCOPE_LEVELS['read'] < SCOPE_LEVELS['write'] < SCOPE_LEVELS['admin']


class TestAuthorize:
    def test_read_token_allowed_on_get(self):
        authorize(Principal('service', 't', scope='read'), 'GET', '/api/pipelines')

    def test_read_token_denied_on_write(self):
        with pytest.raises(AuthzError):
            authorize(Principal('service', 't', scope='read'), 'POST', '/api/pipeline-run')

    def test_write_token_can_backfill(self):
        authorize(Principal('service', 't', scope='write'), 'POST', '/api/backfill')

    def test_write_token_denied_on_delete_and_tokens(self):
        p = Principal('service', 't', scope='write')
        with pytest.raises(AuthzError):
            authorize(p, 'DELETE', '/api/asset-delete')
        with pytest.raises(AuthzError):
            authorize(p, 'POST', '/api/tokens')

    def test_admin_token_allowed_everywhere(self):
        p = Principal('service', 't', scope='admin')
        authorize(p, 'DELETE', '/api/asset-delete')
        authorize(p, 'POST', '/api/tokens')

    def test_legacy_pat_without_scope_is_admin(self):
        # Backward compat (#4): tokens minted before scopes keep full access.
        p = Principal('service', 't')  # no scope
        assert p.scope == 'admin'
        authorize(p, 'DELETE', '/api/asset-delete')

    def test_cognito_user_is_admin(self):
        p = Principal('user', 'cognito-sub', scope='read')  # scope arg ignored for users
        assert p.scope == 'admin'
        authorize(p, 'DELETE', '/api/asset-delete')
