"""Tests for Asset inspection / export helpers.

Covers:
    - Asset.print_schema()  — REPL ergonomics
    - Asset.to_ddl()        — Glue/Hive DDL generation
    - Asset.to_jsonschema() — JSON Schema Draft 2020-12 export
    - Asset.from_iceberg()  — pyiceberg Table → Asset shortcut
"""
from __future__ import annotations

import io
import sys

import pytest

from polyris import Asset, Column, types as t


# =============================================================================
# print_schema
# =============================================================================

class TestPrintSchema:
    def _capture(self, fn) -> str:
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            fn()
        finally:
            sys.stdout = old
        return buf.getvalue()

    def test_no_schema_prints_one_line(self):
        a = Asset(name="empty/asset")
        out = self._capture(a.print_schema)
        assert "no schema" in out.lower()
        assert "empty/asset" in out

    def test_basic_schema_prints_table_header(self):
        a = Asset(name="x", schema=[Column("id", t.bigint())])
        out = self._capture(a.print_schema)
        assert "name" in out
        assert "type" in out
        assert "constraints" in out
        assert "description" in out

    def test_columns_show_in_order(self):
        a = Asset(name="x", schema=[
            Column("alpha", t.bigint()),
            Column("beta", t.string()),
            Column("gamma", t.date()),
        ])
        out = self._capture(a.print_schema)
        # Index of each name in the output should be in declared order.
        assert out.index("alpha") < out.index("beta") < out.index("gamma")

    def test_constraints_compact(self):
        a = Asset(name="x", schema=[
            Column("id", t.bigint(), primary_key=True, nullable=False),
            Column("event_date", t.date(), partition_key=True),
            Column("token", t.string(), unique=True),
        ])
        out = self._capture(a.print_schema)
        # Each constraint label appears at least once.
        for label in ("PK", "NOT NULL", "Partition", "UNIQUE"):
            assert label in out, f"missing constraint label {label!r} in print_schema output"

    def test_column_count_in_header(self):
        a = Asset(name="x", schema=[
            Column("a", t.bigint()),
            Column("b", t.string()),
        ])
        out = self._capture(a.print_schema)
        assert "2 column" in out

    def test_singular_column_label(self):
        # No trailing 's' for a single column — small detail, real polish.
        a = Asset(name="x", schema=[Column("only", t.bigint())])
        out = self._capture(a.print_schema)
        assert "1 column" in out
        assert "1 columns" not in out


# =============================================================================
# to_ddl
# =============================================================================

class TestToDDL:
    def test_basic_create_table(self):
        a = Asset(name="x", schema=[
            Column("id", t.bigint()),
            Column("amount", t.decimal(10, 2)),
        ])
        ddl = a.to_ddl()
        assert "CREATE EXTERNAL TABLE" in ddl
        assert "`id` bigint" in ddl
        assert "`amount` decimal(10,2)" in ddl

    def test_uses_glue_table_when_set(self):
        a = Asset(name="retail/orders",
                  glue_table="analytics.orders",
                  schema=[Column("id", t.bigint())])
        assert "analytics.orders" in a.to_ddl()
        # Asset name with slash should not appear unwrapped.
        assert "`retail/orders`" not in a.to_ddl()

    def test_falls_back_to_asset_name_when_no_glue_table(self):
        a = Asset(name="my-asset", schema=[Column("id", t.bigint())])
        assert "`my-asset`" in a.to_ddl()

    def test_partition_columns_separated(self):
        a = Asset(name="x", schema=[
            Column("id", t.bigint()),
            Column("event_date", t.date(), partition_key=True),
        ])
        ddl = a.to_ddl()
        assert "PARTITIONED BY" in ddl
        # Partition column NOT inside the regular columns block.
        column_block = ddl.split("PARTITIONED BY")[0]
        assert "event_date" not in column_block
        # But IS in the partition block.
        partition_block = ddl.split("PARTITIONED BY")[1]
        assert "`event_date` date" in partition_block

    def test_description_emits_comment(self):
        a = Asset(name="x", schema=[
            Column("id", t.bigint(), description="Primary key"),
        ])
        ddl = a.to_ddl()
        assert "COMMENT 'Primary key'" in ddl

    def test_single_quote_in_description_is_escaped(self):
        a = Asset(name="x", schema=[
            Column("id", t.bigint(), description="user's id"),
        ])
        ddl = a.to_ddl()
        # Hive convention: `'` doubles to `''` inside the COMMENT literal.
        assert "user''s id" in ddl

    def test_table_description_emits_table_comment(self):
        a = Asset(name="x",
                  description="Customer orders fact table",
                  schema=[Column("id", t.bigint())])
        ddl = a.to_ddl()
        assert "COMMENT 'Customer orders fact table'" in ddl

    def test_s3_uri_emits_location(self):
        a = Asset(name="x",
                  uri="s3://lake/orders/",
                  schema=[Column("id", t.bigint())])
        assert "LOCATION 's3://lake/orders/'" in a.to_ddl()

    def test_non_s3_uri_does_not_emit_location(self):
        # Asset can be declared with a non-S3 uri (placeholder, name-only); we
        # only emit LOCATION when it is a real lake path.
        a = Asset(name="x", uri="", schema=[Column("id", t.bigint())])
        assert "LOCATION" not in a.to_ddl()

    def test_empty_schema_raises(self):
        a = Asset(name="x")
        with pytest.raises(ValueError, match="no schema declared"):
            a.to_ddl()

    def test_unsupported_dialect_raises(self):
        a = Asset(name="x", schema=[Column("id", t.bigint())])
        with pytest.raises(ValueError, match="bigquery"):
            a.to_ddl(dialect='postgres')


