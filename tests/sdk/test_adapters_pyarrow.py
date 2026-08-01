"""Tests for polyris.adapters.pyarrow_ — pyarrow.Schema ↔ List[Column]."""
from __future__ import annotations

import pytest

# Skip the entire module gracefully if pyarrow is not installed — keeps
# `pytest tests/sdk/` runnable on a base-deps-only install.
pa = pytest.importorskip("pyarrow")

from polyris import schema as s
from polyris.adapters.pyarrow_ import (
    columns_to_pyarrow,
    pyarrow_to_columns,
)
from polyris.schema import Column


# =============================================================================
# pyarrow → polyris
# =============================================================================

class TestPyarrowToColumns:

    @pytest.mark.parametrize("pa_type,expected", [
        (pa.int8(),     s.tinyint()),
        (pa.int16(),    s.smallint()),
        (pa.int32(),    s.integer()),
        (pa.int64(),    s.bigint()),
        (pa.uint8(),    s.tinyint()),    # documented collapse
        (pa.uint64(),   s.bigint()),
        (pa.float16(),  s.float_()),     # widens (no float16 in polyris)
        (pa.float32(),  s.float_()),
        (pa.float64(),  s.double()),
        (pa.bool_(),    s.boolean()),
        (pa.string(),   s.string()),
        (pa.large_string(), s.string()),
        (pa.binary(),   s.binary()),
        (pa.large_binary(), s.binary()),
        (pa.binary(16), s.fixed_binary(16)),
        (pa.date32(),   s.date()),
        (pa.date64(),   s.date()),
        (pa.time64('us'), s.time()),
        (pa.decimal128(10, 2), s.decimal(10, 2)),
        (pa.decimal256(38, 0), s.decimal(38, 0)),
    ])
    def test_simple_type_mapping(self, pa_type, expected):
        pa_schema = pa.schema([pa.field('c', pa_type)])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == expected

    def test_timestamp_with_tz_maps_to_tz_aware(self):
        pa_schema = pa.schema([pa.field('t', pa.timestamp('us', tz='UTC'))])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.timestamp(tz_aware=True)

    def test_timestamp_without_tz_maps_to_ntz(self):
        pa_schema = pa.schema([pa.field('t', pa.timestamp('us'))])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.timestamp(tz_aware=False)

    def test_list_maps_to_array(self):
        pa_schema = pa.schema([pa.field('tags', pa.list_(pa.string()))])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.array(s.string())

    def test_large_list_maps_to_array(self):
        pa_schema = pa.schema([pa.field('xs', pa.large_list(pa.int64()))])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.array(s.bigint())

    def test_fixed_size_list_collapses_to_array(self):
        pa_schema = pa.schema([pa.field('xs', pa.list_(pa.int32(), 4))])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.array(s.integer())

    def test_map_maps_to_map(self):
        pa_schema = pa.schema([
            pa.field('m', pa.map_(pa.string(), pa.int64())),
        ])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.map_(s.string(), s.bigint())

    def test_nested_struct(self):
        pa_schema = pa.schema([
            pa.field('point', pa.struct([
                pa.field('x', pa.int64()),
                pa.field('y', pa.int64()),
            ])),
        ])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.struct(x=s.bigint(), y=s.bigint())

    def test_dictionary_collapses_to_value_type(self):
        pa_schema = pa.schema([
            pa.field('cat', pa.dictionary(pa.int32(), pa.string())),
        ])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].type == s.string()

    def test_nullability_carried_over(self):
        pa_schema = pa.schema([
            pa.field('required', pa.int64(), nullable=False),
            pa.field('optional', pa.int64(), nullable=True),
        ])
        cols = pyarrow_to_columns(pa_schema)
        assert cols[0].nullable is False
        assert cols[1].nullable is True

    def test_field_names_preserved(self):
        pa_schema = pa.schema([
            pa.field('order_id', pa.int64()),
            pa.field('amount', pa.decimal128(10, 2)),
        ])
        cols = pyarrow_to_columns(pa_schema)
        assert [c.name for c in cols] == ['order_id', 'amount']

    def test_rejects_non_schema_input(self):
        with pytest.raises(TypeError, match="pyarrow.Schema"):
            pyarrow_to_columns([('x', 'bigint')])  # not a Schema


# =============================================================================
# polyris → pyarrow
# =============================================================================

