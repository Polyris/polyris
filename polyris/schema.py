"""
Asset schema type system.

Platform-agnostic typed representation of column schemas for assets.
Single source of truth for all schema processing — used by Asset DSL,
asset serialization, generators, backend, and UI.

The internal type system is independent of any particular catalog
(Glue, BigQuery, Iceberg, ...). Catalog-specific string representations
are produced by adapter functions (`to_glue_string`).

Type instances are frozen dataclasses — immutable, hashable, and comparable
via `==`. Drift detection, conflict detection, and round-trip serialization
all rely on native Python equality without re-parsing strings.

Public API
----------
Type factory functions
    tinyint, smallint, integer, bigint
    float_, double, decimal
    boolean
    string, varchar, char
    binary, fixed_binary
    date, time, timestamp, timestamp_ntz
    uuid
    json_
    array, struct, map_

Column class
    Column(name, type, description="", nullable=True,
           primary_key=False, partition_key=False, unique=False, default=None)

Normalization
    normalize_schema(raw) -> List[Column]
        Accepts: list of Column instances, tuples (name, type[, description]),
        or dicts with name/type/description/... keys.
        Always returns List[Column]. Single normalization point.

Serialization
    column_to_dict(column) -> dict
    column_from_dict(d) -> Column
    to_glue_string(t: PolyrisType) -> str
    type_from_string(s: str) -> PolyrisType   (Glue-format parser)

Backward compatibility
    Old `schema=[("col", "bigint")]` and `schema=[{"name": ..., "type": ...}]`
    declarations continue to work — they normalize to typed Column instances.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


# =============================================================================
# Type system — frozen dataclasses, hashable, comparable
# =============================================================================

@dataclass(frozen=True)
class PolyrisType:
    """Marker base class for all schema types.

    Empty by design — type-specific behaviour lives on subclasses, and
    platform-specific conversion lives in adapter functions (not on the type).
    """


# Integer types ---------------------------------------------------------------

@dataclass(frozen=True)
class TinyIntType(PolyrisType):
    """8-bit signed integer (-128..127). Glue: tinyint. Iceberg/BigQuery: → int."""


@dataclass(frozen=True)
class SmallIntType(PolyrisType):
    """16-bit signed integer (-32768..32767). Glue: smallint."""


@dataclass(frozen=True)
class IntType(PolyrisType):
    """32-bit signed integer. Glue: int. BigQuery: INT64 (upcast)."""


@dataclass(frozen=True)
class BigIntType(PolyrisType):
    """64-bit signed integer. Glue: bigint. BigQuery: INT64."""


# Floating-point types --------------------------------------------------------

@dataclass(frozen=True)
class FloatType(PolyrisType):
    """32-bit floating-point. Glue: float. BigQuery: FLOAT64 (upcast)."""


@dataclass(frozen=True)
class DoubleType(PolyrisType):
    """64-bit floating-point. Glue: double. BigQuery: FLOAT64."""


# Fixed-precision -------------------------------------------------------------

@dataclass(frozen=True)
class DecimalType(PolyrisType):
    """Fixed-precision decimal. Glue: decimal(p, s)."""
    precision: int
    scale: int

    def __post_init__(self) -> None:
        if not (1 <= self.precision <= 38):
            raise ValueError(
                f"decimal precision must be 1..38, got {self.precision}"
            )
        if not (0 <= self.scale <= self.precision):
            raise ValueError(
                f"decimal scale must be 0..{self.precision} (precision), got {self.scale}"
            )


# Boolean ---------------------------------------------------------------------

@dataclass(frozen=True)
class BooleanType(PolyrisType):
    """Boolean. Glue: boolean. BigQuery: BOOL."""


# String-like -----------------------------------------------------------------

@dataclass(frozen=True)
class StringType(PolyrisType):
    """Variable-length unicode string with no maximum. Glue: string."""


@dataclass(frozen=True)
class VarcharType(PolyrisType):
    """Variable-length string with a maximum. Glue: varchar(length)."""
    length: int

    def __post_init__(self) -> None:
        if self.length < 1:
            raise ValueError(f"varchar length must be >= 1, got {self.length}")


@dataclass(frozen=True)
class CharType(PolyrisType):
    """Fixed-length string, padded with spaces. Glue: char(length)."""
    length: int

    def __post_init__(self) -> None:
        if not (1 <= self.length <= 255):
            raise ValueError(f"char length must be 1..255, got {self.length}")


# Binary-like -----------------------------------------------------------------

@dataclass(frozen=True)
class BinaryType(PolyrisType):
    """Variable-length byte array. Glue: binary."""


@dataclass(frozen=True)
class FixedBinaryType(PolyrisType):
    """Fixed-length byte array. Glue: fixed_size_binary(length). Iceberg: fixed(length)."""
    length: int

    def __post_init__(self) -> None:
        if self.length < 1:
            raise ValueError(f"fixed_binary length must be >= 1, got {self.length}")


# Date / time -----------------------------------------------------------------

@dataclass(frozen=True)
class DateType(PolyrisType):
    """Calendar date (no time component). Glue: date."""


@dataclass(frozen=True)
class TimeType(PolyrisType):
    """Time of day (no date component). BigQuery: TIME. Iceberg: time. Glue: → string."""


@dataclass(frozen=True)
class TimestampType(PolyrisType):
    """Timestamp.

    tz_aware=True (default) → BigQuery TIMESTAMP, Iceberg timestamptz.
    tz_aware=False → BigQuery DATETIME, Iceberg timestamp, Glue timestamp.
    """
    tz_aware: bool = True


# Identifiers -----------------------------------------------------------------

@dataclass(frozen=True)
class UuidType(PolyrisType):
    """UUID. Iceberg first-class. Glue/BigQuery: → string fallback."""


# Semi-structured -------------------------------------------------------------

@dataclass(frozen=True)
class JsonType(PolyrisType):
    """JSON value. BigQuery: JSON. Glue/Iceberg: → string fallback."""


# Nested types ----------------------------------------------------------------

@dataclass(frozen=True)
class ArrayType(PolyrisType):
    """Array of homogeneous type. Glue: array<inner>."""
    inner: PolyrisType


@dataclass(frozen=True)
class StructType(PolyrisType):
    """Struct with named, ordered fields. Glue: struct<f1:t1, f2:t2, ...>.

    Fields are stored as a tuple of (name, type) pairs to remain hashable.
    """
    fields: Tuple[Tuple[str, PolyrisType], ...]

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("struct must have at least one field")
        seen: set = set()
        for name, _ in self.fields:
            if not isinstance(name, str) or not name:
                raise ValueError(f"struct field name must be non-empty string, got {name!r}")
            if name in seen:
                raise ValueError(f"duplicate struct field name: {name!r}")
            seen.add(name)


@dataclass(frozen=True)
class MapType(PolyrisType):
    """Map from key type to value type. Glue: map<key, value>."""
    key: PolyrisType
    value: PolyrisType


# =============================================================================
# Type factory functions — public API for ergonomic construction
# =============================================================================

def tinyint() -> TinyIntType:
    return TinyIntType()


def smallint() -> SmallIntType:
    return SmallIntType()


def integer() -> IntType:
    """32-bit integer. Named `integer` to avoid shadowing Python's `int` builtin."""
    return IntType()