# =============================================================================
# to_jsonschema
# =============================================================================

class TestToJsonSchema:
    def test_empty_schema_yields_object_with_no_props(self):
        a = Asset(name="empty")
        js = a.to_jsonschema()
        assert js["type"] == "object"
        assert js["properties"] == {}
        assert js["title"] == "empty"

    def test_basic_props_and_types(self):
        a = Asset(name="x", schema=[
            Column("id", t.bigint(), nullable=False),
            Column("name", t.string()),
            Column("active", t.boolean()),
        ])
        js = a.to_jsonschema()
        # Non-nullable id → required
        assert "id" in js["required"]
        assert "name" not in js.get("required", [])
        # Nullable -> type union
        assert js["properties"]["name"]["type"] == ["string", "null"]
        # Non-nullable -> plain type
        assert js["properties"]["id"]["type"] == "integer"

    def test_descriptions_carry_through(self):
        a = Asset(name="x",
                  description="An asset",
                  schema=[Column("id", t.bigint(), nullable=False, description="Unique")])
        js = a.to_jsonschema()
        assert js["description"] == "An asset"
        assert js["properties"]["id"]["description"] == "Unique"

    def test_decimal_carries_format(self):
        a = Asset(name="x", schema=[
            Column("amount", t.decimal(10, 2), nullable=False),
        ])
        js = a.to_jsonschema()
        assert js["properties"]["amount"]["format"] == "decimal(10,2)"

    def test_array_field_yields_items(self):
        a = Asset(name="x", schema=[
            Column("tags", t.array(t.string()), nullable=False),
        ])
        js = a.to_jsonschema()
        assert js["properties"]["tags"]["type"] == "array"
        assert js["properties"]["tags"]["items"] == {"type": "string"}

    def test_struct_field_yields_nested_object(self):
        a = Asset(name="x", schema=[
            Column("address", t.struct(street=t.string(), zip=t.bigint()), nullable=False),
        ])
        js = a.to_jsonschema()
        addr = js["properties"]["address"]
        assert addr["type"] == "object"
        assert set(addr["properties"]) == {"street", "zip"}


# =============================================================================
# from_iceberg
# =============================================================================

class _FakeIcebergSchema:
    """Minimal duck-typed stand-in for `pyiceberg.schema.Schema`."""
    def __init__(self, pa_schema):
        self._pa = pa_schema

    def as_arrow(self):
        return self._pa


class _FakeIcebergTable:
    """Minimal duck-typed stand-in for `pyiceberg.table.Table`."""
    def __init__(self, pa_schema, ident=('analytics', 'orders')):
        self._schema = _FakeIcebergSchema(pa_schema)
        self._ident = ident

    def schema(self):
        return self._schema

    def name(self):
        return self._ident