class TestColumnsToPyarrow:

    def test_simple_columns_round_trip_back_to_pyarrow(self):
        cols = [
            Column('id', s.bigint(), nullable=False),
            Column('amount', s.decimal(10, 2)),
            Column('tags', s.array(s.string())),
        ]
        pa_schema = columns_to_pyarrow(cols)
        assert pa_schema.field('id').type == pa.int64()
        assert pa_schema.field('amount').type == pa.decimal128(10, 2)
        assert pa_schema.field('tags').type == pa.list_(pa.string())

    def test_nullability_propagates(self):
        cols = [
            Column('a', s.bigint(), nullable=False),
            Column('b', s.bigint(), nullable=True),
        ]
        pa_schema = columns_to_pyarrow(cols)
        assert pa_schema.field('a').nullable is False
        assert pa_schema.field('b').nullable is True

    def test_description_attached_as_metadata(self):
        cols = [Column('x', s.bigint(), description="Primary key")]
        pa_schema = columns_to_pyarrow(cols)
        meta = pa_schema.field('x').metadata or {}
        assert meta.get(b'description') == b'Primary key'

    def test_empty_description_omits_metadata(self):
        cols = [Column('x', s.bigint())]
        pa_schema = columns_to_pyarrow(cols)
        # No metadata at all is acceptable; if present, must not contain
        # a description key.
        meta = pa_schema.field('x').metadata
        if meta is not None:
            assert b'description' not in meta

    def test_timestamp_tz_aware_uses_utc(self):
        cols = [Column('t', s.timestamp(tz_aware=True))]
        pa_schema = columns_to_pyarrow(cols)
        assert pa_schema.field('t').type == pa.timestamp('us', tz='UTC')

    def test_uuid_maps_to_fixed_binary_16(self):
        # Iceberg convention; test guards against accidental drift.
        cols = [Column('u', s.uuid())]
        pa_schema = columns_to_pyarrow(cols)
        assert pa_schema.field('u').type == pa.binary(16)

    def test_struct_round_trip(self):
        cols = [Column('point', s.struct(x=s.bigint(), y=s.string()))]
        pa_schema = columns_to_pyarrow(cols)
        # Convert back and verify equality.
        round_trip = pyarrow_to_columns(pa_schema)
        assert round_trip[0].type == s.struct(x=s.bigint(), y=s.string())


# =============================================================================
# Round-trip — write all 21 simple type names through both directions
# =============================================================================

class TestRoundTrip:

    @pytest.mark.parametrize("polyris_type", [
        s.tinyint(), s.smallint(), s.integer(), s.bigint(),
        s.float_(), s.double(), s.boolean(),
        s.decimal(10, 2), s.decimal(38, 0),
        s.string(), s.binary(), s.fixed_binary(16),
        s.date(), s.timestamp(), s.timestamp_ntz(),
        s.array(s.string()), s.array(s.bigint()),
        s.map_(s.string(), s.bigint()),
        s.struct(x=s.bigint(), y=s.string()),
    ])
    def test_round_trip_preserves_type(self, polyris_type):
        original = [Column('c', polyris_type)]
        pa_schema = columns_to_pyarrow(original)
        restored = pyarrow_to_columns(pa_schema)
        assert restored[0].type == polyris_type, (
            f"Round-trip changed: {polyris_type!r} -> {restored[0].type!r}"
        )

    @pytest.mark.parametrize("lossy_type,collapses_to", [
        (s.varchar(255), s.string()),  # pyarrow has no length-bounded variant
        (s.char(10),     s.string()),  # same
        (s.uuid(),       s.fixed_binary(16)),  # Iceberg convention
        (s.json_(),      s.string()),  # no JSON in pyarrow
        (s.time(),       s.time()),    # round-trips fine
    ])
    def test_documented_lossy_round_trip(self, lossy_type, collapses_to):
        original = [Column('c', lossy_type)]
        pa_schema = columns_to_pyarrow(original)
        restored = pyarrow_to_columns(pa_schema)
        assert restored[0].type == collapses_to


# =============================================================================
# Asset.from_pyarrow integration
# =============================================================================

class TestAssetFromPyarrow:

    def test_basic_construction(self):
        from polyris import Asset
        pa_schema = pa.schema([
            pa.field('id', pa.int64()),
            pa.field('amount', pa.decimal128(10, 2)),
        ])
        a = Asset.from_pyarrow(pa_schema, name='orders')
        assert a.name == 'orders'
        assert len(a.schema) == 2
        assert a.schema[0].type == s.bigint()
        assert a.schema[1].type == s.decimal(10, 2)

    def test_passes_kwargs_through(self):
        from polyris import Asset
        pa_schema = pa.schema([pa.field('id', pa.int64())])
        a = Asset.from_pyarrow(
            pa_schema,
            name='orders',
            owner='data-team',
            glue_table='analytics.orders',
            description='Test',
        )
        assert a.owner == 'data-team'
        assert a.glue_table == 'analytics.orders'
        assert a.description == 'Test'

    def test_rejects_explicit_schema_kwarg(self):
        from polyris import Asset
        pa_schema = pa.schema([pa.field('id', pa.int64())])
        with pytest.raises(TypeError, match='from_pyarrow'):
            Asset.from_pyarrow(pa_schema, name='x', schema=[])


