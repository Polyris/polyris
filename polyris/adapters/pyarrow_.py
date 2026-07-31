"""pyarrow.Schema  ↔  List[polyris.Column].

Pyarrow is the de facto Python type system for tabular data — Iceberg,
BigQuery, Parquet, Polars, Pandas, and DuckDB all expose pyarrow.Schema
either natively or via a converter they themselves maintain. Bridging
polyris types to pyarrow once gets us all of those at no marginal cost.
This is the bridge pattern documented in ADR #42.

Public surface:
    pyarrow_to_columns(pa_schema) -> List[Column]
    columns_to_pyarrow(columns)   -> pa.Schema

The classmethod helpers `Asset.from_pyarrow(...)` and
`Asset.from_parquet(s3_or_local_path)` are thin wrappers around
`pyarrow_to_columns` (the latter reads a Parquet file's footer first).

Type mapping (pyarrow → polyris):

    int8/uint8         → tinyint        (uint8 fits in signed int8 range +
                                          overflow; documented as best-effort)
    int16/uint16       → smallint
    int32/uint32       → int
    int64/uint64       → bigint
    float16/float32    → float          (float16 widened to float)
    float64            → double
    bool               → boolean
    decimal128/256     → decimal(p, s)
    string/large_string/utf8/large_utf8  → string
    binary/large_binary                  → binary
    fixed_size_binary(n)                 → fixed_binary(n)
    date32/date64      → date
    time32/time64      → time
    timestamp(unit, tz=None)  → timestamp_ntz()
    timestamp(unit, tz=…)     → timestamp(tz_aware=True)
    list_/large_list/fixed_size_list → array(inner)
    map_                              → map(key, value)
    struct                            → struct(fields...)

Unsigned integer types map to the next-larger signed type would be lossless
but inflates storage; we instead map to the same width and accept that the
top of the unsigned range overflows. Users who need full uint64 range
should declare manually with a wider type.
"""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from ..schema import (
    Column, Schema, PolyrisType,
    array, bigint, binary, boolean, date, decimal, double,
    fixed_binary, float_, integer, map_, smallint, string, struct,
    time, timestamp, tinyint, ArrayType, BigIntType, BinaryType, BooleanType, CharType, DateType,
    DecimalType, DoubleType, FixedBinaryType, FloatType, IntType, JsonType,
    MapType, SmallIntType, StringType, StructType, TimeType, TimestampType,
    TinyIntType, UuidType, VarcharType,
)

if TYPE_CHECKING:
    import pyarrow as pa


# =============================================================================
# Lazy import — pyarrow is an optional dep
# =============================================================================

def _require_pyarrow():
    """Import pyarrow with a clear actionable error if missing.

    Returns the pyarrow module so callers can use it as `pa = _require_pyarrow()`.
    """
    try:
        import pyarrow as pa  # noqa: F401
        return pa
    except ImportError as e:  # pragma: no cover -- pyarrow is an installed optional dependency in CI; the not-installed guard is for end-user environments
        raise ImportError(
            "pyarrow is required for polyris.adapters.pyarrow_. "
            "Install with:  pip install 'polyris[pyarrow]'"
        ) from e


# =============================================================================
# pyarrow → polyris
# =============================================================================

def pyarrow_to_columns(pa_schema: "pa.Schema") -> Schema:
    """Convert a pyarrow.Schema to a list of polyris Column instances.

    Field nullability is carried over. Pyarrow has no notion of primary key,
    partition key, or unique — all defaults (False) on the Column side.
    Field metadata is not currently surfaced as Column.description; pyarrow
    metadata is freeform bytes and rarely contains a stable description
    convention. Add explicit descriptions on the Column constructor if needed.
    """
    pa = _require_pyarrow()
    if not isinstance(pa_schema, pa.Schema):
        raise TypeError(
            f"pyarrow_to_columns expects pyarrow.Schema, got {type(pa_schema).__name__}"
        )
    out: List[Column] = []
    for field in pa_schema:
        out.append(Column(
            name=field.name,
            type=_pa_type_to_polyris(field.type),
            nullable=bool(field.nullable),
        ))
    return out