class TestFromIceberg:
    def _pa_schema(self):
        # pyarrow is an optional extra (`pip install polyris[pyarrow]`); skip the
        # Iceberg construction tests cleanly when it is not installed rather than
        # hard-failing on a base install.
        pa = pytest.importorskip("pyarrow")
        return pa.schema([
            pa.field('id', pa.int64(), nullable=False),
            pa.field('amount', pa.decimal128(10, 2)),
        ])

    def test_basic_construction_with_explicit_name(self):
        a = Asset.from_iceberg(_FakeIcebergTable(self._pa_schema()),
                               name='retail/orders')
        assert a.name == 'retail/orders'
        assert a.group == 'retail'
        assert len(a.schema) == 2

    def test_default_name_from_iceberg_identifier(self):
        a = Asset.from_iceberg(_FakeIcebergTable(self._pa_schema()))
        # Iceberg Identifier (tuple) → joined with dot, mirrors from_glue_table.
        assert a.name == 'analytics.orders'

    def test_kwargs_pass_through(self):
        a = Asset.from_iceberg(
            _FakeIcebergTable(self._pa_schema()),
            name='retail/orders',
            owner='data-team',
            description='Iceberg-backed asset',
        )
        assert a.owner == 'data-team'
        assert a.description == 'Iceberg-backed asset'

    def test_rejects_explicit_schema_kwarg(self):
        with pytest.raises(TypeError, match='from_iceberg'):
            Asset.from_iceberg(_FakeIcebergTable(self._pa_schema()),
                               name='x', schema=[])

    def test_rejects_non_iceberg_object(self):
        with pytest.raises(AttributeError, match='pyiceberg'):
            Asset.from_iceberg("just a string", name='x')

    def test_rejects_old_pyiceberg_without_as_arrow(self):
        class OldStyleSchema:
            pass  # no .as_arrow() method
        class OldStyleTable:
            def schema(self):
                return OldStyleSchema()
            def name(self):
                return ('a', 'b')
        with pytest.raises(AttributeError, match='as_arrow'):
            Asset.from_iceberg(OldStyleTable(), name='x')


# =============================================================================
# Cross-account / cross-region Glue support — ADR #45
#
# `glue_table` validates structurally at construction time so that a malformed
# value surfaces in the editor, not as a 422 from the Console API. `glue_region`
# threads through `to_dict` so the backend can pin its boto3 client.
# `from_glue_table` carries `region` into `glue_region` and prepends
# `catalog_id` to the default name so two same-named tables in different
# accounts don't collide on a single asset entry.
# =============================================================================

class TestGlueTableValidation:
    def test_missing_dot_raises(self):
        with pytest.raises(ValueError, match="must be 'database.table'"):
            Asset(name='x', glue_table='no_dot_here')

    def test_trailing_dot_raises(self):
        with pytest.raises(ValueError, match="both sides non-empty"):
            Asset(name='x', glue_table='db.')

    def test_leading_dot_raises(self):
        with pytest.raises(ValueError, match="both sides non-empty"):
            Asset(name='x', glue_table='.tbl')

    def test_only_dot_raises(self):
        # Single-dot edge case — both sides are empty.
        with pytest.raises(ValueError, match="both sides non-empty"):
            Asset(name='x', glue_table='.')

    def test_valid_simple_format_accepted(self):
        # The canonical case: should construct without raising.
        a = Asset(name='x', glue_table='default.example')
        assert a.glue_table == 'default.example'

    def test_empty_glue_table_skips_validation(self):
        # No glue_table set is the common case for assets that don't yet
        # have a Glue mapping — validation should not fire.
        a = Asset(name='x')
        assert a.glue_table == ''

    def test_table_with_underscore_and_hyphen_accepted(self):
        # Glue allows underscores and (usually) hyphens; only the format
        # check (one dot, both sides non-empty) matters here.
        a = Asset(name='x', glue_table='my_db.my-table_v2')
        assert a.glue_table == 'my_db.my-table_v2'


class TestGlueRegion:
    def test_default_glue_region_empty(self):
        a = Asset(name='x')
        assert a.glue_region == ''

    def test_glue_region_stored(self):
        a = Asset(name='x', glue_table='default.example', glue_region='eu-west-1')
        assert a.glue_region == 'eu-west-1'

    def test_glue_region_in_to_dict(self):
        a = Asset(name='x', glue_table='default.example', glue_region='ap-northeast-1',
                  schema=[Column('id', t.bigint())])
        d = a.to_dict()
        assert d['glue_region'] == 'ap-northeast-1'

    def test_glue_region_independent_of_catalog(self):
        # Both fields are independently optional — region without catalog,
        # catalog without region, both, or neither are all valid.
        a = Asset(name='x', glue_table='default.example', glue_region='eu-west-1')
        assert a.glue_catalog == ''
        a = Asset(name='x', glue_table='default.example', glue_catalog='111111111111')
        assert a.glue_region == ''