# =============================================================================
# Asset.from_parquet integration — convenience over from_pyarrow that reads
# the Parquet footer and forwards the resulting pyarrow schema.
# =============================================================================

class TestAssetFromParquet:

    def _write_parquet(self, tmp_path, pa_schema):
        """Write an empty parquet file with the given schema. Footer-only — no rows."""
        import pyarrow.parquet as pq
        path = tmp_path / "sample.parquet"
        # Build an empty table that matches the schema and write it out.
        table = pa.Table.from_arrays(
            [pa.array([], type=f.type) for f in pa_schema],
            schema=pa_schema,
        )
        pq.write_table(table, path)
        return str(path)

    def test_reads_schema_from_local_parquet(self, tmp_path):
        from polyris import Asset
        pa_schema = pa.schema([
            pa.field('id', pa.int64()),
            pa.field('amount', pa.decimal128(10, 2)),
            pa.field('created_at', pa.timestamp('us', tz='UTC')),
        ])
        path = self._write_parquet(tmp_path, pa_schema)

        a = Asset.from_parquet(path, name='retail/orders')

        assert a.name == 'retail/orders'
        assert len(a.schema) == 3
        assert a.schema[0].name == 'id'
        assert a.schema[0].type == s.bigint()
        assert a.schema[1].type == s.decimal(10, 2)
        assert a.schema[2].type == s.timestamp(tz_aware=True)

    def test_passes_kwargs_through(self, tmp_path):
        from polyris import Asset
        pa_schema = pa.schema([pa.field('id', pa.int64())])
        path = self._write_parquet(tmp_path, pa_schema)

        a = Asset.from_parquet(
            path,
            name='retail/orders',
            owner='data-team',
            glue_table='analytics.orders',
            description='Test',
        )
        assert a.owner == 'data-team'
        assert a.glue_table == 'analytics.orders'
        assert a.description == 'Test'

    def test_rejects_explicit_schema_kwarg(self, tmp_path):
        from polyris import Asset
        pa_schema = pa.schema([pa.field('id', pa.int64())])
        path = self._write_parquet(tmp_path, pa_schema)
        with pytest.raises(TypeError, match='from_parquet'):
            Asset.from_parquet(path, name='x', schema=[])

    def test_default_name_from_path_basename(self, tmp_path):
        """No `name=` → fallback to file basename without extension.

        Mirrors the ergonomic of from_pydantic (model class name) and
        from_glue_table (db.table). Convenient for prototypes; production
        code should pass an explicit name.
        """
        from polyris import Asset
        pa_schema = pa.schema([pa.field('id', pa.int64())])
        # Place the file at a known stem so we can assert on it.
        sub = tmp_path / "ignored_dir"
        sub.mkdir()
        path = sub / "orders.parquet"
        import pyarrow.parquet as pq
        pq.write_table(
            pa.Table.from_arrays([pa.array([], type=pa.int64())], schema=pa_schema),
            str(path),
        )
        a = Asset.from_parquet(str(path))
        assert a.name == 'orders'

    def test_explicit_name_wins_over_fallback(self, tmp_path):
        """Explicit `name=` is not overridden by the basename fallback."""
        from polyris import Asset
        pa_schema = pa.schema([pa.field('id', pa.int64())])
        path = self._write_parquet(tmp_path, pa_schema)  # writes "sample.parquet"
        a = Asset.from_parquet(path, name='retail/orders')
        assert a.name == 'retail/orders'
        # And group should auto-derive from the slash, as for any Asset.
        assert a.group == 'retail'

    def test_missing_pyarrow_extra_raises_clear_error(self, tmp_path, mocker):
        """When pyarrow is unavailable, the lazy import should surface a
        clear ImportError that names the extra to install."""
        from polyris import Asset
        # Patch the lazy importer to simulate a missing pyarrow install.
        mocker.patch(
            'polyris.adapters.pyarrow_._require_pyarrow',
            side_effect=ImportError(
                "pyarrow is required for polyris.adapters.pyarrow_. "
                "Install with:  pip install 'polyris[pyarrow]'"
            ),
        )
        with pytest.raises(ImportError, match=r"polyris\[pyarrow\]"):
            Asset.from_parquet('whatever.parquet', name='x')