def bigint() -> BigIntType:
    return BigIntType()


def float_() -> FloatType:
    """32-bit float. Trailing underscore to avoid shadowing Python's `float` builtin."""
    return FloatType()


def double() -> DoubleType:
    return DoubleType()


def decimal(precision: int, scale: int = 0) -> DecimalType:
    return DecimalType(precision=precision, scale=scale)


def boolean() -> BooleanType:
    return BooleanType()


def string() -> StringType:
    return StringType()


def varchar(length: int) -> VarcharType:
    return VarcharType(length=length)


def char(length: int) -> CharType:
    return CharType(length=length)


def binary() -> BinaryType:
    return BinaryType()


def fixed_binary(length: int) -> FixedBinaryType:
    return FixedBinaryType(length=length)


def date() -> DateType:
    return DateType()


def time() -> TimeType:
    return TimeType()


def timestamp(tz_aware: bool = True) -> TimestampType:
    return TimestampType(tz_aware=tz_aware)


def timestamp_ntz() -> TimestampType:
    """Alias for `timestamp(tz_aware=False)`."""
    return TimestampType(tz_aware=False)


def uuid() -> UuidType:
    return UuidType()


def json_() -> JsonType:
    """JSON value. Trailing underscore to avoid shadowing the `json` stdlib module."""
    return JsonType()


