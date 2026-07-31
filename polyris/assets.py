"""
Asset classes for SFN-DSL.

Assets represent logical data units (files, tables, models) that tasks can produce or consume.
Enables cross-pipeline dependencies through EventBridge events.

Example:
    from sfn_dsl import DAG, task, Asset, Metadata
    
    # Define assets
    inventory = Asset("raw/inventory", uri="s3://bucket/raw/inventory/")
    processed = Asset("processed/inventory")
    
    # Producer pipeline
    with DAG(dag_id="acquisition", schedule="@daily") as dag:
        @task.sfn(arn="${inv_arn}", outlets=[inventory])
        def get_inventory():
            pass
    
    # Consumer pipeline (triggered when inventory is updated)
    with DAG(dag_id="processing", schedule=[inventory]) as dag:
        @task.sfn(arn="${proc_arn}", inlets=[inventory], outlets=[processed])
        def process_inventory():
            pass
    
    # Multiple assets with AND logic (all must be ready)
    with DAG(dag_id="dashboard", schedule=[asset_a & asset_b]) as dag:
        ...
    
    # OR logic (any one is enough)
    with DAG(dag_id="alerts", schedule=[asset_a | asset_b]) as dag:
        ...
    
    # Metadata - emitted with asset events (optional pattern for future use)
    # This is a declarative pattern - actual metadata emission happens in backend
    # via task output parsing or explicit API calls.
"""

import warnings
from typing import List, Literal, Optional, Dict, Any, Union, TYPE_CHECKING, cast
from dataclasses import dataclass, field

from .schema import (
    Schema, column_to_dict, normalize_schema,
    _polyris_type_to_jsonschema,
)


class ExperimentalWarning(UserWarning):
    """Emitted when using a feature whose API may still change (currently: assets).

    EXPERIMENTAL-ASSETS: this whole warning mechanism (class, module flag, and the
    warn() call in Asset.__init__) is temporary scaffolding. Remove it when assets
    graduate to stable — see docs/reference/EXPERIMENTAL_ASSETS.md.

    Assets work end to end (define, produce, wait on, asset-triggered schedules),
    but the API is not yet frozen and the visual asset console is not in the
    open-source build. Silence this warning with:

        import warnings, polyris
        warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)
    """


# Warn once per process: the message is the same for every Asset, so repeating it
# per construction is noise (define ten assets, get one warning, not ten).
_EXPERIMENTAL_WARNED = False


if TYPE_CHECKING:
    from .dag import DAG
    from .task import Task


# ──────────────────────────────────────────────────────────────────────────
# Partition granularity (ADR #50)
#
# Asset's `granularity` declares the natural cadence of its partitions —
# how often it produces a new slice of data. Used by the Matrix view to
# render the right column structure (daily columns vs weekly vs monthly),
# and by drift detection to flag assets that aren't materializing at the
# expected pace.
#
# `Literal` gives IDE autocomplete and type-check errors at edit time, so
# typos like `granularity="dayly"` fail before deploy. The set is closed
# (no `quarterly`/`yearly` yet) — adding values is a one-line change when
# real demand appears (CLAUDE.md #5 — extend, don't pre-build).
# ──────────────────────────────────────────────────────────────────────────

Granularity = Literal["hourly", "daily", "weekly", "monthly"]

# Order matters for drift comparison: index = relative density.
_GRANULARITIES: tuple = ("hourly", "daily", "weekly", "monthly")

# Per-granularity expected count of materializations in a 30-day window.
# Used by drift detection to flag declared-vs-observed mismatches.
_EXPECTED_PER_30_DAYS: Dict[str, int] = {
    "hourly":  24 * 30,    # 720
    "daily":   30,
    "weekly":  4,
    "monthly": 1,
}

# `partition_start` format per granularity. Validated at Asset construction.
# Forward-only patterns — anchored, not full-match, so timezone suffixes
# (`Z`, `+02:00`) on hourly are accepted by the SDK and stripped server-side.
_PARTITION_START_PATTERNS: Dict[str, str] = {
    "hourly":  r"^\d{4}-\d{2}-\d{2}T\d{2}",
    "daily":   r"^\d{4}-\d{2}-\d{2}$",
    "weekly":  r"^\d{4}-W\d{2}$",
    "monthly": r"^\d{4}-\d{2}$",
}


@dataclass
class Metadata:
    """
    Metadata to be emitted with an asset materialization event.
    
    This is a declarative pattern - metadata can be:
    1. Declared statically in task definition
    2. Returned from task output (parsed by backend)
    3. Sent via API during manual trigger
    
    Args:
        asset: The Asset this metadata is for
        data: Key-value pairs of metadata (row_count, schema_version, etc.)
    
    Example:
        # Static metadata in task definition
        @task.sfn(
            arn="${inv_arn}", 
            outlets=[inventory],
            outlet_metadata={inventory: {"type": "full_refresh"}}
        )
        def get_inventory():
            pass
        
        # Or return from task output (backend parses this)
        # Task output: {"metadata": {"row_count": 1234, "file_size": "1.2GB"}}
    """
    asset: 'Asset'
    data: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_name": self.asset.name,
            "metadata": self.data
        }