class TestFromGlueTablePersistsRegion:
    """`from_glue_table(region=...)` populates `Asset.glue_region` so the
    backend's drift-detection call can target the right region. Without this,
    a cross-region asset succeeds at deploy time (developer credentials see
    the table) but fails at runtime because the Lambda queries the wrong
    region and gets EntityNotFoundException."""

    def test_region_persists_to_glue_region(self, mocker):
        from polyris import Column, types as t
        mocker.patch('polyris.adapters.glue.glue_table_to_columns',
                     return_value=[Column('id', t.bigint())])
        a = Asset.from_glue_table('default.example', region='eu-west-1')
        assert a.glue_region == 'eu-west-1'

    def test_region_None_yields_empty_string(self, mocker):
        # Empty string is the storage convention; None is the kwarg default.
        from polyris import Column, types as t
        mocker.patch('polyris.adapters.glue.glue_table_to_columns',
                     return_value=[Column('id', t.bigint())])
        a = Asset.from_glue_table('default.example')
        assert a.glue_region == ''


class TestFromGlueTableDefaultName:
    """Cross-account collision: two pipelines each declaring `default.example`
    against different AWS accounts must not collapse into one asset entry.
    Default name includes `catalog_id` when present."""

    def test_default_name_when_no_catalog(self, mocker):
        from polyris import Column, types as t
        mocker.patch('polyris.adapters.glue.glue_table_to_columns',
                     return_value=[Column('id', t.bigint())])
        a = Asset.from_glue_table('default.example')
        assert a.name == 'default.example'

    def test_default_name_when_catalog_set(self, mocker):
        from polyris import Column, types as t
        mocker.patch('polyris.adapters.glue.glue_table_to_columns',
                     return_value=[Column('id', t.bigint())])
        a = Asset.from_glue_table('default.example', catalog_id='222222222222')
        assert a.name == '222222222222.default.example'

    def test_explicit_name_overrides_default(self, mocker):
        from polyris import Column, types as t
        mocker.patch('polyris.adapters.glue.glue_table_to_columns',
                     return_value=[Column('id', t.bigint())])
        a = Asset.from_glue_table('default.example', catalog_id='222',
                                  name='retail/orders')
        assert a.name == 'retail/orders'

    def test_two_accounts_yield_distinct_default_names(self, mocker):
        # Regression test for the cross-account collision: same Glue table
        # declared across two accounts must have distinct asset names so
        # the backend's `_build_assets_from_pipelines` doesn't merge them.
        from polyris import Column, types as t
        mocker.patch('polyris.adapters.glue.glue_table_to_columns',
                     return_value=[Column('id', t.bigint())])
        a1 = Asset.from_glue_table('default.example', catalog_id='111')
        a2 = Asset.from_glue_table('default.example', catalog_id='222')
        assert a1.name != a2.name


# ──────────────────────────────────────────────────────────────────────────────
# _serialize_outlet — partition_start + granularity (v0.78+). These fields
# were added to Asset constructor for granularity-aware backfill (ADR #58);
# this test confirms they survive serialization into the pipeline_registry
# via the path actually used by registration.
# ──────────────────────────────────────────────────────────────────────────────


class TestSerializeOutletGranularityFields:
    """Confirm partition_start and granularity flow into pipeline_registry."""

    def test_outlet_with_partition_start_serialized(self):
        from polyris.assets import Asset
        from polyris.generators import _serialize_outlet
        a = Asset('cat/db/table', partition_start='2023-01-01')
        d = _serialize_outlet(a)
        assert d['partition_start'] == '2023-01-01'

    def test_outlet_without_partition_start_omits_field(self):
        from polyris.assets import Asset
        from polyris.generators import _serialize_outlet
        a = Asset('cat/db/table')
        d = _serialize_outlet(a)
        # Field is intentionally omitted (not None) when unset, to keep
        # registry records compact.
        assert 'partition_start' not in d

    def test_outlet_granularity_serialized(self):
        from polyris.assets import Asset
        from polyris.generators import _serialize_outlet
        a = Asset('cat/db/table', granularity='weekly')
        d = _serialize_outlet(a)
        assert d['granularity'] == 'weekly'
