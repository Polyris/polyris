# ADR #39 — Asset Enrichment: Schema Declaration + asset_registry Removal

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §8 (1.3.0).** Asset data comes from the specs of each pipeline's `spec_id`, not from the registry's `tasks`.

**Decision:** Add `owner`, `schema`, `glue_table`, `glue_catalog` to Asset DSL. Remove `asset_registry` DynamoDB table entirely. All asset data derived from `pipeline_registry.tasks` at query time.

**Context:** `asset_registry` table existed but was never written to by any SFN or Lambda. Three endpoints (`list_assets`, `delete_asset`, `delete_orphaned_assets`) read from it and returned empty/wrong results.

**Changes:**
- `_build_assets_from_pipelines()` shared helper — single source of truth
- `_serialize_outlet()` includes all new fields in pipeline registration
- `list_assets()` builds from pipeline_registry (was: empty asset_registry)
- `delete_asset()` purges asset_events (was: no-op on empty table)
- `delete_orphaned_assets()` compares asset_events vs pipeline_registry (was: always 0)
- Table, repo class, config, IAM — all removed (8→7 tables)
- `AssetDetailPage` UI component (6 tabs + sidebar)
