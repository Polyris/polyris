# ADR #42 — Asset Schema 2.0: Typed `Column` Class with Platform-Agnostic Type System

**Decision:** Asset schemas are declared with a typed `Column` class and
factory-built type instances. Internal representation is a list of frozen
`Column` dataclasses; on-disk wire format is a Glue-compatible string per type.
Legacy tuple/dict declarations continue to work without warning.

**Context:** Until v0.71.x the schema field on `Asset` accepted only freeform
tuples (`("col", "bigint")`) or dicts (`{"name": ..., "type": ..., "description": ...}`).
The `type` was a freeform string with no validation, which had three concrete
problems:

1. **No IDE help.** `"BIGINT"`, `"int8"`, `"bigint"` were all accepted; users
   had no way to know which form was canonical until runtime.
2. **Parametric types were opaque strings.** `"decimal(10,2)"` had to be
   re-parsed on every comparison (drift detection, conflict detection,
   adapter conversion). Reversing this several times across the codebase
   was a bug magnet.
3. **No constraint metadata.** Nullability, primary keys, and partition keys
   could not be expressed at all — yet they are first-class concepts in every
   serious catalog (Glue, Iceberg, BigQuery, Snowflake).

**Industry analysis:** pyarrow, SQLAlchemy, Pandera, and PyIceberg all use
type instances/classes (factory-built) rather than freeform strings. Dagster
chose freeform strings for `TableColumn.type` and has open issues asking for
strict typing. We aligned with the convergent pattern across the four serious
data libraries, not the outlier.

**API:**

```python
from polyris import Asset, Column, types as t

orders = Asset(
    name="retail/orders",
    schema=[
        Column("order_id",   t.bigint(),       primary_key=True, nullable=False),
        Column("customer_id", t.bigint(),       nullable=False),
        Column("event_date", t.date(),         partition_key=True),
        Column("amount",     t.decimal(10, 2), description="USD amount"),
        Column("tags",       t.array(t.string())),
    ],
)
```

21 type classes cover Glue/Hive, Iceberg, BigQuery, and Snowflake primitives:
TinyInt/SmallInt/Int/BigInt, Float/Double/Decimal, Boolean, String/Varchar/Char,
Binary/FixedBinary, Date/Time/Timestamp, Uuid, Json, Array/Struct/Map.

**Single source of truth:**

- `polyris/schema.py` — type classes, factories, `Column`, `normalize_schema`,
  `column_to_dict`/`column_from_dict`, `to_glue_string`/`type_from_string`.
- `Asset.__init__` calls `normalize_schema(schema)` once. The `Asset.schema`
  attribute is always `List[Column]` internally.
- `Asset.to_dict()` and `_serialize_outlet()` both call `column_to_dict(c)` —
  no parallel serialization paths.
- The previous `Asset._serialize_schema()` method has been removed.

**Backward compatibility (non-breaking):**

- `schema=[("col", "bigint")]` and `schema=[("col", "bigint", "desc")]` continue
  to work. They normalize to typed `Column` instances internally.
- `schema=[{"name": "col", "type": "bigint"}]` continues to work.
- Mixed lists are allowed: a single schema can contain `Column` instances,
  tuples, and dicts.
- Wire format on disk: `column_to_dict` omits fields that match defaults, so
  for legacy declarations the serialized JSON is byte-identical to the previous
  output. All 60 ASL snapshot tests pass without regeneration.

**Conflict detection:** When the same asset is declared with different schemas
across multiple pipelines (one is a producer, another is consumer with its own
declaration), `_build_assets_from_pipelines` now picks the schema with more
columns and emits a `log.warn` with asset name, pipeline, and column counts.
Previously this was silent last-writer-wins.

**Out of scope for this decision (deferred):**

- Glue Catalog live sync (Phase 2 — requires Lambda + IAM `glue:GetTable`).
- Schema drift detection at materialization time (Phase 3 — requires producer
  SFN to emit actual schema in materialization events).
- Convenience constructors `Asset.from_pyarrow()`, `Asset.from_pydantic()`,
  `Asset.from_glue_table()` (separate phase, requires optional dependencies).
- Column-level lineage (Dagster+ paid feature; complex; not on near-term roadmap).
- UI features: copy as DDL, schema diff between materializations, search/filter
  columns (separate phase).

**Why type instances, not Glue strings, as internal representation?** Phase 2
(Glue sync) and Phase 3 (drift detection) will compare schemas hundreds of
times per minute. Native `dataclass` equality on frozen instances is O(1) and
correct without any parsing. With strings, every comparison re-parses
`"decimal(10,2)"` into `(10, 2)` to compare scale separately — slow, error-prone,
and a duplicate of the parsing already done at load time.

**Why our own types instead of pyarrow as internal?** Adding `pyarrow` as a
required dependency would push polyris's install footprint from ~5MB to ~35MB
(pyarrow ships compiled C++ extensions). polyris's only required dependency
is `boto3`, and we want to keep it that way. pyarrow becomes an *optional*
bridge in a later phase: a single adapter (`to_pyarrow` / `from_pyarrow`) gives
us access to Iceberg, BigQuery, Parquet, Polars, Pandas, DuckDB, and Spark
through pyarrow as a hub, all via converters those projects already maintain.
