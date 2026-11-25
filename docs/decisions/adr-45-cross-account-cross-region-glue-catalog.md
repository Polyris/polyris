---
area: sdk
accepted: by 2026-05-04
---
# ADR #45 — Cross-Account / Cross-Region Glue Catalog Support

**Status:** Adopted in v0.75.1.

**Context:** Asset already had `glue_table` (`"database.table"`) and
`glue_catalog` (AWS account ID for cross-account references) fields. The
schema-fetch route (ADR #43) honoured `glue_catalog` by passing
`CatalogId=` to `glue.get_table()`. But three gaps were silent:

1. The Glue API call always used the Lambda's default region. An asset
   declared with a Glue Catalog in another region (e.g. Lambda in
   `us-east-1`, target catalog in `eu-west-1`) returned
   `EntityNotFoundException` — the table existed, but in a different
   region than the request.
2. `Asset.from_glue_table(...)` accepted a `region` kwarg used for the
   deploy-time fetch but did not persist it. Authoring worked,
   runtime drift detection silently mis-targeted.
3. Two pipelines pulling `default.example` from different AWS accounts
   collapsed into one asset entry because the default name strategy
   `from_glue_table("db.t", catalog_id="222")` produced `"db.t"` for
   both accounts. Backend `_build_assets_from_pipelines` merged them.

**Decision:**

  - Add `Asset.glue_region: str = ''`. Empty string preserves current
    same-region behaviour; non-empty string pins the boto3 client to
    that region.
  - Add `config.get_glue_client(region: str)` factory with per-region
    caching (Lambda containers reuse, so memoization avoids recreating
    boto3 clients on every invocation). Empty region returns the
    same default-region client the legacy `glue` proxy uses — single
    source of truth, no duplication.
  - `from_glue_table(region=...)` persists the value into `glue_region`
    on the resulting Asset.
  - `from_glue_table` default name becomes
    `"{catalog_id}.{database}.{table}"` when `catalog_id` is non-empty,
    `"{database}.{table}"` when empty. Local-account behaviour is
    unchanged; cross-account references get unique names by default
    without forcing the user to pass `name=`.
  - `glue_region` joins the existing pipeline_registry serialization,
    Console API response, and last-writer-wins enrichment in
    `_build_assets_from_pipelines`.
  - UI: GlueSyncPanel header gains a scope subtitle showing
    `account 222... · eu-west-1` when either field is non-empty. The
    local-account, local-region case (most users) sees no clutter.
    Sidebar adds explicit "Account: ..." / "Region: ..." rows so the
    Overview tab also surfaces scope.

**`glue_table` validation at construction:** Constructor now rejects
malformed values up front (must contain exactly one `.` with both sides
non-empty). The validation lands the failure in the developer's editor,
not as a 422 from the Console API after deploy. CLAUDE.md #4 — fail
early, fail loud.

**Why not split into `glue_database` + `glue_name` fields?**
Considered. Rejected. The `glue_table="database.table"` form is the
canonical short form 99% of users want — three fields where one suffices
violates CLAUDE.md #4 (don't break what works) and adds API surface for
no real win. The dot-in-table-name edge case (Glue technically permits
it, rare in practice) is documented; if a real user hits it we'll add
an escape mechanism then per CLAUDE.md #5.

**Why prepend account ID, not append?**
`"222.default.example"` reads left-to-right as "in account 222, table
default.example". The opposite (`"default.example.222"`) blurs into the
table name and breaks the `database.table` mental model that the rest
of the codebase preserves.

**IAM caveat (out of scope):** Cross-account drift detection requires
both (a) `glue:GetTable` on the target ARN in the Console API Lambda's
role and (b) a resource policy on the target catalog/table or a Lake
Formation share granting access. This ADR adds the *mechanism*; users
configuring cross-account references must arrange the *permissions*
themselves. The friendly Glue-error mapping (ADR #43, refined in v0.75.0)
already covers the `AccessDeniedException` case — it tells users
"add `glue:GetTable` on this catalog" without pretending we can
provision it for them.

**Out of scope (deferred):**

  - Auto-detect own-account / own-region and elide redundant scope
    info on the Asset constructor side. We don't know the Lambda's
    account from the SDK side, so this would require a runtime callback
    or env var. Not worth the complexity until a user complains about
    verbose default names from `from_glue_table(catalog_id=own)`.
  - Multi-region drift comparison (i.e. compare a single asset against
    its replicas in multiple regions). Niche; revisit if asked.

**Out of scope (by design — won't add):**

  - **Athena Federated Catalogs.** Athena's UI shows a four-level
    hierarchy (Data source → Catalog → Database → Table) where "Data
    source" can be a Lambda-backed connector to Hive, MySQL, Snowflake,
    etc. Polyris targets AWS Glue Data Catalog directly via
    `glue.get_table()`. Users who need to drift-detect against
    federated sources should use the source-of-truth catalog's own
    APIs, not Polyris.
  - **Athena DataSource aliases.** Cross-account Glue catalogs
    registered as Athena data sources (e.g.
    `Central_Data_Catalog` pointing at account 222) are Athena UI
    conveniences. The underlying catalog is still a Glue Data Catalog
    addressable by its `CatalogId`; users should set
    `glue_catalog="222..."` directly rather than referencing the
    Athena alias.

**Lake Formation interaction (operator concern, not an API change):**
If the target Glue Data Catalog is managed by AWS Lake Formation, IAM
permissions alone are insufficient — Lake Formation's GRANT/REVOKE
permissions gate access independently. Without an LF data permission
on the Console API Lambda's role, drift detection returns
`AccessDeniedException` even when IAM is correct. The friendly Glue
error mapping (ADR #43) already surfaces this with an actionable hint
("the Console API Lambda needs `glue:GetTable` on this catalog").
Users should coordinate with the catalog owner to add an LF GRANT if
the table reports `IsRegisteredWithLakeFormation: true`.