class Asset:
    """
    Represents a logical data asset.

    ⚠️  Experimental — the asset API may change in a future release, and the
    visual asset console is not yet in the open-source build (engine only).
    Constructing an Asset emits an ExperimentalWarning; silence it via
    ``warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)``.

    Assets are used to:
    1. Declare what data a task produces (outlets)
    2. Declare what data a task consumes (inlets)
    3. Trigger DAGs when assets are updated (schedule)
    
    Args:
        name: Unique identifier for the asset. If uri-like string provided as first 
              positional arg without name=, it becomes the uri and name is derived.
        uri: Optional physical location (e.g., "s3://bucket/path/", "postgres://...")
        group: Optional grouping for UI organization (e.g., "raw", "processed", "external")
        extra: Optional arbitrary metadata dict.
               Use for views, schema info, etc.
        description: Human-readable description (polyris extension)
        tags: Optional list of tags for filtering/categorization (polyris extension)
        freshness_hours: Optional freshness threshold in hours; may be fractional
                 (polyris extension)
        owner: Optional owner team or person (polyris extension)
        schema: Optional column schema. Accepted forms (mixed in one list is allowed):
                  - Column instances:   Column("order_id", types.bigint(), primary_key=True)
                  - Tuples:             ("order_id", "bigint") or ("order_id", "bigint", "Unique ID")
                  - Dicts:              {"name": "order_id", "type": "bigint", "primary_key": True}
                All forms are normalized to List[Column] internally.
                See `polyris.schema` for the full type system (types.bigint(), types.decimal(10, 2), etc.)
                and `polyris.Column` for the column class with constraints
                (nullable, primary_key, partition_key, unique).
                When glue_table is set and accessible, Glue schema takes priority.
        glue_table: Optional Glue Catalog reference as "<database>.<table>".
                    Both sides must be non-empty. Validated at construction
                    time. The "<database>" maps to AWS Glue's `DatabaseName`
                    parameter and "<table>" to `Name`. AWS Glue's three-level
                    structure is `Catalog → Database → Table`; this field
                    encodes the bottom two levels in a Hive-compatible
                    shorthand that Athena, Spark, and Presto all accept.
        glue_catalog: Optional Glue Data Catalog ID. AWS Glue Data Catalog
                      IDs are 12-digit AWS account IDs — there is one
                      catalog per AWS account per region. Empty means
                      "the Console API Lambda's own AWS account", which
                      is the common case. Set this when the Glue table
                      lives in a peer account (cross-account access).
                      Maps to the `CatalogId` parameter of `glue.get_table()`.
        glue_region: Optional AWS region (e.g. "eu-west-1"). The Glue
                     Data Catalog is per-region, so a catalog in another
                     region requires a region-pinned boto3 client. Empty
                     means "the Console API Lambda's own region". Maps
                     to `region_name` of the boto3 client.

        Note on Athena: Amazon Athena's UI shows a four-level hierarchy
        (Data source → Catalog → Database → Table), where "Data source"
        and the inner "Catalog" are Athena-side aliases that may map to
        a Glue Data Catalog (default `AwsDataCatalog`) or to a
        federated source (Lambda-backed Hive/MySQL/Snowflake). Polyris
        targets the AWS Glue Data Catalog directly via `glue.get_table()`,
        so federated sources are out of scope. For tables that live in
        an `AwsDataCatalog`-mapped Glue catalog, the SDK fields above
        cover the full addressing space.
        
    Example:
        # Simple
        orders = Asset("orders")
        
        # URI as first arg
        orders = Asset("s3://bucket/orders/")
        
        # Full declaration with typed schema (recommended)
        from polyris import Column, types as t
        orders = Asset(
            name="retail/orders",
            uri="s3://bucket/orders/",
            group="retail",
            description="Daily order aggregation",
            owner="data-team",
            tags=["daily", "critical"],
            freshness_hours=24,
            schema=[
                Column("order_id", t.bigint(), primary_key=True, nullable=False,
                       description="Unique order identifier"),
                Column("customer_id", t.bigint(), nullable=False,
                       description="FK to customers"),
                Column("event_date", t.date(), partition_key=True),
                Column("amount", t.decimal(10, 2), description="Total order amount"),
                Column("status", t.string(),
                       description="pending/confirmed/shipped/delivered"),
            ],
            glue_table="analytics.orders",
        )
        
        # Legacy tuple form is still supported (no warning, no migration required)
        orders = Asset(
            name="retail/orders",
            schema=[
                ("order_id", "bigint", "Unique order identifier"),
                ("amount", "decimal(10,2)"),
            ],
        )
    """
    
    def __init__(
        self,
        name: Optional[str] = None,
        uri: Optional[str] = None,
        *,
        group: str = "",
        extra: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,  # Alias for extra
        # polyris extensions
        description: str = "",
        tags: Optional[List[str]] = None,
        freshness_hours: Optional[float] = None,
        owner: str = "",
        schema: Optional[List] = None,
        glue_table: str = "",
        glue_catalog: str = "",
        glue_region: str = "",
        # Partition cadence (ADR #50) — declarative DSL for the Matrix view.
        # `granularity` defaults to "daily" to keep existing assets working
        # with zero boilerplate; explicit declaration is needed only for
        # weekly/monthly/hourly cadences. `partition_start` is optional —
        # when provided it must match the granularity's format (validated
        # below), and is used by drift detection to know how far back to look.
        granularity: Granularity = "daily",
        partition_start: Optional[str] = None,
    ):
        global _EXPERIMENTAL_WARNED
        if not _EXPERIMENTAL_WARNED:
            _EXPERIMENTAL_WARNED = True
            warnings.warn(
                "polyris assets are experimental — the asset API (Asset, outlets, "
                "wait_for, asset-triggered schedules) may change in a future release. "
                "The visual asset console is not yet in the open-source build; "
                "inspect lineage with `polyris-output --graph`.",
                ExperimentalWarning,
                stacklevel=2,
            )
        # Post-init invariant: resolution below always sets a non-None name
        self.name: str
        # Support metadata as alias for extra
        if metadata is not None and extra is None:
            extra = metadata
        elif metadata is not None and extra is not None:
            # Merge: extra takes precedence
            extra = {**metadata, **extra}
        
        # First positional arg can be uri or name
        # If it looks like a URI (contains ://), treat as uri
        if name is not None and uri is None and "://" in name:
            uri = name
            name = None
        
        # At least one of name/uri must be provided
        if name is None and uri is None:
            raise ValueError("At least one of 'name' or 'uri' must be provided")
        
        # Derive name from uri if not provided
        if name is None and uri is not None:
            # Extract meaningful name from uri
            # s3://bucket/path/to/data/ -> path/to/data
            name = uri.rstrip('/').split('://')[-1]
            if '/' in name:
                # Remove bucket name for s3/gs urls
                parts = name.split('/', 1)
                if len(parts) > 1:
                    name = parts[1]
        
        # Derive uri from name if not provided (just use name as identifier)
        if uri is None:
            uri = ""
        
        assert name is not None  # guaranteed: raised above when both name/uri missing; else derived from uri
        self.name = name
        self.uri = uri
        self.group = group
        self.extra = extra or {}
        self.description = description
        self.tags = tags or []
        self.freshness_hours = freshness_hours
        self.owner = owner
        self.schema: Schema = normalize_schema(schema)
        self.glue_table = glue_table
        self.glue_catalog = glue_catalog
        self.glue_region = glue_region

        # Partition cadence validation (ADR #50).
        # `granularity` is enforced by Literal at type-check time, but Python
        # doesn't validate Literal at runtime — so we also check at construction.
        # `partition_start` (if provided) must match the granularity's format.
        if granularity not in _GRANULARITIES:
            raise ValueError(
                f"granularity must be one of {_GRANULARITIES}, got {granularity!r}"
            )
        self.granularity: str = granularity

        if partition_start is not None:
            import re as _re
            pattern = _PARTITION_START_PATTERNS[granularity]
            if not _re.match(pattern, partition_start):
                raise ValueError(
                    f"partition_start {partition_start!r} does not match the "
                    f"format expected for granularity={granularity!r}. "
                    f"Expected format: "
                    f"daily=YYYY-MM-DD, weekly=YYYY-Www, "
                    f"monthly=YYYY-MM, hourly=YYYY-MM-DDTHH"
                )
        self.partition_start: Optional[str] = partition_start

        # Validate glue_table format up front: backend parses it as
        # `database.table` via `split('.', 1)`. Catching a malformed value at
        # construction time means the failure surfaces in the developer's
        # editor, not in a 422 response from the Console API after deploy.
        # Glue technically permits dots in table names (rare in practice), so
        # the check is structural: presence of exactly one separator, both
        # sides non-empty. Users with literal dots in identifiers can open
        # an issue and we'll add an escape mechanism then (CLAUDE.md #5).
        if self.glue_table:
            if '.' not in self.glue_table:
                raise ValueError(
                    f"glue_table must be 'database.table', got {self.glue_table!r}. "
                    f"The format references a Glue database (e.g. 'default') and a "
                    f"table within it (e.g. 'example'). The Glue Data Catalog itself "
                    f"is implicit — by default the AWS account of the Console API "
                    f"Lambda. Set `glue_catalog=<account-id>` to target another "
                    f"account's catalog, and `glue_region=<region>` for cross-region."
                )
            db, _, tbl = self.glue_table.partition('.')
            if not db or not tbl:
                raise ValueError(
                    f"glue_table must be 'database.table' with both sides non-empty, "
                    f"got {self.glue_table!r}"
                )
        
        # Internal - will be populated during DAG parsing
        self._producers: List['Task'] = []
        self._consumers: List['Task'] = []
        self._dag: Optional['DAG'] = None
        
        # Auto-derive group from name if not provided
        if not self.group and '/' in self.name:
            self.group = self.name.split('/')[0]
    
    # =========================================================================
    # Classmethod constructors — populate `schema` from external sources.
    #
    # All three are thin wrappers around the corresponding adapter in
    # polyris.adapters; they exist so users get the natural call site
    # `Asset.from_pyarrow(...)` instead of having to import the adapter
    # module separately. See ADR #44 for design.
    # =========================================================================

    @classmethod
    def from_pyarrow(
        cls,
        pa_schema: Any,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> 'Asset':
        """Create an Asset whose `schema` is derived from a pyarrow.Schema.

        Useful when the source-of-truth schema lives in Parquet, Iceberg,
        BigQuery, Polars, Pandas, or DuckDB — all of which can produce a
        pyarrow.Schema natively or via a one-line converter.

        Example:
            import pyarrow.parquet as pq
            sample = pq.read_metadata("s3://bucket/orders/sample.parquet")
            orders = Asset.from_pyarrow(
                sample.schema.to_arrow_schema(),
                name="retail/orders",
                glue_table="analytics.orders",
            )

        All standard `Asset(...)` keyword arguments are accepted alongside
        `pa_schema` — the schema is injected as `schema=`.
        """
        from .adapters.pyarrow_ import pyarrow_to_columns
        if 'schema' in kwargs:
            raise TypeError(
                "from_pyarrow() derives `schema` from pa_schema; do not also pass `schema=`"
            )
        return cls(name=name, schema=pyarrow_to_columns(pa_schema), **kwargs)

    @classmethod
    def from_parquet(
        cls,
        path: str,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> 'Asset':
        """Create an Asset whose `schema` is read from a Parquet file's metadata.

        Convenience wrapper over `from_pyarrow` for the most common entry
        point — a Parquet sample on local disk or in S3. Reads only the
        file footer (no row data is fetched), so it is cheap even for
        large files.

        Example:
            # Local path
            orders = Asset.from_parquet(
                "samples/orders.parquet",
                name="retail/orders",
            )

            # S3 path — uses pyarrow's built-in S3 filesystem; requires
            # AWS credentials in the standard chain (env vars, ~/.aws, IAM role).
            orders = Asset.from_parquet(
                "s3://bucket/orders/sample.parquet",
                name="retail/orders",
                glue_table="analytics.orders",
            )

            # No explicit name — derived from the file basename.
            # Reads "orders.parquet" → asset name "orders". Convenient for
            # quick prototypes; production code should pass an explicit
            # `name="domain/asset"` so the asset shows up grouped on the
            # Assets page (matches the `from_pydantic` convention).
            orders = Asset.from_parquet("samples/orders.parquet")
            # orders.name == 'orders'

        Requires the `pyarrow` extra (`pip install 'polyris[pyarrow]'`);
        the lazy import surfaces a clear ImportError otherwise. All
        standard `Asset(...)` keyword arguments are accepted alongside
        `path` — the schema is injected as `schema=`.
        """
        from .adapters.pyarrow_ import _require_pyarrow
        if 'schema' in kwargs:
            raise TypeError(
                "from_parquet() derives `schema` from the file; do not also pass `schema=`"
            )
        _require_pyarrow()
        # `pq.read_schema` reads only the Parquet footer and supports
        # local paths and pyarrow.fs URIs (s3://, gs://, hdfs://, ...)
        # with no extra wiring on our side.
        import pyarrow.parquet as pq
        pa_schema = pq.read_schema(path)

        # Default name to the file basename (without extension) so a
        # bare `Asset.from_parquet("orders.parquet")` works without the
        # cryptic "name or uri must be provided" error from Asset.__init__.
        # Mirrors the fallback ergonomics of `from_pydantic` (model class
        # name) and `from_glue_table` (db.table). If the path does not yield
        # a sensible stem (rare — empty string, root, etc.), fall through
        # without a default and let Asset.__init__ surface the issue.
        if name is None:
            from os.path import basename, splitext
            # basename handles both local paths and s3:// (uses last segment).
            stem = splitext(basename(path.rstrip('/')))[0] or None
            name = stem
        return cls.from_pyarrow(pa_schema, name=name, **kwargs)

    @classmethod
    def from_pydantic(
        cls,
        model_cls: type,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> 'Asset':
        """Create an Asset whose `schema` is derived from a pydantic model.

        Pydantic model fields, types, descriptions, defaults, and optionality
        are all surfaced. See `polyris.adapters.pydantic_` for the full
        type-mapping table.

        Example:
            class Order(BaseModel):
                order_id: int = Field(description="Primary key")
                amount: Decimal
                created_at: datetime
                tags: list[str] = []

            orders = Asset.from_pydantic(Order, name="retail/orders")
        """
        from .adapters.pydantic_ import pydantic_to_columns
        if 'schema' in kwargs:
            raise TypeError(
                "from_pydantic() derives `schema` from the model; do not also pass `schema=`"
            )
        # If no explicit name, fall back to the model's class name
        # so the asset has a sensible default identity.
        derived_name = name or getattr(model_cls, '__name__', None)
        return cls(name=derived_name, schema=pydantic_to_columns(model_cls), **kwargs)

    @classmethod
    def from_glue_table(
        cls,
        glue_table: str,
        name: Optional[str] = None,
        *,
        catalog_id: str = '',
        region: Optional[str] = None,
        **kwargs: Any,
    ) -> 'Asset':
        """Create an Asset whose `schema` is fetched from AWS Glue Catalog.

        The `glue_table`, `catalog_id`, and `region` arguments are also stored
        on the resulting Asset, so the runtime drift-detection path (ADR #43)
        works on this asset out of the box without re-typing them.

        Example:
            orders = Asset.from_glue_table(
                "analytics.orders",
                name="retail/orders",
                owner="data-team",
            )

        Cross-account: pass `catalog_id="<account-id>"`. The provided account
        must grant `glue:GetTable` to the caller; the deploy-time call uses
        the developer's local credentials, the runtime drift-detection call
        uses the Console API Lambda's role.

        Cross-region: pass `region="eu-west-1"`. The value is stored in
        `glue_region` on the resulting Asset so backend drift detection
        queries the correct region.

        Default name behaviour:
            - Local catalog (no `catalog_id`):         "{database}.{table}"
            - Cross-account (`catalog_id` provided):   "{catalog_id}.{database}.{table}"

            The cross-account form prevents collisions between same-named
            tables in different AWS accounts. Pass `name=` explicitly to
            override.
        """
        from .adapters.glue import glue_table_to_columns
        if 'schema' in kwargs:
            raise TypeError(
                "from_glue_table() derives `schema` from Glue; do not also pass `schema=`"
            )
        # Note: a duplicate `glue_table=` kwarg is caught by Python itself
        # ("multiple values for argument 'glue_table'") because glue_table is
        # the first positional parameter — no explicit guard needed here.
        if '.' not in glue_table:
            raise ValueError(
                f"glue_table must be 'database.table', got {glue_table!r}"
            )
        database, table = glue_table.split('.', 1)
        cols = glue_table_to_columns(
            database, table,
            catalog_id=catalog_id or None,
            region=region,
        )
        # Default name strategy disambiguates cross-account references.
        # When `catalog_id` is non-empty we prepend it to the default name
        # so two pipelines pulling `default.example` from accounts 111 and 222
        # don't collide on a single asset entry.
        default_name = (
            f"{catalog_id}.{glue_table}" if catalog_id else glue_table
        )
        return cls(
            name=name or default_name,
            schema=cols,
            glue_table=glue_table,
            glue_catalog=catalog_id,
            glue_region=region or '',
            **kwargs,
        )

    @classmethod
    def from_iceberg(
        cls,
        iceberg_table: Any,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> 'Asset':
        """Create an Asset whose `schema` is taken from a pyiceberg Table.

        Convenience wrapper over `from_pyarrow` for users whose source-of-truth
        catalog is Apache Iceberg. Reads the table's current schema in-memory
        (via `iceberg_table.schema().as_arrow()`); does not fetch row data.

        Example:
            from pyiceberg.catalog import load_catalog

            catalog = load_catalog('default')
            iceberg_table = catalog.load_table('analytics.orders')
            orders = Asset.from_iceberg(iceberg_table, name="retail/orders")

        Args:
            iceberg_table: a `pyiceberg.table.Table` instance. The
                pyiceberg package is NOT a polyris dependency — the
                caller already has it (otherwise they have no Iceberg
                table to pass). We just call methods on the object.
            name: optional asset name. If omitted, defaults to the
                Iceberg table's identifier ("namespace.tablename"),
                consistent with the `from_glue_table` convention.

        Pyarrow IS required (it is the underlying schema bridge — see
        `from_pyarrow`). Install with `pip install 'polyris[pyarrow]'`.

        Raises:
            TypeError if `schema=` is also passed.
            AttributeError if the object does not look like a
                pyiceberg Table (no `.schema()` method).
        """
        if 'schema' in kwargs:
            raise TypeError(
                "from_iceberg() derives `schema` from the Iceberg table; "
                "do not also pass `schema=`"
            )
        # Duck-typed access: Iceberg's Table API exposes .schema() (returns
        # pyiceberg.schema.Schema, which has .as_arrow()) and .name()
        # (returns Identifier, a tuple of strings). We do not import
        # pyiceberg ourselves to keep the dependency surface tiny.
        try:
            iceberg_schema = iceberg_table.schema()
        except AttributeError as e:
            raise AttributeError(
                f"from_iceberg() expects a pyiceberg.table.Table instance, "
                f"got {type(iceberg_table).__name__}. Pass the result of "
                f"`catalog.load_table('db.t')`."
            ) from e

        # `as_arrow()` is the pyiceberg-native bridge to pyarrow.Schema.
        # It exists on pyiceberg.schema.Schema in 0.4+.
        if not hasattr(iceberg_schema, 'as_arrow'):
            raise AttributeError(
                "Iceberg schema object has no `as_arrow()` method. "
                "Upgrade pyiceberg to 0.4 or later."
            )
        pa_schema = iceberg_schema.as_arrow()

        # Default name: Iceberg's identifier joined as "ns.table". Mirrors
        # from_glue_table convention so the same asset declared via either
        # path lands at the same default name.
        if name is None:
            ident = getattr(iceberg_table, 'name', lambda: None)()
            if ident:
                # Identifier may be a tuple (Iceberg) or a string.
                name = '.'.join(ident) if isinstance(ident, tuple) else str(ident)

        return cls.from_pyarrow(pa_schema, name=name, **kwargs)

    def __hash__(self):
        return hash(self.name)
    
    def __eq__(self, other):
        if isinstance(other, Asset):
            return self.name == other.name
        return False
    
    def __and__(self, other: 'Asset') -> 'AssetAll':
        """
        asset_a & asset_b creates AND condition.
        DAG will trigger only when ALL assets are updated.
        """
        if isinstance(other, AssetAll):
            return AssetAll(assets=[self] + other.assets)
        elif isinstance(other, (Asset, AssetRef, AssetConsecutiveRef)):
            return AssetAll(assets=[self, other])
        raise TypeError(f"Cannot combine Asset with {type(other)}")
    
    def __or__(self, other: 'Asset') -> 'AssetAny':
        """
        asset_a | asset_b creates OR condition.
        DAG will trigger when ANY asset is updated.
        """
        if isinstance(other, AssetAny):
            return AssetAny(assets=[self] + other.assets)
        elif isinstance(other, (Asset, AssetRef, AssetConsecutiveRef)):
            return AssetAny(assets=[self, other])
        raise TypeError(f"Cannot combine Asset with {type(other)}")
    
    def add_producer(self, task: 'Task'):
        """Register a task as producer of this asset."""
        if task not in self._producers:
            self._producers.append(task)
    
    def add_consumer(self, task: 'Task'):
        """Register a task as consumer of this asset."""
        if task not in self._consumers:
            self._consumers.append(task)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize asset for JSON output."""
        result: Dict[str, Any] = {
            "name": self.name,
            "uri": self.uri,
            "group": self.group,
            "description": self.description,
            "tags": self.tags,
            "freshness_hours": self.freshness_hours,
            "owner": self.owner,
            "schema": [column_to_dict(c) for c in self.schema],
            "glue_table": self.glue_table,
            "glue_catalog": self.glue_catalog,
            "glue_region": self.glue_region,
            "producers": [f"{t._dag.dag_id}.{t.task_id}" if t._dag else t.task_id for t in self._producers],
            "consumers": [f"{t._dag.dag_id}.{t.task_id}" if t._dag else t.task_id for t in self._consumers],
        }
        # Include extra metadata if present
        if self.extra:
            result["extra"] = self.extra
        return result

    # =========================================================================
    # Inspection / export helpers — make the typed schema useful at the REPL,
    # in CLI tooling, and for sharing with downstream systems.
    # =========================================================================

    def print_schema(self) -> None:
        """Print the schema as a human-readable table (mirrors `pandas.DataFrame.info()`).

        Prints column index, name, type (Glue-format string), constraint
        flags, and description if any. No-op for empty schemas (prints
        a single line). Safe to call interactively without imports.

        Output format:
            #  name        type           constraints       description
            0  order_id    bigint         PK, NOT NULL      Primary key
            1  amount      decimal(10,2)
            2  event_date  date           Partition
            3  status      string                           pending | shipped | …
        """
        from .schema import to_glue_string  # local import: avoid cycle on package init
        if not self.schema:
            print(f"Asset {self.name!r} — no schema declared.")
            return

        # Compute column widths so the table aligns.
        rows = []
        for col in self.schema:
            constraints = []
            if col.primary_key:
                constraints.append("PK")
            if col.partition_key:
                constraints.append("Partition")
            if not col.nullable:
                constraints.append("NOT NULL")
            if col.unique:
                constraints.append("UNIQUE")
            rows.append((col.name, to_glue_string(col.type),
                         ", ".join(constraints), col.description))

        idx_w  = max(len(str(len(rows) - 1)), 1)
        name_w = max(len("name"),        max(len(r[0]) for r in rows))
        type_w = max(len("type"),        max(len(r[1]) for r in rows))
        cons_w = max(len("constraints"), max(len(r[2]) for r in rows))

        print(f"Asset {self.name!r} — {len(rows)} column{'s' if len(rows) != 1 else ''}:")
        print(f"  {'#':>{idx_w}}  {'name':<{name_w}}  {'type':<{type_w}}  "
              f"{'constraints':<{cons_w}}  description")
        print(f"  {'-' * idx_w}  {'-' * name_w}  {'-' * type_w}  "
              f"{'-' * cons_w}  -----------")
        for i, (name, typ, cons, desc) in enumerate(rows):
            print(f"  {i:>{idx_w}}  {name:<{name_w}}  {typ:<{type_w}}  "
                  f"{cons:<{cons_w}}  {desc}")

    def to_ddl(self, dialect: str = 'glue') -> str:
        """Render the asset as a `CREATE TABLE` statement.

        Args:
            dialect: SQL dialect to target. Currently only `'glue'` is
                supported (Glue/Hive DDL, also accepted by Athena, Spark,
                and Trino). Other dialects (`bigquery`, `postgres`,
                `iceberg`) are deferred until a real user request — the
                Glue/Hive output covers the project's primary use case.

        Returns:
            DDL string. The table name is taken from `glue_table` if set,
            otherwise the asset's `name`. Partition columns use
            `PARTITIONED BY (...)` and are excluded from the regular column
            list, matching Glue/Hive convention.

        Raises:
            ValueError if the schema is empty (no columns to declare).
            ValueError if `dialect` is unsupported.

        Architecture note (ADR #46): this method is a thin dispatcher
        over per-dialect renderer helpers (currently only `_render_glue_ddl`).
        When the second dialect lands (BigQuery, Postgres, Iceberg, ...),
        the renderers move to `polyris/renderers/` as a plugin pattern;
        this public API stays unchanged.
        """
        if not self.schema:
            raise ValueError(
                f"to_ddl(): asset {self.name!r} has no schema declared — "
                f"nothing to render. Add columns via `Asset(..., schema=[...])` "
                f"or one of the `from_*` constructors."
            )
        if dialect == 'glue':
            return _render_glue_ddl(self)
        raise ValueError(
            f"to_ddl(): only 'glue' dialect is currently supported, got {dialect!r}. "
            f"Open an issue if you need bigquery/postgres/iceberg output."
        )

    def to_jsonschema(self) -> Dict[str, Any]:
        """Render the asset as a JSON Schema (Draft 2020-12) object.

        Each row of the asset is treated as an object whose properties
        are the columns. Nullable columns become `{"type": [<t>, "null"]}`;
        non-nullable columns appear in `required`. Useful for generating
        API contracts, validation snippets, or pydantic-via-codegen.

        Type mapping:
            bigint/int/smallint/tinyint  → "integer"
            float/double/decimal          → "number"  (decimal carries
                                            "format": "decimal(p,s)")
            boolean                       → "boolean"
            string/varchar/char/uuid/json → "string"  (varchar/char carry
                                            "maxLength")
            date                          → "string", "format": "date"
            time                          → "string", "format": "time"
            timestamp(tz_aware)           → "string", "format": "date-time"
            timestamp_ntz                 → "string", "format": "date-time"
            binary/fixed_binary           → "string", "contentEncoding": "base64"
            array<inner>                  → {"type":"array","items": <inner>}
            map<k,v>                      → {"type":"object",
                                            "additionalProperties": <v>}
            struct<...>                   → {"type":"object","properties":...}
        """
        if not self.schema:
            return {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "title": self.name,
                "type": "object",
                "properties": {},
            }
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for col in self.schema:
            properties[col.name] = _polyris_type_to_jsonschema(col.type, col.nullable)
            if col.description:
                properties[col.name]["description"] = col.description
            if not col.nullable:
                required.append(col.name)
        out: Dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.name,
            "type": "object",
            "properties": properties,
        }
        if self.description:
            out["description"] = self.description
        if required:
            out["required"] = required
        return out

    def __repr__(self):
        return f"Asset('{self.name}')"
    
    def within(self, hours: int = 0, days: int = 0, weeks: int = 0, minutes: int = 0) -> 'AssetRef':
        """
        Create a reference to this asset with freshness constraint.
        Used with wait_for for pull-based cross-pipeline dependencies.
        
        Args:
            hours: Maximum age in hours
            days: Maximum age in days  
            weeks: Maximum age in weeks
            minutes: Maximum age in minutes (converted to fractional hours —
                mainly useful for quick manual testing; hours/days/weeks cover
                real production freshness windows)
            
        Returns:
            AssetRef with freshness constraint
            
        Example:
            @task.sfn(wait_for=[asset_x.within(hours=24)])
            def process(): ...
            
            @task.sfn(wait_for=[asset_x.within(days=1, hours=12)])
            def process(): ...

            @task.sfn(wait_for=[asset_x.within(minutes=2)])  # quick manual testing
            def process(): ...
            
        Raises:
            ValueError: If no time parameters provided
        """
        if hours == 0 and days == 0 and weeks == 0 and minutes == 0:
            raise ValueError("within() requires at least one of: hours, days, weeks, minutes")
        
        total_hours = hours + (days * 24) + (weeks * 24 * 7) + (minutes / 60 if minutes else 0)
        return AssetRef(asset=self, freshness_hours=total_hours)

    def consecutive(self, days: int) -> 'AssetConsecutiveRef':
        """
        Check that asset has events for N consecutive dates.
        
        Used with wait_for to ensure multiple dates of data are ready.
        Checks for events with execution_date in the range
        [current_date - (days-1), ..., current_date].
        
        Args:
            days: Number of consecutive dates required
            
        Returns:
            AssetConsecutiveRef with consecutive constraint
            
        Examples:
            # Weekly waits for 7 daily completions
            @task.sfn(wait_for=[daily.consecutive(days=7)])
            
            # Combined with other assets (AND - list)
            @task.sfn(wait_for=[daily.consecutive(days=7), prices.within(hours=24)])
            
            # OR - either consecutive complete or manual override
            @task.sfn(wait_for=[daily.consecutive(days=7) | manual_override])
            
            # Multiple consecutives
            @task.sfn(wait_for=[sales.consecutive(days=7) & inventory.consecutive(days=7)])
        
        Raises:
            ValueError: If days < 1
        """
        if days < 1:
            raise ValueError(f"consecutive() requires days >= 1, got {days}")
        return AssetConsecutiveRef(asset=self, consecutive_days=days)


@dataclass
class AssetRef:
    """
    Reference to an Asset with optional freshness constraint.
    Used in wait_for for pull-based cross-pipeline dependencies.
    
    Created via Asset.within() method, not directly.
    
    Example:
        # These are equivalent:
        asset_x.within(hours=24)
        AssetRef(asset=asset_x, freshness_hours=24)
    """
    asset: Asset
    # float, not int: within(minutes=...) is a documented public entry point and
    # legitimately yields fractional hours (minutes=30 -> 0.5). The consumer
    # compares `age_hours <= freshness_hours`, so fractions are meaningful.
    freshness_hours: Optional[float] = None
    
    def __hash__(self):
        return hash((self.asset.name, self.freshness_hours))
    
    def __eq__(self, other):
        if isinstance(other, AssetRef):
            return self.asset == other.asset and self.freshness_hours == other.freshness_hours
        return False
    
    def __and__(self, other: Union[Asset, 'AssetRef', 'AssetConsecutiveRef', 'AssetAll']) -> 'AssetAll':
        """asset_x.within(24) & asset_y creates AND condition."""
        if isinstance(other, AssetAll):
            return AssetAll(assets=[self] + other.assets)
        elif isinstance(other, (Asset, AssetRef, AssetConsecutiveRef)):
            return AssetAll(assets=[self, other])
        raise TypeError(f"Cannot combine AssetRef with {type(other)}")
    
    def __or__(self, other: Union[Asset, 'AssetRef', 'AssetConsecutiveRef', 'AssetAny']) -> 'AssetAny':
        """asset_x.within(24) | asset_y creates OR condition."""
        if isinstance(other, AssetAny):
            return AssetAny(assets=[self] + other.assets)
        elif isinstance(other, (Asset, AssetRef, AssetConsecutiveRef)):
            return AssetAny(assets=[self, other])
        raise TypeError(f"Cannot combine AssetRef with {type(other)}")
    
    @property
    def name(self) -> str:
        """Delegate to underlying asset."""
        return self.asset.name
    
    @property
    def uri(self) -> str:
        """Delegate to underlying asset."""
        return self.asset.uri
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON output."""
        return {
            "asset_name": self.asset.name,
            "freshness_hours": self.freshness_hours,
        }
    
    def __repr__(self):
        if self.freshness_hours:
            return f"AssetRef('{self.asset.name}', within={self.freshness_hours}h)"
        return f"AssetRef('{self.asset.name}')"


@dataclass
class AssetConsecutiveRef:
    """
    Reference to an Asset with consecutive-days constraint.
    Used in wait_for for pull-based cross-pipeline dependencies.
    
    Checks that asset has events for N consecutive dates ending at
    the pipeline's current_date.
    
    Created via Asset.consecutive(days=N), not directly.
    
    Example:
        # Weekly waits for 7 daily completions
        wait_for=[daily_complete.consecutive(days=7)]
        
        # This checks for events with execution_date in:
        # [current_date - 6, current_date - 5, ..., current_date]
    """
    asset: Asset
    consecutive_days: int
    
    def __hash__(self):
        return hash((self.asset.name, self.consecutive_days))
    
    def __eq__(self, other):
        if isinstance(other, AssetConsecutiveRef):
            return self.asset == other.asset and self.consecutive_days == other.consecutive_days
        return False
    
    def __and__(self, other: Union[Asset, AssetRef, 'AssetConsecutiveRef', 'AssetAll']) -> 'AssetAll':
        """asset.consecutive(7) & other creates AND condition."""
        if isinstance(other, AssetAll):
            return AssetAll(assets=[self] + other.assets)
        elif isinstance(other, (Asset, AssetRef, AssetConsecutiveRef)):
            return AssetAll(assets=[self, other])
        raise TypeError(f"Cannot combine AssetConsecutiveRef with {type(other)}")
    
    def __or__(self, other: Union[Asset, AssetRef, 'AssetConsecutiveRef', 'AssetAny']) -> 'AssetAny':
        """asset.consecutive(7) | other creates OR condition."""
        if isinstance(other, AssetAny):
            return AssetAny(assets=[self] + other.assets)
        elif isinstance(other, (Asset, AssetRef, AssetConsecutiveRef)):
            return AssetAny(assets=[self, other])
        raise TypeError(f"Cannot combine AssetConsecutiveRef with {type(other)}")
    
    @property
    def name(self) -> str:
        """Delegate to underlying asset."""
        return self.asset.name
    
    @property
    def uri(self) -> str:
        """Delegate to underlying asset."""
        return self.asset.uri
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON output."""
        return {
            "asset_name": self.asset.name,
            "consecutive_days": self.consecutive_days,
        }
    
    def __repr__(self):
        return f"AssetConsecutiveRef('{self.asset.name}', consecutive={self.consecutive_days}d)"


# Operand type the combinator algebra actually accepts at runtime
# (Asset plus its freshness/consecutive refs) — annotations match behavior.
AssetOperand = Union[Asset, "AssetRef", "AssetConsecutiveRef"]
# What an AssetAll may hold. A nested AssetAny is legal — `schedule=[a, b | c]`
# is a AND (b OR c) — so the AND-group's operand type is wider than a leaf's.
AssetAllOperand = Union[AssetOperand, "AssetAny"]


@dataclass
class AssetAll:
    """
    AND condition for multiple assets.
    DAG triggers only when ALL assets have been updated since last run.
    
    Example:
        schedule=[inventory & catalog]  # Wait for both
        # or equivalently:
        schedule=[AssetAll([inventory, catalog])]
    """
    # AssetAny is allowed as an operand: `schedule=[a, b | c]` means
    # a AND (b OR c), which is stored as an AssetAll holding a nested AssetAny.
    # Symmetric with AssetAny.assets, which already permits nesting.
    assets: List[AssetAllOperand] = field(default_factory=list)
    
    def __and__(self, other: Union[Asset, 'AssetAll']) -> 'AssetAll':
        """Chain AND: (a & b) & c"""
        if isinstance(other, AssetAll):
            return AssetAll(assets=cast("List[AssetAllOperand]", self.assets + other.assets))
        elif isinstance(other, Asset):
            return AssetAll(assets=cast("List[AssetAllOperand]", self.assets + [other]))
        raise TypeError(f"Cannot combine AssetAll with {type(other)}")
    
    def __or__(self, other) -> 'AssetAny':
        """Mixed: (a & b) | c — creates AssetAny containing AssetAll.

        OR is associative, so an AssetAny operand is flattened into the
        result rather than nested: nested AssetAny is invisible to
        asset_names/to_dict and would silently drop trigger operands.
        """
        if isinstance(other, AssetAny):
            ops: List[Union[AssetOperand, AssetAll]] = [self]
            ops.extend(other.assets)
            return AssetAny(assets=ops)
        if isinstance(other, Asset):
            return AssetAny(assets=[self, other])
        raise TypeError(f"Cannot combine AssetAll with {type(other)}")
    
    @property
    def operator(self) -> str:
        return "AND"
    
    @property
    def asset_names(self) -> List[str]:
        names = []
        for a in self.assets:
            if isinstance(a, AssetAny):
                names.append(f"({' | '.join(a.asset_names)})")
            else:
                names.append(a.name)
        return names
    
    def to_dict(self) -> Dict[str, Any]:
        items: List[Any] = [
            a.name if isinstance(a, Asset) else a.to_dict() for a in self.assets
        ]
        return {
            "operator": "AND",
            "assets": items
        }
    
    def __repr__(self):
        return f"AssetAll({self.asset_names})"


@dataclass
class AssetAny:
    """
    OR condition for multiple assets.
    DAG triggers when ANY asset is updated.

    Example:
        schedule=[inventory | catalog]  # Trigger on either
        # or equivalently:
        schedule=[AssetAny([inventory, catalog])]

    Single-item AssetAny (deliberate pattern, not a typo): `schedule=AssetAny([x])`
    triggers on EVERY materialization of `x`, with no day-scoped deduplication —
    unlike `schedule=x` or `schedule=[x]`, which are AND-of-one (same net effect
    of "one required asset", but the notify_asset_consumers path dedupes by
    calendar day, so a producer that materializes `x` more than once on the same
    day only triggers this consumer once). Use `AssetAny([x])` when the consumer
    should react to every update, e.g. an hourly producer, or a deliberate
    re-run whose downstream should recompute too. See
    docs/reference/SPIKE_ASSET_TRIGGER_GRANULARITY.md for the full analysis.
    """
    assets: List[Union[AssetOperand, AssetAll]] = field(default_factory=list)
    
    def __or__(self, other: Union[Asset, 'AssetAny', AssetAll]) -> 'AssetAny':
        """Chain OR: (a | b) | c"""
        if isinstance(other, AssetAny):
            return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", self.assets + other.assets))
        elif isinstance(other, (Asset, AssetAll)):
            return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", self.assets + [other]))
        raise TypeError(f"Cannot combine AssetAny with {type(other)}")
    
    @property
    def operator(self) -> str:
        return "OR"
    
    @property
    def asset_names(self) -> List[str]:
        names = []
        for a in self.assets:
            if isinstance(a, AssetAll):
                names.append(f"({' & '.join(a.asset_names)})")
            else:
                names.append(a.name)
        return names
    
    def to_dict(self) -> Dict[str, Any]:
        items: List[Any] = [
            a.name if isinstance(a, Asset) else a.to_dict() for a in self.assets
        ]
        return {
            "operator": "OR",
            "assets": items
        }
    
    def __repr__(self):
        return f"AssetAny({self.asset_names})"


@dataclass
class AssetAlias:
    """
    Alias for a group of related assets.
    
    When used in a schedule, the DAG triggers when ANY asset in the alias is updated.
    Useful for grouping assets from different sources that represent the same logical data.
    
    Args:
        name: Alias name (e.g., "all_sales", "regional_data")
        assets: List of Assets that belong to this alias
        description: Human-readable description
    
    Example:
        # Group regional sales assets
        us_sales = Asset("sales/us")
        eu_sales = Asset("sales/eu")
        apac_sales = Asset("sales/apac")
        
        all_sales = AssetAlias(
            name="all_sales",
            assets=[us_sales, eu_sales, apac_sales],
            description="Sales data from all regions"
        )
        
        # DAG triggers when ANY regional sales is updated
        with DAG(dag_id="global-report", schedule=[all_sales]) as dag:
            ...
    
    Backend Implementation:
        - Resolved from pipeline_registry task outlets at query time
        - EventBridge rule matches any asset in the alias
        - UI shows alias with member count and expansion
    """
    name: str
    assets: List[Asset] = field(default_factory=list)
    description: str = ""
    
    def __hash__(self):
        return hash(self.name)
    
    def __eq__(self, other):
        if isinstance(other, AssetAlias):
            return self.name == other.name
        return False
    
    def __and__(self, other: Union[Asset, 'AssetAlias', AssetAll]) -> AssetAll:
        """Alias & something → AND condition with all assets in alias"""
        if isinstance(other, AssetAlias):
            return AssetAll(assets=cast("List[AssetAllOperand]", self.assets + other.assets))
        elif isinstance(other, AssetAll):
            return AssetAll(assets=cast("List[AssetAllOperand]", self.assets + other.assets))
        elif isinstance(other, Asset):
            return AssetAll(assets=cast("List[AssetAllOperand]", self.assets + [other]))
        raise TypeError(f"Cannot combine AssetAlias with {type(other)}")
    
    def __or__(self, other: Union[Asset, 'AssetAlias', AssetAny]) -> AssetAny:
        """Alias | something → OR condition"""
        if isinstance(other, AssetAlias):
            return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", self.assets + other.assets))
        elif isinstance(other, AssetAny):
            return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", self.assets + other.assets))
        elif isinstance(other, Asset):
            return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", self.assets + [other]))
        raise TypeError(f"Cannot combine AssetAlias with {type(other)}")
    
    @property
    def asset_names(self) -> List[str]:
        return [a.name for a in self.assets]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "is_alias": True,
            "description": self.description,
            "assets": [a.name for a in self.assets],
            "operator": "OR"  # Alias = OR by default
        }
    
    def __repr__(self):
        return f"AssetAlias('{self.name}', {len(self.assets)} assets)"


# Type alias for schedule parameter
AssetSchedule = Union[Asset, AssetAll, AssetAny, AssetAlias, List[Union[Asset, AssetAll, AssetAny, AssetAlias]]]


def normalize_asset_schedule(schedule: Optional[AssetSchedule]) -> Union[AssetAll, AssetAny, None]:
    """
    Normalize various schedule formats to AssetAll or AssetAny.
    
    Supported formats:
        schedule=asset                    → AssetAll([asset])
        schedule=[asset]                  → AssetAll([asset])
        schedule=[asset_a, asset_b]       → AssetAll([asset_a, asset_b])  # AND
        schedule=[asset_a & asset_b]      → AssetAll([asset_a, asset_b])  # explicit AND
        schedule=[asset_a | asset_b]      → AssetAny([asset_a, asset_b])  # OR
        schedule=AssetAll([...])          → AssetAll([...])
        schedule=AssetAny([...])          → AssetAny([...])
        schedule=AssetAlias([...])        → AssetAny([...])  # Alias = OR
        schedule=[AssetAlias]             → AssetAny(alias.assets)
    """
    if schedule is None:
        return None
    
    # Single Asset → AND with one item
    if isinstance(schedule, Asset):
        return AssetAll(assets=[schedule])

    # Bare AssetRef/AssetConsecutiveRef (e.g. schedule=asset.within(hours=1)
    # or schedule=asset.consecutive(days=3)) — same treatment as a single
    # Asset: AND with one item. Previously unhandled, so it fell through to
    # `return None` below, and the DAG silently got no trigger at all: not
    # asset-triggered (this function returning None), and not time-based
    # either (dag.py's __post_init__ only takes the time-based branch for a
    # str schedule) — the pipeline just never fired, with no error anywhere.
    if isinstance(schedule, (AssetRef, AssetConsecutiveRef)):
        return AssetAll(assets=[schedule])

    # AssetAlias → OR of all member assets
    if isinstance(schedule, AssetAlias):
        return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", schedule.assets))
    
    # Already normalized
    if isinstance(schedule, (AssetAll, AssetAny)):
        return schedule
    
    # List of assets
    if isinstance(schedule, list):
        if len(schedule) == 0:
            return None
        
        # If list contains a single item
        if len(schedule) == 1:
            item = schedule[0]
            if isinstance(item, (AssetAll, AssetAny)):
                return item
            elif isinstance(item, AssetAlias):
                return AssetAny(assets=cast("List[Union[AssetOperand, AssetAll]]", item.assets))
            elif isinstance(item, Asset):
                return AssetAll(assets=cast("List[AssetAllOperand]", [item]))
        
        # Multiple items in list → AND by default. AND is associative, so a
        # nested AssetAll item is flattened (schedule=[a, b & c] means
        # a AND b AND c, not a AND (AND b c) — leaving it nested crashed
        # asset_names/to_dict downstream, since AssetAll has no .name).
        # An AssetAny item is kept as a single nested OR-group operand
        # (schedule=[a, b | c] means a AND (b OR c) — flattening it would
        # silently change the semantics to a AND b AND c). AssetRef/
        # AssetConsecutiveRef (e.g. a bare `asset.within(hours=24)` in the
        # list) previously matched none of the isinstance branches below and
        # silently vanished from the result — now they're appended too.
        assets: List[AssetAllOperand] = []
        for item in schedule:
            if isinstance(item, AssetAll):
                assets.extend(item.assets)
            elif isinstance(item, AssetAlias):
                # Expand alias assets
                assets.extend(item.assets)
            elif isinstance(item, AssetAny):
                # Mixed operators in list - keep as a nested OR-group operand
                assets.append(item)
            else:
                # Asset, AssetRef, AssetConsecutiveRef
                assets.append(item)
        
        return AssetAll(assets=assets)
    
    return None


def is_asset_triggered(dag: 'DAG') -> bool:
    """Check if DAG is triggered by assets (not time-based)."""
    schedule = getattr(dag, 'schedule', None)
    if schedule is None:
        return False
    
    # String schedule = time-based
    if isinstance(schedule, str):
        return False
    
    # Asset-based
    return isinstance(schedule, (Asset, AssetAll, AssetAny, list))


def get_asset_schedule_info(dag: 'DAG') -> Optional[Dict[str, Any]]:
    """
    Get asset schedule information for a DAG.
    
    Returns:
        {
            "operator": "AND" | "OR",
            "assets": ["raw/inventory", "raw/catalog", ...],
            "eventbridge_rule_pattern": {...}
        }
    """
    schedule = getattr(dag, 'schedule', None)
    normalized = normalize_asset_schedule(schedule)
    
    if normalized is None:
        return None
    
    result = normalized.to_dict()
    
    # Add EventBridge pattern
    if isinstance(normalized, AssetAll):
        # AND logic: Need to track state, trigger when all ready
        result["eventbridge_rule_pattern"] = {
            "source": ["polyris.assets"],
            "detail-type": ["Asset Materialized"],
            "detail": {
                "asset_name": normalized.asset_names
            }
        }
    elif isinstance(normalized, AssetAny):
        # OR logic: Trigger on any. EventBridge event patterns match on the
        # literal asset_name field, so AssetRef/AssetConsecutiveRef
        # contribute their underlying asset's .name here (not their full
        # to_dict(), which carries freshness/consecutive info that has no
        # meaning for pattern-matching an incoming event) — same as a plain
        # Asset. A nested AssetAll is flattened into its member names so the
        # EventBridge pattern is a flat array, not a nested structure.
        flat_names = []
        for a in normalized.assets:
            if isinstance(a, AssetAll):
                flat_names.extend(a.asset_names)
            else:
                flat_names.append(a.name)
        result["eventbridge_rule_pattern"] = {
            "source": ["polyris.assets"],
            "detail-type": ["Asset Materialized"],
            "detail": {
                "asset_name": flat_names
            }
        }
    
    return result


# ============================================
# Watcher - External Event Source
# ============================================
# FUTURE FEATURE: Backend ready, UI not implemented yet.
# Use case: Trigger pipelines from external systems (S3, webhooks, etc.) via SQS.

@dataclass
class Watcher:
    """
    [FUTURE FEATURE] Watcher for external events that produce assets.
    
    Watches an SQS queue for messages and triggers asset materialization events.
    Use this for external systems that don't directly emit to EventBridge.
    
    Args:
        asset: The Asset this watcher produces
        sqs_queue_arn: ARN of the SQS queue to watch
        message_filter: Optional JMESPath filter for messages
        transform: Optional mapping from message fields to metadata
    
    Example:
        # Watch SQS queue for S3 notifications
        inventory_watcher = Watcher(
            asset=inventory,
            sqs_queue_arn="${inventory_queue_arn}",
            message_filter="Records[?eventSource=='aws:s3']",
            transform={
                "bucket": "Records[0].s3.bucket.name",
                "key": "Records[0].s3.object.key"
            }
        )
        
        # Or watch for custom application events
        order_watcher = Watcher(
            asset=orders_asset,
            sqs_queue_arn="${orders_queue_arn}",
            transform={
                "order_count": "body.count",
                "region": "body.region"
            }
        )
    
    Backend Implementation:
        - Lambda triggered by SQS
        - Parses message, applies filter/transform
        - Emits "Asset Materialized" event to EventBridge
    """
    asset: Asset
    sqs_queue_arn: str
    message_filter: str = ""
    transform: Dict[str, str] = field(default_factory=dict)
    batch_size: int = 10
    enabled: bool = True
    
    def __post_init__(self):
        # Register watcher with asset
        if not hasattr(self.asset, '_watchers'):
            self.asset._watchers = []
        self.asset._watchers.append(self)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for backend configuration."""
        return {
            "asset_name": self.asset.name,
            "sqs_queue_arn": self.sqs_queue_arn,
            "message_filter": self.message_filter,
            "transform": self.transform,
            "batch_size": self.batch_size,
            "enabled": self.enabled
        }


def generate_watchers_config(watchers: List[Watcher]) -> Dict[str, Any]:
    """
    Generate configuration for SQS watcher Lambdas.
    
    Returns configuration for creating:
    - Lambda function (shared)
    - SQS event source mappings
    - IAM permissions
    
    Args:
        watchers: List of Watcher instances
    
    Returns:
        {
            "lambda_config": {...},
            "event_source_mappings": [...],
            "iam_statements": [...]
        }
    """
    if not watchers:
        return {"lambda_config": None, "event_source_mappings": [], "iam_statements": []}
    
    # Group by queue ARN to avoid duplicate event sources
    queue_watchers: Dict[str, List[Any]] = {}
    for w in watchers:
        if w.sqs_queue_arn not in queue_watchers:
            queue_watchers[w.sqs_queue_arn] = []
        queue_watchers[w.sqs_queue_arn].append(w)
    
    event_source_mappings = []
    for queue_arn, queue_watchers_list in queue_watchers.items():
        # Use max batch_size from watchers for this queue
        batch_size = max(w.batch_size for w in queue_watchers_list)
        event_source_mappings.append({
            "event_source_arn": queue_arn,
            "batch_size": batch_size,
            "watchers": [w.to_dict() for w in queue_watchers_list]
        })
    
    iam_statements = [
        {
            "effect": "Allow",
            "actions": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
            "resources": list(queue_watchers.keys())
        },
        {
            "effect": "Allow",
            "actions": ["events:PutEvents"],
            "resources": ["*"]  # EventBridge default bus
        }
    ]
    
    return {
        "lambda_config": {
            "handler": "watcher.handler",
            "runtime": "python3.11",
            "timeout": 30,
            "memory_size": 256
        },
        "event_source_mappings": event_source_mappings,
        "iam_statements": iam_statements
    }


# ─── DDL renderers ──────────────────────────────────────────────────────────
#
# Per-dialect helpers invoked by `Asset.to_ddl(dialect)`. Today there is only
# one — Glue/Hive — and it lives here as a module-private function. When the
# second dialect lands (BigQuery, Iceberg, Postgres, Snowflake, ...), the
# renderers move to a `polyris/renderers/` package and `to_ddl` becomes a
# small dispatcher over a registry. ADR #46 documents the trigger and the
# expected shape of that future refactor.
#
# Until then, keeping the helper at module scope (rather than inside Asset
# or in a sibling module) gives us:
#   - trivial extraction later (one cut/paste, no public-API change)
#   - parity-test friendliness (test imports the helper directly without
#     needing an Asset instance to compare against TS)
#   - no premature abstraction (no Renderer Protocol, no plugin registry,
#     no `renderers/` directory with one file in it)
# ────────────────────────────────────────────────────────────────────────────

def _render_glue_ddl(asset: Asset) -> str:
    """Render an asset as a Glue/Hive `CREATE EXTERNAL TABLE` statement.

    Output matches what Athena, Spark SQL, and Trino accept directly. Single
    quotes inside descriptions are doubled (Hive convention) so the DDL stays
    parseable. Partition columns are extracted into a `PARTITIONED BY (...)`
    clause and excluded from the main column list, matching Hive convention.

    The companion TypeScript implementation lives in
    `ui/src/utils/ddl-glue.ts` (UI ships independently of the SDK and cannot
    import Python). A parity test in `tests/sdk/test_ddl_parity.py` and
    `ui/src/utils/__tests__/ddl-glue-parity.test.ts` reads a shared fixture
    file (`tests/fixtures/ddl_parity.json`) and asserts byte-identical output
    from both renderers. Any change here must be reflected in the TS mirror
    or CI fails — see ADR #46.
    """
    from .schema import to_glue_string

    # Glue tables look like `database.table`; bare asset names go in
    # backticks so non-identifier characters survive.
    table_id = asset.glue_table or f"`{asset.name}`"

    regular: List[str] = []
    partitions: List[str] = []
    for col in asset.schema:
        line = f"  `{col.name}` {to_glue_string(col.type)}"
        if col.description:
            # Single-quote escape to keep DDL parseable.
            line += f" COMMENT '{col.description.replace(chr(39), chr(39) * 2)}'"
        (partitions if col.partition_key else regular).append(line)

    out = [f"CREATE EXTERNAL TABLE {table_id} ("]
    out.append(",\n".join(regular))
    out.append(")")
    if partitions:
        out.append("PARTITIONED BY (")
        out.append(",\n".join(partitions))
        out.append(")")
    if asset.description:
        esc = asset.description.replace("'", "''")
        out.append(f"COMMENT '{esc}'")
    if asset.uri and asset.uri.startswith(('s3://', 's3a://')):
        out.append(f"LOCATION '{asset.uri}'")
    return "\n".join(out)
