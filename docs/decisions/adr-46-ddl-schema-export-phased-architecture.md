# ADR #46 — DDL/Schema Export — Phased Architecture

**Status:** Phase 1 in v0.75.4. Phase 2 deferred until trigger fires.
Phase 3 deferred until trigger fires.

**Context:** `Asset.to_ddl()` renders Glue/Hive `CREATE EXTERNAL TABLE`
output for the "Copy as DDL" UI button. The Python implementation lives
in the SDK (`polyris/assets.py`); the UI bundle ships independently and
cannot import Python at runtime, so a TypeScript mirror exists in
`ui/src/utils/ddl-glue.ts`. This is duplication.

**Why duplication is acceptable here:** alternatives all have higher
cost than the duplication itself.

  - *Backend endpoint per click* — adds 100-200ms latency on every Copy
    button press. Copy-to-clipboard UX expects instant feedback;
    network round-trip breaks that.
  - *Pre-render DDL into `/api/assets` response* — adds ~25 KB of
    payload that 99% of users never use. Pollutes the API contract
    with a UI-only convenience.
  - *Pyodide / WASM in browser* — runs Python in JS. Massive cost
    (multi-MB bundle, slow startup) for one 50-line function.
  - *Compile Python → TS at SDK build time* — invented machinery, two
    moving parts to maintain.

The duplication itself is small (~50 lines, mirror logic), the format
is stable (Hive/Glue DDL is an AWS specification, not a Polyris
invention), and the divergence risk is **enforced down to zero** by a
shared fixture file plus parity tests on both sides.

**Decision — Phase 1 (current):**

  - `Asset.to_ddl()` is a thin dispatcher: validates dialect + schema,
    then calls a module-private `_render_glue_ddl(asset)` helper.
  - The TS mirror lives in `ui/src/utils/ddl-glue.ts` and exports
    `renderGlueDDL(input)` from a single named function.
  - A shared fixture file `tests/fixtures/ddl_parity.json` defines
    6 canonical input/expected pairs covering every renderer branch:
    simple, with_partition, with_description, with_uri, bare_name,
    all_features.
  - `tests/sdk/test_ddl_parity.py` (pytest) and
    `ui/src/utils/ddl-glue-parity.test.ts` (vitest) both load the
    fixture and assert byte-identical output. Either side drifting
    fails its own parity test in CI.

This is the minimum architecture that gives us:

  - Public API stability (`Asset.to_ddl(dialect)` unchanged forever).
  - Trivial Phase 2 extraction (one `git mv` of the helper).
  - Zero-drift guarantee via tests.
  - No premature `Renderer` Protocol, no `renderers/` directory with
    one file in it, no plugin registry without plugins to register.

**Decision — Phase 2 (deferred until trigger fires):**

Trigger: a second user-requested DDL dialect (BigQuery, Iceberg,
Postgres, Snowflake) lands.

Refactor outline:

  - Move `_render_glue_ddl` from `polyris/assets.py` to
    `polyris/renderers/glue.py`. New sibling files for each new
    dialect (`bigquery.py`, `iceberg.py`, ...).
  - Define a `Renderer` Protocol *only after seeing the second
    dialect's requirements* — this is the key timing point. Designing
    the interface from a single implementation (Phase 1 trap) almost
    always produces an interface that doesn't fit the second
    implementation, forcing a re-design.
  - `Asset.to_ddl(dialect)` continues to dispatch by string; only the
    internal call shape changes.
  - UI strategy splits at this point: instant-copy formats stay
    mirrored in `ui/src/utils/` (Glue, JSON Schema, Markdown — all
    AWS-shaped or trivial); less-common formats route through a new
    `GET /api/assets/{name}/export/{format}` endpoint. The latency on
    those is acceptable because they're rare-click actions.

The `tests/fixtures/ddl_parity.json` schema extends to support
multi-dialect entries (`expected_glue`, `expected_bigquery`, etc.) so
a single fixture can lock all dialects simultaneously.

**Decision — Phase 3 (deferred until trigger fires):**

Trigger: a user requests a custom dialect we don't ship (e.g.
"render Iceberg DDL with my company's table-format conventions").

Refactor outline:

  - Publish the `Renderer` Protocol as a public extension point.
  - Discover renderers via Python `entry_points` in `pyproject.toml`:

```toml
[project.entry-points."polyris.renderers"]
my_dialect = "my_pkg.renderers:MyDialectRenderer"
```

  - This matches the dbt-adapter pattern (`dbt-bigquery`, `dbt-snowflake`,
    `dbt-databricks` are all separate PyPI packages plugged in via
    entry_points).
  - UI handles unknown dialects by routing through the backend
    endpoint — the backend already loads the renderer registry, so
    Phase 3 doesn't require UI changes beyond a "more formats..." path.

**Why this phased approach beats "build the abstraction now":**

Designing the `Renderer` interface from a single Glue implementation
is the canonical YAGNI failure mode. The second implementation almost
always exposes an interface assumption that doesn't fit:

  - BigQuery has `partitioning_field` at the column level, not a
    separate `PARTITIONED BY` block.
  - Iceberg uses transforms (`bucket(N, col)`, `truncate(K, col)`).
  - Snowflake has `CLUSTER BY` post-hooks.
  - Postgres doesn't have external tables — the whole `EXTERNAL` /
    `LOCATION` shape needs a different abstraction.

Designing the interface *after* seeing the requirements of two
implementations gives an interface that fits both, with high odds of
also fitting the third. This is why dbt waited until they had ~5
working warehouse adapters before stabilizing their Adapter Protocol —
the early interface designs all needed major revisions.

**What "scaffolding for scalability" actually means in Phase 1:**

Not "build the abstraction". Build the code such that the *future
abstraction* is a 30-minute refactor instead of a 4-hour one:

  - Helper at module scope, not buried in a method body.
  - TS mirror in its own file, not inline in a component.
  - Public API (`Asset.to_ddl(dialect)`) shaped as if dispatch already
    happened — calls are already keyed by dialect string.
  - Fixture-driven parity tests that already store inputs in the
    multi-dialect-friendly shape (one fixture, currently one
    `expected` field, easily extended to `expected_<dialect>`).

This is the maximum forward-investment without paying for unused
abstraction today.
