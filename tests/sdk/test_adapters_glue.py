"""Tests for polyris.adapters.glue — Glue Catalog → List[Column].

Patches boto3.client at import time inside the adapter module so we can
drive the Glue API surface without real AWS credentials.
"""
from __future__ import annotations


import pytest

from polyris import schema as s
from polyris.adapters.glue import glue_table_to_columns


def _glue_response(columns=None, partition_keys=None):
    """Shape a fake glue.get_table() response."""
    return {
        'Table': {
            'StorageDescriptor': {'Columns': columns or []},
            'PartitionKeys': partition_keys or [],
        },
    }


def _patched_glue_client(mocker, response):
    """Build a fake boto3 client whose .get_table() returns `response`."""
    client = mocker.MagicMock()
    client.get_table.return_value = response
    return client


# =============================================================================
# Basic shape
# =============================================================================

class TestBasicShape:

    def test_simple_columns_returned_as_columns(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[
                {'Name': 'order_id', 'Type': 'bigint'},
                {'Name': 'amount', 'Type': 'decimal(10,2)'},
            ],
        ))
        # _require_boto3() only triggers `import boto3`; the patch above
        # also covers the import path because polyris.adapters.glue
        # already has `boto3` in its module namespace.
        cols = glue_table_to_columns('analytics', 'orders')

        assert len(cols) == 2
        assert cols[0].name == 'order_id'
        assert cols[0].type == s.bigint()
        assert cols[0].partition_key is False
        assert cols[1].name == 'amount'
        assert cols[1].type == s.decimal(10, 2)

    def test_partition_keys_marked_partition_key(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[{'Name': 'id', 'Type': 'bigint'}],
            partition_keys=[{'Name': 'event_date', 'Type': 'date'}],
        ))
        cols = glue_table_to_columns('analytics', 'orders')

        assert len(cols) == 2
        assert cols[1].name == 'event_date'
        assert cols[1].partition_key is True
        assert cols[1].type == s.date()

    def test_comment_becomes_description(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[{'Name': 'id', 'Type': 'bigint',
                      'Comment': 'Primary key'}],
        ))
        cols = glue_table_to_columns('analytics', 'orders')

        assert cols[0].description == 'Primary key'

    def test_empty_table_returns_empty_list(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response())
        cols = glue_table_to_columns('analytics', 'empty')

        assert cols == []

    def test_complex_types_parse_via_type_from_string(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[
                {'Name': 'tags', 'Type': 'array<string>'},
                {'Name': 'attrs', 'Type': 'map<string,bigint>'},
                {'Name': 'point', 'Type': 'struct<x:int,y:int>'},
            ],
        ))
        cols = glue_table_to_columns('analytics', 'orders')

        assert cols[0].type == s.array(s.string())
        assert cols[1].type == s.map_(s.string(), s.bigint())
        assert cols[2].type == s.struct(x=s.integer(), y=s.integer())


# =============================================================================
# CatalogId / region pass-through
# =============================================================================

class TestCallParameters:

    def test_catalog_id_passed_when_set(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_client = _patched_glue_client(mocker, _glue_response())
        mock_boto3.client.return_value = mock_client
        glue_table_to_columns('db', 't', catalog_id='123456789012')

        kwargs = mock_client.get_table.call_args.kwargs
        assert kwargs['CatalogId'] == '123456789012'
        assert kwargs['DatabaseName'] == 'db'
        assert kwargs['Name'] == 't'

    def test_catalog_id_omitted_when_unset(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_client = _patched_glue_client(mocker, _glue_response())
        mock_boto3.client.return_value = mock_client
        glue_table_to_columns('db', 't')

        kwargs = mock_client.get_table.call_args.kwargs
        assert 'CatalogId' not in kwargs

    def test_region_passed_to_boto3_client(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response())
        glue_table_to_columns('db', 't', region='us-west-2')

        # boto3.client('glue', region_name='us-west-2')
        mock_boto3.client.assert_called_with('glue', region_name='us-west-2')

    def test_region_default_uses_session_default(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response())
        glue_table_to_columns('db', 't')

        mock_boto3.client.assert_called_with('glue')


# =============================================================================
# Validation
# =============================================================================

class TestValidation:

    def test_missing_name_raises(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[{'Type': 'bigint'}],  # no Name
        ))
        with pytest.raises(ValueError, match="missing 'Name'"):
            glue_table_to_columns('db', 't')

    def test_missing_type_raises(self, mocker):
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[{'Name': 'x'}],  # no Type
        ))
        with pytest.raises(ValueError, match="missing 'Type'"):
            glue_table_to_columns('db', 't')


# =============================================================================
# Asset.from_glue_table integration
# =============================================================================

class TestAssetFromGlueTable:

    def test_basic_construction_and_glue_table_carried(self, mocker):
        from polyris import Asset
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[
                {'Name': 'order_id', 'Type': 'bigint'},
                {'Name': 'amount', 'Type': 'decimal(10,2)'},
            ],
            partition_keys=[{'Name': 'event_date', 'Type': 'date'}],
        ))
        a = Asset.from_glue_table(
            'analytics.orders',
            name='retail/orders',
            owner='data-team',
        )

        assert a.name == 'retail/orders'
        assert a.glue_table == 'analytics.orders'
        assert a.owner == 'data-team'
        assert len(a.schema) == 3
        assert a.schema[2].partition_key is True

    def test_default_name_is_glue_table(self, mocker):
        from polyris import Asset
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[{'Name': 'x', 'Type': 'bigint'}],
        ))
        a = Asset.from_glue_table('analytics.orders')

        assert a.name == 'analytics.orders'

    def test_malformed_glue_table_rejected(self, mocker):
        from polyris import Asset
        with pytest.raises(ValueError, match="database.table"):
            Asset.from_glue_table('not_a_table_ref')

    def test_rejects_explicit_schema_kwarg(self, mocker):
        from polyris import Asset
        with pytest.raises(TypeError, match='from_glue_table'):
            Asset.from_glue_table('a.b', schema=[])

    def test_rejects_glue_table_kwarg_collision(self, mocker):
        from polyris import Asset
        with pytest.raises(TypeError, match='glue_table'):
            Asset.from_glue_table('a.b', glue_table='c.d')

    def test_catalog_id_propagates_to_asset(self, mocker):
        from polyris import Asset
        mock_boto3 = mocker.patch('polyris.adapters.glue.boto3')
        mock_boto3.client.return_value = _patched_glue_client(mocker, _glue_response(
            columns=[{'Name': 'x', 'Type': 'bigint'}],
        ))
        a = Asset.from_glue_table(
            'analytics.orders',
            catalog_id='123456789012',
        )

        # Cross-account: glue_catalog must be carried into the Asset so the
        # Phase 2 console_api drift route can pass CatalogId at runtime too.
        assert a.glue_catalog == '123456789012'