def array(inner: PolyrisType) -> ArrayType:
    if not isinstance(inner, PolyrisType):
        raise TypeError(f"array inner must be a PolyrisType, got {type(inner).__name__}")
    return ArrayType(inner=inner)


def struct(fields: Optional[Mapping[str, PolyrisType]] = None, /, **kwargs: PolyrisType) -> StructType:
    """Build a struct type.

    Either pass a mapping positionally (preserves order in Python 3.7+) or use
    keyword arguments:

        struct({"x": integer(), "y": string()})
        struct(x=integer(), y=string())
    """
    if fields is not None and kwargs:
        raise TypeError("struct() accepts either a mapping OR keyword arguments, not both")
    items = fields if fields is not None else kwargs
    if not items:
        raise ValueError("struct must have at least one field")
    pairs: List[Tuple[str, PolyrisType]] = []
    for name, t in items.items():
        if not isinstance(t, PolyrisType):
            raise TypeError(f"struct field {name!r} must be a PolyrisType, got {type(t).__name__}")
        pairs.append((name, t))
    return StructType(fields=tuple(pairs))


def map_(key: PolyrisType, value: PolyrisType) -> MapType:
    """Build a map type. Trailing underscore to avoid shadowing the `map` builtin."""
    if not isinstance(key, PolyrisType):
        raise TypeError(f"map key must be a PolyrisType, got {type(key).__name__}")
    if not isinstance(value, PolyrisType):
        raise TypeError(f"map value must be a PolyrisType, got {type(value).__name__}")
    return MapType(key=key, value=value)


# =============================================================================
# Column — schema entry with optional constraints
# =============================================================================

# Column constraint defaults — the canonical baseline. Anything matching this
# is omitted from serialized output to keep storage compact and snapshot tests
# stable for unchanged declarations.
_COLUMN_DEFAULTS: Dict[str, Any] = {
    "description": "",
    "nullable": True,
    "primary_key": False,
    "partition_key": False,
    "unique": False,
    "default": None,
}


