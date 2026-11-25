# ADR #43 — Glue Schema Sync: On-Demand, Not Scheduled

**Decision:** When a user opens an asset's Schema tab, the UI fetches the
asset's actual schema from AWS Glue Catalog and shows a side-by-side diff
against the schema declared in code. Triggered on tab open; refreshable on
demand. No cron, no scheduled checks, no separate Lambda, no new DDB table.

**Context:** The `glue_table` field on `Asset` was declarative-only as of
v0.72 — stored, displayed in the sidebar, never compared against reality.
The natural follow-up was "detect drift between declared and Glue". Two
families of design existed:

1. **Scheduled checks** — EventBridge cron Lambda runs every N hours,
   compares every asset's declared schema with Glue, writes results to a
   new `asset_checks` DDB table, UI reads from DDB. This is what
   Dagster-style "Asset Checks" do conceptually.

2. **On-demand fetch** — page open triggers a single Glue API call for the
   current asset; result lives in browser cache. No background work.

We chose (2). Reasoning:

- **Cost.** (1) means thousands of Glue API calls per month even for a small
  catalog; (2) means one call per page view (~100/month for a typical team).
  At realistic scale (50 assets, 3 databases) the difference is 2,160 vs
  ~100 calls/month. Both are inside the Glue Catalog free tier today, but
  (2) stays inside it forever and (1) does not.
- **Complexity.** (1) requires: new Lambda, EventBridge rule, IAM, new DDB
  table, TTL for old results, retention policy, cleanup logic, per-asset
  interval config, pagination across batches. (2) requires: one new route,
  one IAM action, one React Query hook. ~50 lines of backend vs ~300.
- **Stale data.** (1) shows results up to N hours old by design. (2) shows
  whatever Glue says at the moment the user looked. The user is the only
  consumer, and they only care about freshness when they're looking.
- **Failure mode.** If a (1) cron stops running, drift goes silently
  undetected for days. If a (2) call fails, the user sees the error
  immediately and can act on it.
- **Competitor parity.** Dagster's `build_column_schema_change_checks`
  compares one materialization against the previous one (its own
  pipeline output across runs), not declared-vs-external-catalog.
  Drift-vs-Glue alerts are a Dagster Cloud paid feature, not core. There
  is no parity gap to close with (1).

The on-demand pattern is also a precedent for any future "asset health
check" that needs external data — fetch when looked at, not on a schedule.

**API:**

```
GET /api/assets/{name}/glue-schema   →  200 {
    asset_name, glue_table, glue_catalog,
    declared_schema:  [Column, ...],
    glue_schema:      [Column, ...] | null,
    diff: {in_sync, missing_in_glue, extra_in_glue, type_mismatches} | null,
    fetched_at,
    error: {code, message} | null
}
```

Returns 404 when the asset is not registered, 422 when the asset has no
`glue_table` or it is malformed (must be `database.table`). Glue API
failures are returned as 200 with `error` field populated and `glue_schema`
null — this lets the UI render the declared schema and a structured error
card alongside, rather than a blank network-error toast.

**Wire-format compatibility:** Glue Catalog returns column types as strings
(`"bigint"`, `"decimal(10,2)"`, `"array<string>"`). The polyris type system
emits the byte-identical format via `to_glue_string` (ADR #42). The diff
therefore reduces to string equality on `(name, type)` pairs. No parsing,
no normalization, no type system in the backend Lambda — `polyris` is not
imported there. Two separately-evolving systems converged on the same
format precisely because the format is Glue's, not ours.

**Constraint fields ignored in diff:** `nullable`, `primary_key`, `unique`,
`partition_key` are polyris Column metadata that Glue Catalog does not
represent. They cannot drift in either direction. Partition columns (which
Glue does represent) are surfaced from `Table.PartitionKeys` and merged
with regular columns in the response so a column declared with
`partition_key=True` matches its Glue counterpart.

**IAM:** the `console_api` Lambda role gains exactly one new permission:
`glue:GetTable` on `Resource: "*"`. Read-only, scoped to the single API
the route needs. Cross-account Glue access works via the optional
`Asset(glue_catalog="<account-id>")` parameter, surfaced as `CatalogId`
in the API call.

**UI behaviour:** `useAssetGlueSchemaQuery` is a React Query hook with
`enabled: activeTab === 'schema' && Boolean(glueTable)`, `staleTime: 5min`,
`refetchInterval: false`, `refetchOnWindowFocus: false`. So: opens the
Schema tab once → one Glue call. Switches tabs → cache hit. Refreshes
explicitly → one Glue call. No polling, ever.

**Out of scope (deferred):**

- Schema drift alerts (Slack/email when drift detected). Add in a later
  phase if a real user asks for it; in Dagster this is a paid feature.
- Drift detection across pipeline materializations
  (Dagster `build_column_schema_change_checks` style). Different concern;
  requires producer-side metadata emission.
- Pyarrow/Iceberg/BigQuery schema sync. Phase 3 (pyarrow bridge) makes
  these straightforward to add via the same pattern: one route per
  catalog system, one IAM action, no scheduled work.
- DDL generation (`CREATE TABLE` from declared schema, ALTER TABLE on diff).
  Useful but separate; needs more design around dialect handling.
- "Drift indicator" in the asset list view. Would require N Glue calls
  per page load — explicitly rejected; only the detail view fetches.

**Why not also offer a "scheduled" mode behind a feature flag?** Two code
paths is twice the maintenance for the same outcome. If a user with strong
real-time alerting needs shows up, the right answer is to add Slack
notifications to the on-demand result (one action, one place) rather than
to add a parallel cron infrastructure.
