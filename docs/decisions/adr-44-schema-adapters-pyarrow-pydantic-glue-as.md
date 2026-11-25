# ADR #44 — Schema Adapters: pyarrow / pydantic / Glue as `Asset.from_*` Constructors

**Decision:** Three classmethod constructors on `Asset` accept external
schema sources and produce ready-to-deploy assets:

  - `Asset.from_pyarrow(pa_schema, ...)` — bridge to Iceberg, Parquet,
    BigQuery, Polars, Pandas, DuckDB (anything with a pyarrow.Schema).
  - `Asset.from_pydantic(Model, ...)` — pydantic v2 BaseModel subclass
    becomes the schema declaration.
  - `Asset.from_glue_table("db.t", ...)` — fetch existing Glue table at
    deploy time as the source of truth.

Each constructor is a thin wrapper around an adapter module under
`polyris/adapters/` (`pyarrow_.py`, `pydantic_.py`, `glue.py`). Adapters
are independently importable.

**Context:** Phase 1 (v0.72) established the typed Column system but
required users to write each column by hand — even when the schema
already exists in their parquet sample, pydantic model, or Glue table.
This is busy work that quickly diverges from the source of truth: every
schema change has to be replicated in two places. The ergonomic gap
hurts adoption — Dagster has rich integrations of this kind via its
ecosystem packages.

**Why pyarrow as the bridge, not internal:** ADR #42 covers this. The
short version: pyarrow is the convergent type system for tabular data
in the Python ecosystem (Iceberg, Parquet, BigQuery, Polars, Pandas, and
DuckDB all expose or accept it), but its install footprint (~30MB of
compiled C++ extensions) is too large to make a required polyris
dependency. Bridging once via `pyarrow_to_columns` / `columns_to_pyarrow`
gets us all of those formats at zero marginal cost when the user opts in.

**Optional dependency mechanics:**

```toml
[project.optional-dependencies]
pyarrow  = ["pyarrow>=14.0.0"]
pydantic = ["pydantic>=2.0.0"]
all      = [..., "pyarrow>=14.0.0", "pydantic>=2.0.0"]
```

`import polyris` succeeds without either installed. The adapter modules
exist and are importable too — only the actual conversion functions
guard their peer import. Calling `pyarrow_to_columns` without
`pyarrow` installed raises:

    ImportError: pyarrow is required for polyris.adapters.pyarrow_.
    Install with:  pip install 'polyris[pyarrow]'

**Type-mapping decisions worth recording:**

For pyarrow → polyris:
  - Unsigned ints collapse to same-width signed (uint64 → bigint, with
    overflow at the top of the unsigned range). Promoting to wider types
    would inflate Glue/Iceberg storage; users needing full uint64 should
    declare manually.
  - `float16` widens to `float` (polyris has no float16).
  - `dictionary` collapses to its value type (compression detail, not a
    semantic type).
  - `fixed_size_list` collapses to `array` (polyris has no fixed-array
    distinction).

For polyris → pyarrow:
  - `varchar(N)` and `char(N)` both collapse to `string`. Pyarrow has no
    length-bounded string variant — this is unavoidable without inventing
    a parquet-incompatible extension type.
  - `uuid` maps to `binary(16)` (the Iceberg convention).
  - `json` maps to `string` (no JSON in pyarrow).
  - `timestamp(tz_aware=True)` always uses UTC; the original tz is not
    surfaced because we only carry "is the timestamp tz-aware", not which
    tz.

For pydantic → polyris:
  - `int` maps to `bigint`, not `integer`. Python's int is unbounded and
    a 32-bit landing would silently truncate at the catalog. Users wanting
    narrower types should use the explicit Column form.
  - `Decimal` maps to `decimal(38, 9)`. Largest portable precision across
    Glue/Iceberg/BigQuery; users can tighten via explicit Column.
  - `datetime` defaults to `tz_aware=False`. Pydantic does not infer
    tz-awareness from annotations.
  - Unions other than `Optional[T]` collapse to `string` (catalog-safe
    fallback). Mark the field nullable if `None` is in the union.
  - Default values for non-scalar types (list, dict, complex objects) are
    dropped, only JSON-safe scalars survive on `Column.default`.

For Glue → polyris:
  - Reuses `type_from_string` from ADR #42 — same parser, no duplication.
  - `Comment` becomes `Column.description`.
  - `PartitionKeys` are included with `partition_key=True`.

**Why three classmethods, not just one factory `Asset.from_(source)`?**
A single dispatcher would hide the adapter-specific kwargs (catalog_id,
region for Glue; nothing else for pyarrow; nothing for pydantic) and
make signatures harder to read. Three explicit methods keep call sites
self-documenting. Discoverability via IDE autocomplete also wins:
typing `Asset.from_` shows three obvious next steps.

**Why `polyris.adapters.pyarrow_` and `pydantic_` (trailing underscore)?**
Without it, `from polyris.adapters import pyarrow` would shadow the real
`pyarrow` package in caller scope. The trailing underscore is the
standard Python convention for "almost-name-but-keyword-conflict" cases.
The classmethods on Asset are the canonical user-facing API — direct
adapter use is for advanced cases.

**Out of scope (deferred):**

  - SQL DDL parsing (`Asset.from_sql_ddl("CREATE TABLE ...")`) — needs a
    SQL parser dependency or a hand-rolled parser. Rare use case; users
    who already have SQL DDL probably also have a Glue table from it.
  - `Asset.from_avro` / `Asset.from_protobuf` / `Asset.from_jsonschema` —
    each is its own dependency and design surface; add when a real user
    asks. The `from_pyarrow` bridge handles the common cases via the
    converters those projects already maintain.
  - Annotated type narrowing (`Annotated[int, FieldType.int32]`) for
    pydantic — would let users opt in to narrower integer / decimal
    types. Worth doing once the use case shows up.

**Shipped after v0.74.0:**

  - `Asset.from_parquet(path)` convenience wrapper over `from_pyarrow`
    landed in v0.74.1. Reads the Parquet footer via
    `pyarrow.parquet.read_schema(path)`; supports local paths and
    `s3://` URIs through pyarrow's built-in filesystems. Same `[pyarrow]`
    extra requirement as the underlying `from_pyarrow`.