@dataclass(frozen=True)
class Column:
    """A schema column.

    Args:
        name: Column name (non-empty string).
        type: PolyrisType instance.
        description: Human-readable description.
        nullable: Whether the column can contain nulls. Default True.
        primary_key: Whether this column is part of the primary key. Default False.
        partition_key: Whether this column partitions the asset. Default False.
        unique: Whether values must be unique. Default False.
        default: Default value when null/missing. JSON-serializable scalar or None.
    """
    name: str
    type: PolyrisType
    description: str = ""
    nullable: bool = True
    primary_key: bool = False
    partition_key: bool = False
    unique: bool = False
    default: Optional[Union[str, int, float, bool]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(f"Column name must be a non-empty string, got {self.name!r}")
        if not isinstance(self.type, PolyrisType):
            raise TypeError(
                f"Column.type must be a PolyrisType instance, got {type(self.type).__name__}. "
                f"Use a factory function like `bigint()`, `string()`, `decimal(10, 2)`."
            )

    def __repr__(self) -> str:
        """Concise, default-eliding repr for REPL ergonomics.

        The dataclass-generated repr lists every field including all six
        defaults, which makes a 5-column schema unreadable at the REPL.
        This shows only fields that diverge from the default — the same
        compaction that `column_to_dict` applies for storage.

        Examples:
            Column('id', bigint, primary_key=True, nullable=False)
            Column('amount', decimal(10,2), description='USD')
            Column('status', string)
        """
        parts = [repr(self.name), to_glue_string(self.type)]
        for key, default in _COLUMN_DEFAULTS.items():
            value = getattr(self, key)
            if value != default:
                parts.append(f"{key}={value!r}")
        return f"Column({', '.join(parts)})"


# A schema is just an ordered list of columns. Exposed as a name for type hints.
Schema = List[Column]


# =============================================================================
# Glue-format string conversion (canonical wire format for catalogs and storage)
# =============================================================================

def to_glue_string(t: PolyrisType) -> str:
    """Render a type as a Glue / Hive-style type string.

    This is the canonical wire format used in pipeline_registry storage and
    in API responses to the UI. It matches AWS Glue Catalog conventions, which
    are also accepted by Athena, Spark, and Trino.
    """
    if isinstance(t, TinyIntType):
        return "tinyint"
    if isinstance(t, SmallIntType):
        return "smallint"
    if isinstance(t, IntType):
        return "int"
    if isinstance(t, BigIntType):
        return "bigint"
    if isinstance(t, FloatType):
        return "float"
    if isinstance(t, DoubleType):
        return "double"
    if isinstance(t, DecimalType):
        return f"decimal({t.precision},{t.scale})"
    if isinstance(t, BooleanType):
        return "boolean"
    if isinstance(t, StringType):
        return "string"
    if isinstance(t, VarcharType):
        return f"varchar({t.length})"
    if isinstance(t, CharType):
        return f"char({t.length})"
    if isinstance(t, BinaryType):
        return "binary"
    if isinstance(t, FixedBinaryType):
        return f"fixed_size_binary({t.length})"
    if isinstance(t, DateType):
        return "date"
    if isinstance(t, TimeType):
        return "time"
    if isinstance(t, TimestampType):
        # Glue itself has only `timestamp`; the tz suffix is a polyris extension
        # so the typed information survives a round trip.
        return "timestamp" if t.tz_aware else "timestamp_ntz"
    if isinstance(t, UuidType):
        return "uuid"
    if isinstance(t, JsonType):
        return "json"
    if isinstance(t, ArrayType):
        return f"array<{to_glue_string(t.inner)}>"
    if isinstance(t, StructType):
        inner = ",".join(f"{name}:{to_glue_string(ft)}" for name, ft in t.fields)
        return f"struct<{inner}>"
    if isinstance(t, MapType):
        return f"map<{to_glue_string(t.key)},{to_glue_string(t.value)}>"
    raise TypeError(f"Unknown PolyrisType: {type(t).__name__}")


# Mapping of simple (non-parametric) Glue type strings to factories.
# Aliases (e.g. `integer` for `int`, `long` for `bigint`) are intentional and
# match Hive/Athena tolerances so user input does not need to be canonicalized.
_SIMPLE_TYPE_PARSERS: Dict[str, PolyrisType] = {
    "tinyint": TinyIntType(),
    "byte": TinyIntType(),
    "smallint": SmallIntType(),
    "short": SmallIntType(),
    "int": IntType(),
    "integer": IntType(),
    "bigint": BigIntType(),
    "long": BigIntType(),
    "float": FloatType(),
    "real": FloatType(),
    "double": DoubleType(),
    "boolean": BooleanType(),
    "bool": BooleanType(),
    "string": StringType(),
    "binary": BinaryType(),
    "date": DateType(),
    "time": TimeType(),
    "timestamp": TimestampType(tz_aware=True),
    "timestamp_ntz": TimestampType(tz_aware=False),
    "datetime": TimestampType(tz_aware=False),
    "uuid": UuidType(),
    "json": JsonType(),
}


def type_from_string(s: str) -> PolyrisType:
    """Parse a Glue / Hive-style type string into a PolyrisType.

    Inverse of `to_glue_string`. Accepts canonical and common-alias forms.

    Examples:
        "bigint"             -> BigIntType()
        "decimal(10,2)"      -> DecimalType(10, 2)
        "varchar(255)"       -> VarcharType(255)
        "array<string>"      -> ArrayType(StringType())
        "struct<x:int,y:string>" -> StructType((("x", IntType()), ("y", StringType())))
        "map<string,bigint>" -> MapType(StringType(), BigIntType())
    """
    if not isinstance(s, str) or not s:
        raise ValueError(f"Type string must be non-empty, got {s!r}")
    text = s.strip().lower()

    # Simple types (no parameters, no nesting)
    simple = _SIMPLE_TYPE_PARSERS.get(text)
    if simple is not None:
        return simple

    # Parametric / nested types — dispatch by prefix
    if text.startswith("decimal(") and text.endswith(")"):
        body = text[len("decimal("):-1]
        parts = [p.strip() for p in body.split(",")]
        if len(parts) == 1:
            return DecimalType(precision=int(parts[0]), scale=0)
        if len(parts) == 2:
            return DecimalType(precision=int(parts[0]), scale=int(parts[1]))
        raise ValueError(f"decimal accepts 1 or 2 args, got {len(parts)}: {s!r}")

    if text.startswith("varchar(") and text.endswith(")"):
        return VarcharType(length=int(text[len("varchar("):-1]))

    if text.startswith("char(") and text.endswith(")"):
        return CharType(length=int(text[len("char("):-1]))

    if text.startswith("fixed_size_binary(") and text.endswith(")"):
        return FixedBinaryType(length=int(text[len("fixed_size_binary("):-1]))
    if text.startswith("fixed(") and text.endswith(")"):
        return FixedBinaryType(length=int(text[len("fixed("):-1]))

    if text.startswith("array<") and text.endswith(">"):
        return ArrayType(inner=type_from_string(text[len("array<"):-1]))

    if text.startswith("map<") and text.endswith(">"):
        body = text[len("map<"):-1]
        key_str, value_str = _split_top_level_comma(body, expected=2)
        return MapType(key=type_from_string(key_str), value=type_from_string(value_str))

    if text.startswith("struct<") and text.endswith(">"):
        body = text[len("struct<"):-1]
        parts = _split_top_level_comma(body)
        fields: List[Tuple[str, PolyrisType]] = []
        for part in parts:
            # Accept "name:type" (Hive/Glue) and "name type" (some SQL dialects).
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:\s]\s*(.+)$", part)
            if not m:
                raise ValueError(f"Invalid struct field syntax: {part!r} (in {s!r})")
            fields.append((m.group(1), type_from_string(m.group(2))))
        return StructType(fields=tuple(fields))

    raise ValueError(f"Unknown type string: {s!r}")