def _pa_type_to_polyris(t: "pa.DataType") -> PolyrisType:
    """Map a single pyarrow DataType to a polyris type instance."""
    pa = _require_pyarrow()

    # Integer types — unsigned widths intentionally collapse to same-width
    # signed; see module docstring for rationale.
    if pa.types.is_int8(t) or pa.types.is_uint8(t):
        return tinyint()
    if pa.types.is_int16(t) or pa.types.is_uint16(t):
        return smallint()
    if pa.types.is_int32(t) or pa.types.is_uint32(t):
        return integer()
    if pa.types.is_int64(t) or pa.types.is_uint64(t):
        return bigint()

    # Floating-point — float16 widens to float32-equivalent (polyris has no f16).
    if pa.types.is_float16(t) or pa.types.is_float32(t):
        return float_()
    if pa.types.is_float64(t):
        return double()

    if pa.types.is_boolean(t):
        return boolean()

    if pa.types.is_decimal(t):
        return decimal(t.precision, t.scale)

    # Strings (canonical, large variants, and utf8 alias all collapse to string)
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return string()

    # Binary — fixed-size needs the FixedBinary type; everything else is binary
    if pa.types.is_fixed_size_binary(t):
        return fixed_binary(t.byte_width)
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return binary()

    if pa.types.is_date(t):
        # date32 vs date64 — polyris doesn't distinguish; both are calendar dates.
        return date()

    if pa.types.is_time(t):
        return time()

    if pa.types.is_timestamp(t):
        return timestamp(tz_aware=t.tz is not None)

    # Nested types
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        return array(_pa_type_to_polyris(t.value_type))
    if pa.types.is_fixed_size_list(t):
        # Polyris has no fixed-size array distinction; collapse to array.
        return array(_pa_type_to_polyris(t.value_type))

    if pa.types.is_map(t):
        return map_(_pa_type_to_polyris(t.key_type),
                    _pa_type_to_polyris(t.item_type))

    if pa.types.is_struct(t):
        fields = {}
        for i in range(t.num_fields):
            f = t.field(i)
            fields[f.name] = _pa_type_to_polyris(f.type)
        return struct(fields)

    # Dictionary — index/value compression; surface as the value type.
    if pa.types.is_dictionary(t):
        return _pa_type_to_polyris(t.value_type)

    raise TypeError(f"Unsupported pyarrow type: {t!r} ({type(t).__name__})")


# =============================================================================
# polyris → pyarrow
# =============================================================================

def columns_to_pyarrow(columns: List[Column]) -> "pa.Schema":
    """Convert a list of polyris Column instances to a pyarrow.Schema.

    Constraint fields (primary_key, partition_key, unique) have no pyarrow
    representation and are dropped. Description, if set, is attached as
    field metadata under key b'description' (utf-8 bytes — pyarrow convention).
    """
    pa = _require_pyarrow()
    fields = []
    for col in columns:
        metadata = None
        if col.description:
            metadata = {b"description": col.description.encode("utf-8")}
        fields.append(pa.field(
            col.name,
            _polyris_type_to_pa(col.type),
            nullable=col.nullable,
            metadata=metadata,
        ))
    return pa.schema(fields)


def _polyris_type_to_pa(t: PolyrisType) -> "pa.DataType":
    """Map a polyris type instance to a pyarrow DataType."""
    pa = _require_pyarrow()

    if isinstance(t, TinyIntType):
        return pa.int8()
    if isinstance(t, SmallIntType):
        return pa.int16()
    if isinstance(t, IntType):
        return pa.int32()
    if isinstance(t, BigIntType):
        return pa.int64()
    if isinstance(t, FloatType):
        return pa.float32()
    if isinstance(t, DoubleType):
        return pa.float64()
    if isinstance(t, BooleanType):
        return pa.bool_()
    if isinstance(t, DecimalType):
        return pa.decimal128(t.precision, t.scale)
    # Char/Varchar collapse to string in pyarrow — pyarrow has no length-bounded
    # variant. Round-trip will lose the length attribute; this is unavoidable
    # without inventing a parquet-incompatible extension type.
    if isinstance(t, (StringType, VarcharType, CharType)):
        return pa.string()
    if isinstance(t, FixedBinaryType):
        return pa.binary(t.length)
    if isinstance(t, BinaryType):
        return pa.binary()
    if isinstance(t, DateType):
        return pa.date32()
    if isinstance(t, TimeType):
        return pa.time64('us')
    if isinstance(t, TimestampType):
        return pa.timestamp('us', tz='UTC' if t.tz_aware else None)
    # UUID and JSON have no first-class pyarrow type. UUID → fixed_size_binary(16)
    # is the Iceberg convention; JSON → string is conventional.
    if isinstance(t, UuidType):
        return pa.binary(16)
    if isinstance(t, JsonType):
        return pa.string()
    if isinstance(t, ArrayType):
        return pa.list_(_polyris_type_to_pa(t.inner))
    if isinstance(t, MapType):
        return pa.map_(_polyris_type_to_pa(t.key),
                       _polyris_type_to_pa(t.value))
    if isinstance(t, StructType):
        return pa.struct([
            pa.field(name, _polyris_type_to_pa(ft))
            for name, ft in t.fields
        ])

    raise TypeError(f"Unsupported polyris type: {type(t).__name__}")


# =============================================================================
# Public re-exports
# =============================================================================

__all__ = ["pyarrow_to_columns", "columns_to_pyarrow"]
