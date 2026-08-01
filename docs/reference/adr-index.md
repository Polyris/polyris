# ADR Index

Every architecture decision, in one place. ADRs live in **two** forms and
always have — this index is the map so you never have to guess where one is:

- **Inline** in [`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md) — the majority, especially earlier ones.
- **Standalone** `adr-NN-*.md` files — used for larger or self-contained decisions.

_94 ADRs indexed (73 inline, 21 standalone). Regenerate with the snippet in this repo's docs tooling; do not hand-edit rows._

| # | Title | Where |
|---|-------|-------|
| 1 | Alerts Configured in the UI, Not the DSL | inline |
| 2 | SFN-Based Dependency Notification (not polling, not EventBridge) | inline |
| 3 | Separate Helpers (not monolithic wrapper) | inline |
| 4 | Asset-Based Orchestration (not ExternalTaskSensor) | inline |
| 5 | DynamoDB for State (not Step Functions native, not CloudWatch metrics) | inline |
| 6 | Polling for UI Updates (simple and reliable) | inline |
| 7 | Code Splitting for UI Bundle | inline |
| 8 | JSONata for Step Functions (not JSONPath) | inline |
| 9 | Pulumi for Pipelines, Terraform for Shared | inline |
| 10 | Single Execution ID Pattern + `pipeline_execution_short` | inline |
| 11 | Backfill Failure: Wait for Decision (not auto-fail) | inline |
| 12 | Explicit `skip_on_backfill` Parameter (not heuristic) | inline |
| 13 | Decorator Parameter Duplication (for IDE autocomplete) | inline |
| 14 | Canonical Output Key (not run-specific) | inline |
| 15 | Upstream Read-Time Truncation (25KB per dependency) | inline |
| 16 | Inlets/Outlets for Lineage Only (not orchestration) | inline |
| 17 | Task Output: Truncate vs S3 Claim Check | inline |
| 18 | Standard vs Express Step Functions | inline |
| 19 | Zero-Cost Waiting (waitForTaskToken + .sync) | inline |
| 20 | Transparency: Step Functions over EventBridge for Orchestration | inline |
| 21 | Single Source of Truth for Date Variables | inline |
| 22 | Data Flow: SFN Executes → DynamoDB Stores → UI Reads | inline |
| 23 | DAG Snapshot Per Execution (not per deploy) | inline |
| 24 | Pulumi Dynamic Provider for Pipeline Lifecycle (not EventBridge) | inline |
| 25 | Complete DAL Migration — Zero Direct Table Access in Routes | inline |
| 26 | pytest-mock as Standard Test Mocking Library | inline |
| 27 | Unified Version String Across All Packages | inline |
| 28 | Specific Exception Types in Route Handlers | inline |
| 29 | Generators DRY: Dispatch Dict, Shared Builders, Centralized Constants | inline |
| 30 | SSM Parameter Store instead of Terraform Remote State for Pipeline Configuration | inline |
| 31 | Removal of `config.arn()` and `[tool.polyris.accounts]` | inline |
| 32 | OpenTofu instead of Terraform + native S3 locking | inline |
| 33 | Terraform as a Public Module | inline |
| 34 | AWS SAM instead of OpenTofu for Shared Infrastructure | inline |
| 35 | `polyris-deploy` as a Parallel Alternative to Pulumi | inline |
| 36 | SSM as a Cross-Stack Bridge (instead of `Fn::ImportValue`) | inline |
| 37 | `DefinitionUri` + `AWS::Serverless::StateMachine` for SFN Definitions | inline |
| 38 | Error Visibility — Product Requirement | inline |
| 39 | Asset Enrichment: Schema Declaration + asset_registry Removal | inline |
| 40 | Responsive Layout: Flex Chains, No Viewport Magic Numbers | inline |
| 41 | URL Routing: CloudFront Function + window.history for Deep State | inline |
| 42 | Asset Schema 2.0: Typed `Column` Class with Platform-Agnostic Type System | inline |
| 43 | Glue Schema Sync: On-Demand, Not Scheduled | inline |
| 44 | Schema Adapters: pyarrow / pydantic / Glue as `Asset.from_*` Constructors | inline |
| 45 | Cross-Account / Cross-Region Glue Catalog Support | inline |
| 46 | DDL/Schema Export — Phased Architecture | inline |
| 47 | Asset Lineage `last_updated` Enrichment — Two Endpoints, Two Scopes | inline |
| 48 | AssetDetailPage Composition — Tabs as Independent Sub-Components | inline |
| 49 | Asset Matrix View — Cross-asset temporal grid (v0.76.0) | inline |
| 50 | Asset Granularity — declarative DSL + advisory Glue auto-detect + drift detection (v0.77.0) | inline |
| 51 | Backfill Unification — one concept, one endpoint, one bulk-run SFN (v0.78.0) | inline |
| 52 | Pipeline Granularity — runtime cron cadence inference, no DSL change (v0.78.0) | inline |
| 53 | Cost Preview Methodology — formula, sources, accuracy disclaimer (v0.78.0) | inline |
| 54 | Bulk-Backfill SFN — Standard, Map-based, fire-and-wait architecture (v0.78.0) | inline |
| 55 | Scheduled Pipeline Runs — direct EventBridge → SFN, not routed through bulk-backfill (v0.78.0) | inline |
| 56 | Backfill Status Model — six states, aggregation rules, mapping to execution statuses (v0.78.0) | inline |
| 57 | Cascade Semantics — auto / all / none with non-transitive propagation (v0.78.0) | inline |
| 58 | Partition Keys & Granularity-Aware Range Expansion (v0.78.0) | inline |
| 59 | Backfill options propagated to child SFN — skip_tasks, force, cascade_all, suppress_asset_event (v0.78.0) | inline |
| 60 | Runtime enforcement of `skip_on_backfill` task flag (v0.78.1) | inline |
| 61 | Task-subset selection in backfill UI (v0.78.1) | inline |
| 62 | Backfill cost estimate removed pending Pro tier (v0.78.2) | inline |
| 63 | Backfill detail → pipeline DAG navigation (v0.78.2) | inline |
| 64 | Standard keyboard shortcut convention (v0.78.3) | inline |
| 65 | API Tokens (PAT) & Auth Enforcement | [`adr-65-api-tokens-and-auth-enforcement.md`](adr-65-api-tokens-and-auth-enforcement.md) |
| 66 | Per-Token Scopes (Granular Authorization) | [`adr-66-per-token-scopes.md`](adr-66-per-token-scopes.md) |
| 94 | Runtime config precedence (window.CONFIG over baked NEXT_PUBLIC_*) | [`adr-94-runtime-config-precedence.md`](adr-94-runtime-config-precedence.md) |
| 96 | CloudFront security response headers (static export) | inline |
| 97 | Explicit plugin route registration (open-core seam) | [`adr-97-plugin-route-registration.md`](adr-97-plugin-route-registration.md) |
| 98 | Open-core split structure (proprietary roots, no symlink) | [`adr-98-open-core-split-structure.md`](adr-98-open-core-split-structure.md) |
| 99 | UI open-core exclusion (generated active module) | [`adr-99-ui-open-core-exclusion.md`](adr-99-ui-open-core-exclusion.md) |
| 100 | Tier entitlement (team↔enterprise gated at runtime, not by strip) | [`adr-100-tier-entitlement.md`](adr-100-tier-entitlement.md) |
| 101 | Multi-tenant readiness (entitlement is tenant-source-agnostic; tenancy deferred) | [`adr-101-multi-tenant-readiness.md`](adr-101-multi-tenant-readiness.md) |
| 102 | Repo split (Dagster model): public core repo + private paid repo | [`adr-102-repo-split-dagster-model.md`](adr-102-repo-split-dagster-model.md) |
| 103 | Alerting consolidation: `alerts=` DSL param removed, config moved to `alert_config`; interactive Slack/PagerDuty state machines replaced by `lambda:invoke` calls to the notify Lambda; PagerDuty fires immediately during the decision-wait pause | inline |
| 104 | Open-core CLI split, backfill nav-tab slot, and contract-drift cleanup | inline |
| 105 | Asset console gated to Team with an open-core "coming soon" page; engine stays free | inline |
| 106 | Parameter parity for `@task` decorators (the `task_config` ↔ `Run_Task_<X>` contract) | [`adr-106-task-param-parity.md`](adr-106-task-param-parity.md) |
| 107 | In-place retry loop in the `run_task` wrapper (per-task `retries` / `retry_delay`) | [`adr-107-wrapper-retry-loop.md`](adr-107-wrapper-retry-loop.md) |
| 108 | `TaskConfigKey`: shared constants for the SDK↔wrapper `task_config` contract | [`adr-108-task-config-key-constants.md`](adr-108-task-config-key-constants.md) |
| 109 | Unified common parameters for `@task.<type>` decorators (`CommonTaskKwargs`) | [`adr-109-unified-task-decorator-kwargs.md`](adr-109-unified-task-decorator-kwargs.md) |
| 110 | Task & execution intervention moved from Team to free; config mutation stays paid | [`adr-110-intervention-tier-flip.md`](adr-110-intervention-tier-flip.md) |
| 111 | Client-side view routing | [`adr-111-client-side-view-routing.md`](adr-111-client-side-view-routing.md) |
| 112 | Canonical execution-status derivation and reconciliation | [`adr-112-canonical-execution-status.md`](adr-112-canonical-execution-status.md) |
| 113 | History feed pages on a `started_at` cursor; `next` is the only end-of-feed signal | inline |
| 114 | Intervention-first failure model | [`adr-114-intervention-first-failure-model.md`](adr-114-intervention-first-failure-model.md) |
| 115 | Canonical trigger rules and skip semantics | [`adr-115-canonical-trigger-rules-and-skip-semantics.md`](adr-115-canonical-trigger-rules-and-skip-semantics.md) |
| 116 | Cleanup via scoped de-abort (deferred to Phase 2) | [`adr-116-cleanup-scoped-deabort.md`](adr-116-cleanup-scoped-deabort.md) |
| 117 | Trigger rules trimmed from 11 to 5 (removes 4 duplicates + 2 never-satisfiable) | [`adr-117-trigger-rule-trim-11-to-5.md`](adr-117-trigger-rule-trim-11-to-5.md) |
| 118 | `polyris-deploy --all` / `--only` — bulk deploy and destroy across directories | inline |
| 119 | Unified DAG visualization node/edge builder (`generate_dag_json` + `_build_pipeline_metadata`) | inline |
| 120 | Manual task actions: recorded input, synthetic output markers, asset events on Mark Successful, and safe direct Restart from `waiting_decision` | inline |
| 121 | Restart correctness: stop both wrapper levels in the right order, reconstruct `task_config`/`outlets` from the registry, `restart-` name prefix | inline |
| 122 | Registration fast-path resolves `deps_skip`/`deps_blocked`; `evaluate_deps` gates on `assets_ready` for wait_for coordination | [`adr-122-registration-fast-path-and-asset-coordination.md`](adr-122-registration-fast-path-and-asset-coordination.md) |