def _split_top_level_comma(text: str, expected: Optional[int] = None) -> List[str]:
    """Split a string on commas that are not inside <...> or (...).

    Used to parse argument lists in nested types like `map<string, array<int>>`
    where naive split(",") would break on the comma inside `array<int>`.
    """
    parts: List[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    if expected is not None and len(parts) != expected:
        raise ValueError(
            f"Expected {expected} top-level comma-separated parts, got {len(parts)}: {text!r}"
        )
    return parts


# =============================================================================
# Column ↔ dict serialization
# =============================================================================

def column_to_dict(col: Column) -> Dict[str, Any]:
    """Serialize a Column to a JSON-safe dict.

    Fields that match defaults are omitted to keep storage compact and to keep
    snapshot tests stable for declarations that did not opt into the new
    constraint fields.
    """
    out: Dict[str, Any] = {
        "name": col.name,
        "type": to_glue_string(col.type),
    }
    for key, default_value in _COLUMN_DEFAULTS.items():
        value = getattr(col, key)
        if value != default_value:
            out[key] = value
    return out


def column_from_dict(d: Mapping[str, Any]) -> Column:
    """Deserialize a Column from a JSON dict produced by `column_to_dict`.

    Tolerant to missing optional fields (uses defaults) and to old-format
    dicts that have only `name`/`type` (and optionally `description`).
    """
    name = d.get("name")
    type_str = d.get("type")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Column dict missing/invalid 'name': {dict(d)!r}")
    if not isinstance(type_str, str) or not type_str:
        raise ValueError(f"Column dict missing/invalid 'type': {dict(d)!r}")

    kwargs: Dict[str, Any] = {"name": name, "type": type_from_string(type_str)}
    for key in _COLUMN_DEFAULTS:
        if key in d and d[key] is not None:
            kwargs[key] = d[key]
    return Column(**kwargs)


# =============================================================================
# normalize_schema — single normalization point for all input formats
# =============================================================================

# Type alias for the union of accepted input shapes — kept as `Any` in public
# signatures to avoid leaking complex Union types into user-facing code.
RawColumn = Union[
    Column,
    Tuple[str, Union[str, PolyrisType]],
    Tuple[str, Union[str, PolyrisType], str],
    Mapping[str, Any],
]


def normalize_schema(raw: Optional[Sequence[Any]]) -> Schema:
    """Normalize any accepted schema declaration to `List[Column]`.

    Accepted input shapes per entry:
        - Column instance (passed through)
        - Tuple (name, type)              — type is str or PolyrisType
        - Tuple (name, type, description) — type is str or PolyrisType
        - Mapping with at least 'name' and 'type' keys; remaining keys are
          forwarded to Column where they match Column field names.

    Returns:
        Schema (alias for List[Column]). Empty list when input is None/empty.

    Raises:
        TypeError if an entry has an unsupported shape.
        ValueError if a tuple has fewer than 2 elements, a dict lacks
            required keys, or two columns share the same name.
    """
    if not raw:
        return []
    if isinstance(raw, (str, bytes, Mapping)):
        # Reject these explicitly — easy mistakes that would otherwise iterate
        # over characters or dict keys and produce confusing errors deep below.
        raise TypeError(
            f"schema must be a list of columns, got {type(raw).__name__}"
        )

    out: Schema = []
    for entry in raw:
        out.append(_normalize_entry(entry))

    # Reject duplicate column names. Catalog systems (Glue, Iceberg,
    # BigQuery) all reject duplicates; failing here at deploy-time on the
    # developer's machine is far friendlier than failing during the first
    # CREATE TABLE call in production. Comparison is case-sensitive
    # because Glue, Iceberg, and BigQuery all preserve case in column
    # names — `ID` and `id` are distinct everywhere we care about.
    seen: Dict[str, int] = {}
    for i, col in enumerate(out):
        if col.name in seen:
            raise ValueError(
                f"Duplicate column name in schema: {col.name!r} appears at "
                f"positions {seen[col.name]} and {i}. "
                f"Catalog systems (Glue, Iceberg, BigQuery) reject duplicate "
                f"column names; rename one of the columns."
            )
        seen[col.name] = i
    return out


def _normalize_entry(entry: Any) -> Column:
    """Normalize a single schema entry to a Column instance."""
    if isinstance(entry, Column):
        return entry

    if isinstance(entry, (list, tuple)):
        if len(entry) < 2:
            raise ValueError(
                f"schema tuple must have at least 2 elements (name, type), got {len(entry)}: {entry!r}"
            )
        name = entry[0]
        type_arg = entry[1]
        type_obj = type_arg if isinstance(type_arg, PolyrisType) else type_from_string(type_arg)
        description = entry[2] if len(entry) > 2 and entry[2] is not None else ""
        return Column(name=name, type=type_obj, description=description)

    if isinstance(entry, Mapping):
        return column_from_dict(entry)

    raise TypeError(
        f"schema entry must be Column / tuple / dict, got {type(entry).__name__}: {entry!r}"
    )


# =============================================================================
# Conflict detection — used by backend when the same asset is declared in
# multiple pipelines. Single source of truth so we do not reimplement this
# inside backend route code.
# =============================================================================

def schema_richness(schema: Schema) -> int:
    """Score how 'rich' a schema declaration is.

    Used to pick a winner when the same asset is declared with different
    schemas in different pipelines. Higher score wins. The score rewards:

      - Number of columns (more columns = more information)
      - Number of constraint fields set (PK, partition, etc.)
      - Description text presence

    A schema with zero columns scores 0; an empty list is the natural loser.
    """
    score = 0
    for col in schema:
        score += 1
        for key, default in _COLUMN_DEFAULTS.items():
            if getattr(col, key) != default:
                score += 1
    return score


def dict_schema_richness(schema: Sequence[Mapping[str, Any]]) -> int:
    """Same scoring as `schema_richness`, but for the serialized dict form.

    Used by the backend asset conflict-resolution path, which works with
    already-serialized schema dicts read from
    pipeline_registry. Avoids round-tripping each dict through
    `column_from_dict` on every pipeline build.

    A dict-form column is "rich" when it carries any non-default
    constraint key. Defaults match `_COLUMN_DEFAULTS` and the omit-on-default
    serialization in `column_to_dict`, so a freshly-serialized
    Column-with-PK and a hand-written `{"name": ..., "type": ..., "primary_key": True}`
    score identically.
    """
    score = 0
    for col in schema or ():
        if not isinstance(col, Mapping):
            continue  # be defensive — backend may receive malformed entries
        score += 1
        for key, default in _COLUMN_DEFAULTS.items():
            if key in col and col[key] != default and col[key] is not None:
                score += 1
    return score


# =============================================================================
# JSON Schema (Draft 2020-12) export — used by Asset.to_jsonschema()
# =============================================================================

def _polyris_type_to_jsonschema(t: PolyrisType, nullable: bool) -> Dict[str, Any]:
    """Convert a PolyrisType to a JSON Schema fragment.

    Module-level so backend tooling (CLI, generators) can reuse it without
    importing from the assets module. `nullable` is wired through so we
    can attach ``"null"`` to the type union when appropriate.
    """
    def _ann(base: Dict[str, Any]) -> Dict[str, Any]:
        """Attach `"null"` to the type union if column is nullable."""
        if not nullable:
            return base
        # JSON Schema convention: type can be a list including "null".
        existing = base.get("type")
        if isinstance(existing, str):
            base["type"] = [existing, "null"]
        elif isinstance(existing, list) and "null" not in existing:  # pragma: no cover -- no PolyrisType emits a list-typed base before _ann runs, so only the str branch above is reachable
            base["type"] = existing + ["null"]
        return base

    if isinstance(t, (TinyIntType, SmallIntType, IntType, BigIntType)):
        return _ann({"type": "integer"})
    if isinstance(t, (FloatType, DoubleType)):
        return _ann({"type": "number"})
    if isinstance(t, DecimalType):
        # JSON has no first-class decimal — use number with format hint
        # so consumers that care can opt in to parsing it strictly.
        return _ann({"type": "number", "format": f"decimal({t.precision},{t.scale})"})
    if isinstance(t, BooleanType):
        return _ann({"type": "boolean"})
    if isinstance(t, StringType):
        return _ann({"type": "string"})
    if isinstance(t, VarcharType):
        return _ann({"type": "string", "maxLength": t.length})
    if isinstance(t, CharType):
        return _ann({"type": "string", "minLength": t.length, "maxLength": t.length})
    if isinstance(t, UuidType):
        return _ann({"type": "string", "format": "uuid"})
    if isinstance(t, JsonType):
        # JSON-valued field — leave the value type open.
        return _ann({})
    if isinstance(t, DateType):
        return _ann({"type": "string", "format": "date"})
    if isinstance(t, TimeType):
        return _ann({"type": "string", "format": "time"})
    if isinstance(t, TimestampType):
        return _ann({"type": "string", "format": "date-time"})
    if isinstance(t, BinaryType):
        return _ann({"type": "string", "contentEncoding": "base64"})
    if isinstance(t, FixedBinaryType):
        return _ann({"type": "string", "contentEncoding": "base64",
                     "minLength": t.length, "maxLength": t.length})
    if isinstance(t, ArrayType):
        # Array items inherit the column's nullability — JSON Schema users
        # typically expect array contents to be non-null even when the
        # array itself is nullable.
        return _ann({"type": "array", "items": _polyris_type_to_jsonschema(t.inner, False)})
    if isinstance(t, MapType):
        # JSON objects always have string keys; surface non-string key types
        # via a propertyNames format hint without rejecting them.
        out: Dict[str, Any] = {
            "type": "object",
            "additionalProperties": _polyris_type_to_jsonschema(t.value, False),
        }
        if not isinstance(t.key, StringType):
            out["propertyNames"] = _polyris_type_to_jsonschema(t.key, False)
        return _ann(out)
    if isinstance(t, StructType):
        props: Dict[str, Any] = {}
        for name, ft in t.fields:
            props[name] = _polyris_type_to_jsonschema(ft, False)
        return _ann({"type": "object", "properties": props,
                     "required": [n for n, _ in t.fields]})

    raise TypeError(f"Unknown PolyrisType in JSON Schema export: {type(t).__name__}")


__all__ = [
    # Base + types
    "PolyrisType",
    "TinyIntType", "SmallIntType", "IntType", "BigIntType",
    "FloatType", "DoubleType", "DecimalType",
    "BooleanType",
    "StringType", "VarcharType", "CharType",
    "BinaryType", "FixedBinaryType",
    "DateType", "TimeType", "TimestampType",
    "UuidType", "JsonType",
    "ArrayType", "StructType", "MapType",
    # Factories
    "tinyint", "smallint", "integer", "bigint",
    "float_", "double", "decimal",
    "boolean",
    "string", "varchar", "char",
    "binary", "fixed_binary",
    "date", "time", "timestamp", "timestamp_ntz",
    "uuid", "json_",
    "array", "struct", "map_",
    # Column + Schema
    "Column", "Schema", "RawColumn",
    # Serialization + parsing
    "to_glue_string", "type_from_string",
    "column_to_dict", "column_from_dict",
    "normalize_schema", "schema_richness", "dict_schema_richness",
]
