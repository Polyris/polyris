# ADR Index

Every architecture decision has one file in this folder, `adr-N-<slug>.md`.

_111 rows indexed. Actual ADR count is higher — ADRs 67–93 are not yet backfilled into this index._

| # | Title | Where |
|---|-------|-------|
| 1 | Alerts Configured in the UI, Not the DSL | [`adr-1-alerts-configured-in-ui.md`](adr-1-alerts-configured-in-ui.md) |
| 2 | SFN-Based Dependency Notification (not polling, not EventBridge) | [`adr-2-sfn-dependency-notification.md`](adr-2-sfn-dependency-notification.md) |
| 3 | Separate Helpers (not monolithic wrapper) | [`adr-3-separate-helpers.md`](adr-3-separate-helpers.md) |
| 4 | Asset-Based Orchestration (not ExternalTaskSensor) | [`adr-4-asset-based-orchestration.md`](adr-4-asset-based-orchestration.md) |
| 5 | DynamoDB for State (not Step Functions native, not CloudWatch metrics) | [`adr-5-dynamodb-for-state.md`](adr-5-dynamodb-for-state.md) |
| 6 | Polling for UI Updates (simple and reliable) | [`adr-6-polling-for-ui-updates.md`](adr-6-polling-for-ui-updates.md) |
| 7 | Code Splitting for UI Bundle | [`adr-7-code-splitting-ui-bundle.md`](adr-7-code-splitting-ui-bundle.md) |
| 8 | JSONata for Step Functions (not JSONPath) | [`adr-8-jsonata-for-step-functions.md`](adr-8-jsonata-for-step-functions.md) |
| 9 | Pulumi for Pipelines, Terraform for Shared | [`adr-9-pulumi-and-terraform.md`](adr-9-pulumi-and-terraform.md) |
| 10 | Single Execution ID Pattern + `pipeline_execution_short` | [`adr-10-single-execution-id.md`](adr-10-single-execution-id.md) |
| 11 | Backfill Failure: Wait for Decision (not auto-fail) | [`adr-11-backfill-failure-wait-decision-auto-fail.md`](adr-11-backfill-failure-wait-decision-auto-fail.md) |
| 12 | Explicit `skip_on_backfill` Parameter (not heuristic) | [`adr-12-explicit-skip-backfill-parameter-heuristic.md`](adr-12-explicit-skip-backfill-parameter-heuristic.md) |
| 13 | Decorator Parameter Duplication (for IDE autocomplete) | [`adr-13-decorator-parameter-duplication-ide-autocomplete.md`](adr-13-decorator-parameter-duplication-ide-autocomplete.md) |
| 14 | Canonical Output Key (not run-specific) | [`adr-14-canonical-output-key-run-specific.md`](adr-14-canonical-output-key-run-specific.md) |
| 15 | Upstream Read-Time Truncation (25KB per dependency) | [`adr-15-upstream-read-time-truncation-25kb-per.md`](adr-15-upstream-read-time-truncation-25kb-per.md) |
| 16 | Inlets/Outlets for Lineage Only (not orchestration) | [`adr-16-inlets-outlets-lineage-only-orchestration.md`](adr-16-inlets-outlets-lineage-only-orchestration.md) |
| 17 | Task Output: Truncate vs S3 Claim Check | [`adr-17-task-output-truncate-vs-s3-claim.md`](adr-17-task-output-truncate-vs-s3-claim.md) |
| 18 | Standard vs Express Step Functions | [`adr-18-standard-vs-express-step-functions.md`](adr-18-standard-vs-express-step-functions.md) |
| 19 | Zero-Cost Waiting (waitForTaskToken + .sync) | [`adr-19-zero-cost-waiting-waitfortasktoken-sync.md`](adr-19-zero-cost-waiting-waitfortasktoken-sync.md) |
| 20 | Transparency: Step Functions over EventBridge for Orchestration | [`adr-20-transparency-step-functions-over-eventbridge-orchestration.md`](adr-20-transparency-step-functions-over-eventbridge-orchestration.md) |
| 21 | Single Source of Truth for Date Variables | [`adr-21-single-source-truth-date-variables.md`](adr-21-single-source-truth-date-variables.md) |
| 22 | Data Flow: SFN Executes → DynamoDB Stores → UI Reads | [`adr-22-data-flow-sfn-executes-dynamodb-stores.md`](adr-22-data-flow-sfn-executes-dynamodb-stores.md) |
| 23 | DAG Snapshot Per Execution (not per deploy) | [`adr-23-dag-snapshot-per-execution-per-deploy.md`](adr-23-dag-snapshot-per-execution-per-deploy.md) |
| 24 | Pulumi Dynamic Provider for Pipeline Lifecycle (not EventBridge) | [`adr-24-pulumi-dynamic-provider-pipeline-lifecycle-eventbridge.md`](adr-24-pulumi-dynamic-provider-pipeline-lifecycle-eventbridge.md) |
| 25 | Complete DAL Migration — Zero Direct Table Access in Routes | [`adr-25-complete-dal-migration-zero-direct-table.md`](adr-25-complete-dal-migration-zero-direct-table.md) |
| 26 | pytest-mock as Standard Test Mocking Library | [`adr-26-pytest-mock-as-standard-test-mocking.md`](adr-26-pytest-mock-as-standard-test-mocking.md) |
| 27 | Unified Version String Across All Packages | [`adr-27-unified-version-string-across-all-packages.md`](adr-27-unified-version-string-across-all-packages.md) |
| 28 | Specific Exception Types in Route Handlers | [`adr-28-specific-exception-types-route-handlers.md`](adr-28-specific-exception-types-route-handlers.md) |
| 29 | Generators DRY: Dispatch Dict, Shared Builders, Centralized Constants | [`adr-29-generators-dry-dispatch-dict-shared-builders.md`](adr-29-generators-dry-dispatch-dict-shared-builders.md) |
| 30 | SSM Parameter Store instead of Terraform Remote State for Pipeline Configuration | [`adr-30-ssm-parameter-store-instead-terraform-remote.md`](adr-30-ssm-parameter-store-instead-terraform-remote.md) |
| 31 | Removal of `config.arn()` and `[tool.polyris.accounts]` | [`adr-31-removal-config-arn-tool-polyris-accounts.md`](adr-31-removal-config-arn-tool-polyris-accounts.md) |
| 32 | OpenTofu instead of Terraform + native S3 locking | [`adr-32-opentofu-instead-terraform-native-s3-locking.md`](adr-32-opentofu-instead-terraform-native-s3-locking.md) |
| 33 | Terraform as a Public Module | [`adr-33-terraform-as-public-module.md`](adr-33-terraform-as-public-module.md) |
| 34 | AWS SAM instead of OpenTofu for Shared Infrastructure | [`adr-34-aws-sam-instead-opentofu-shared-infrastructure.md`](adr-34-aws-sam-instead-opentofu-shared-infrastructure.md) |
| 35 | `polyris-deploy` as a Parallel Alternative to Pulumi | [`adr-35-polyris-deploy-as-parallel-alternative-pulumi.md`](adr-35-polyris-deploy-as-parallel-alternative-pulumi.md) |
| 36 | SSM as a Cross-Stack Bridge (instead of `Fn::ImportValue`) | [`adr-36-ssm-as-cross-stack-bridge-instead.md`](adr-36-ssm-as-cross-stack-bridge-instead.md) |
| 37 | `DefinitionUri` + `AWS::Serverless::StateMachine` for SFN Definitions | [`adr-37-definitionuri-aws-serverless-statemachine-sfn-definitions.md`](adr-37-definitionuri-aws-serverless-statemachine-sfn-definitions.md) |
| 38 | Error Visibility — Product Requirement | [`adr-38-error-visibility-product-requirement.md`](adr-38-error-visibility-product-requirement.md) |
| 39 | Asset Enrichment: Schema Declaration + asset_registry Removal | [`adr-39-asset-enrichment-schema-declaration-asset-registry.md`](adr-39-asset-enrichment-schema-declaration-asset-registry.md) |
| 40 | Responsive Layout: Flex Chains, No Viewport Magic Numbers | [`adr-40-responsive-layout-flex-chains-no-viewport.md`](adr-40-responsive-layout-flex-chains-no-viewport.md) |
| 41 | URL Routing: CloudFront Function + window.history for Deep State | [`adr-41-url-routing-cloudfront-function-window-history.md`](adr-41-url-routing-cloudfront-function-window-history.md) |
| 42 | Asset Schema 2.0: Typed `Column` Class with Platform-Agnostic Type System | [`adr-42-asset-schema-2-0-typed-column.md`](adr-42-asset-schema-2-0-typed-column.md) |
| 43 | Glue Schema Sync: On-Demand, Not Scheduled | [`adr-43-glue-schema-sync-demand-scheduled.md`](adr-43-glue-schema-sync-demand-scheduled.md) |
| 44 | Schema Adapters: pyarrow / pydantic / Glue as `Asset.from_*` Constructors | [`adr-44-schema-adapters-pyarrow-pydantic-glue-as.md`](adr-44-schema-adapters-pyarrow-pydantic-glue-as.md) |
| 45 | Cross-Account / Cross-Region Glue Catalog Support | [`adr-45-cross-account-cross-region-glue-catalog.md`](adr-45-cross-account-cross-region-glue-catalog.md) |
| 46 | DDL/Schema Export — Phased Architecture | [`adr-46-ddl-schema-export-phased-architecture.md`](adr-46-ddl-schema-export-phased-architecture.md) |
| 47 | Asset Lineage `last_updated` Enrichment — Two Endpoints, Two Scopes | [`adr-47-asset-lineage-last-updated-enrichment-two.md`](adr-47-asset-lineage-last-updated-enrichment-two.md) |
| 48 | AssetDetailPage Composition — Tabs as Independent Sub-Components | [`adr-48-assetdetailpage-composition-tabs-as-independent-sub.md`](adr-48-assetdetailpage-composition-tabs-as-independent-sub.md) |
| 49 | Asset Matrix View — Cross-asset temporal grid (v0.76.0) | [`adr-49-asset-matrix-view-cross-asset-temporal.md`](adr-49-asset-matrix-view-cross-asset-temporal.md) |
| 50 | Asset Granularity — declarative DSL + advisory Glue auto-detect + drift detection (v0.77.0) | [`adr-50-asset-granularity-declarative-dsl-advisory-glue.md`](adr-50-asset-granularity-declarative-dsl-advisory-glue.md) |
| 51 | Backfill Unification — one concept, one endpoint, one bulk-run SFN (v0.78.0) | [`adr-51-backfill-unification-concept-endpoint-bulk-run.md`](adr-51-backfill-unification-concept-endpoint-bulk-run.md) |
| 52 | Pipeline Granularity — runtime cron cadence inference, no DSL change (v0.78.0) | [`adr-52-pipeline-granularity-runtime-cron-cadence-inference.md`](adr-52-pipeline-granularity-runtime-cron-cadence-inference.md) |
| 53 | Cost Preview Methodology — formula, sources, accuracy disclaimer (v0.78.0) | [`adr-53-cost-preview-methodology-formula-sources-accuracy.md`](adr-53-cost-preview-methodology-formula-sources-accuracy.md) |
| 54 | Bulk-Backfill SFN — Standard, Map-based, fire-and-wait architecture (v0.78.0) | [`adr-54-bulk-backfill-sfn-standard-map-based.md`](adr-54-bulk-backfill-sfn-standard-map-based.md) |
| 55 | Scheduled Pipeline Runs — direct EventBridge → SFN, not routed through bulk-backfill (v0.78.0) | [`adr-55-scheduled-pipeline-runs-direct-eventbridge-sfn.md`](adr-55-scheduled-pipeline-runs-direct-eventbridge-sfn.md) |
| 56 | Backfill Status Model — six states, aggregation rules, mapping to execution statuses (v0.78.0) | [`adr-56-backfill-status-model-six-states-aggregation.md`](adr-56-backfill-status-model-six-states-aggregation.md) |
| 57 | Cascade Semantics — auto / all / none with non-transitive propagation (v0.78.0) | [`adr-57-cascade-semantics-auto-all-none-non.md`](adr-57-cascade-semantics-auto-all-none-non.md) |
| 58 | Partition Keys & Granularity-Aware Range Expansion (v0.78.0) | [`adr-58-partition-keys-granularity-aware-range-expansion.md`](adr-58-partition-keys-granularity-aware-range-expansion.md) |
| 59 | Backfill options propagated to child SFN — skip_tasks, force, cascade_all, suppress_asset_event (v0.78.0) | [`adr-59-backfill-options-propagated-child-sfn-skip.md`](adr-59-backfill-options-propagated-child-sfn-skip.md) |
| 60 | Runtime enforcement of `skip_on_backfill` task flag (v0.78.1) | [`adr-60-runtime-enforcement-skip-backfill-task-flag.md`](adr-60-runtime-enforcement-skip-backfill-task-flag.md) |
| 61 | Task-subset selection in backfill UI (v0.78.1) | [`adr-61-task-subset-selection-backfill-ui.md`](adr-61-task-subset-selection-backfill-ui.md) |
| 62 | Backfill cost estimate removed pending Pro tier (v0.78.2) | [`adr-62-backfill-cost-estimate-removed-pending-pro.md`](adr-62-backfill-cost-estimate-removed-pending-pro.md) |
| 63 | Backfill detail → pipeline DAG navigation (v0.78.2) | [`adr-63-backfill-detail-pipeline-dag-navigation.md`](adr-63-backfill-detail-pipeline-dag-navigation.md) |
| 64 | Standard keyboard shortcut convention (v0.78.3) | [`adr-64-standard-keyboard-shortcut-convention.md`](adr-64-standard-keyboard-shortcut-convention.md) |
| 65 | API Tokens (PAT) & Auth Enforcement | [`adr-65-api-tokens-and-auth-enforcement.md`](adr-65-api-tokens-and-auth-enforcement.md) |
| 66 | Per-Token Scopes (Granular Authorization) | [`adr-66-per-token-scopes.md`](adr-66-per-token-scopes.md) |
| 94 | Runtime config precedence (window.CONFIG over baked NEXT_PUBLIC_*) | [`adr-94-runtime-config-precedence.md`](adr-94-runtime-config-precedence.md) |
| 96 | CloudFront security response headers (static export) | [`adr-96-cloudfront-security-response-headers-static-export.md`](adr-96-cloudfront-security-response-headers-static-export.md) |
| 97 | Explicit plugin route registration (open-core seam) | [`adr-97-plugin-route-registration.md`](adr-97-plugin-route-registration.md) |
| 98 | Open-core split structure (proprietary roots, no symlink) | [`adr-98-open-core-split-structure.md`](adr-98-open-core-split-structure.md) |
| 99 | UI open-core exclusion (generated active module) | [`adr-99-ui-open-core-exclusion.md`](adr-99-ui-open-core-exclusion.md) |
| 100 | Tier entitlement (team↔enterprise gated at runtime, not by strip) | [`adr-100-tier-entitlement.md`](adr-100-tier-entitlement.md) |
| 101 | Multi-tenant readiness (entitlement is tenant-source-agnostic; tenancy deferred) | [`adr-101-multi-tenant-readiness.md`](adr-101-multi-tenant-readiness.md) |
| 102 | Repo split (Dagster model): public core repo + private paid repo | [`adr-102-repo-split-dagster-model.md`](adr-102-repo-split-dagster-model.md) |
| 103 | Alert delivery is runtime config in the registry, read by one notify Lambda | [`adr-103-alert-config-and-notify-lambda.md`](adr-103-alert-config-and-notify-lambda.md) |
| 104 | Open-core CLI split, backfill nav-tab slot, and contract-drift cleanup | [`adr-104-open-core-cli-split-backfill-nav.md`](adr-104-open-core-cli-split-backfill-nav.md) |
| 105 | Asset console gated to Team with an open-core "coming soon" page; engine stays free | [`adr-105-asset-console-gated-team-open-core.md`](adr-105-asset-console-gated-team-open-core.md) |
| 106 | Parameter parity for `@task` decorators (the `task_config` ↔ `Run_Task_<X>` contract) | [`adr-106-task-param-parity.md`](adr-106-task-param-parity.md) |
| 107 | In-place retry loop in the `run_task` wrapper (per-task `retries` / `retry_delay`) | [`adr-107-wrapper-retry-loop.md`](adr-107-wrapper-retry-loop.md) |
| 108 | `TaskConfigKey`: shared constants for the SDK↔wrapper `task_config` contract | [`adr-108-task-config-key-constants.md`](adr-108-task-config-key-constants.md) |
| 109 | Unified common parameters for `@task.<type>` decorators (`CommonTaskKwargs`) | [`adr-109-unified-task-decorator-kwargs.md`](adr-109-unified-task-decorator-kwargs.md) |
| 110 | Task & execution intervention moved from Team to free; config mutation stays paid | [`adr-110-intervention-tier-flip.md`](adr-110-intervention-tier-flip.md) |
| 111 | Client-side view routing | [`adr-111-client-side-view-routing.md`](adr-111-client-side-view-routing.md) |
| 112 | Canonical execution-status derivation and reconciliation | [`adr-112-canonical-execution-status.md`](adr-112-canonical-execution-status.md) |
| 113 | History feed pages on a `started_at` cursor; `next` is the only end-of-feed signal | [`adr-113-history-feed-started-at-cursor.md`](adr-113-history-feed-started-at-cursor.md) |
| 114 | Intervention-first failure model | [`adr-114-intervention-first-failure-model.md`](adr-114-intervention-first-failure-model.md) |
| 115 | Canonical trigger rules and skip semantics | [`adr-115-canonical-trigger-rules-and-skip-semantics.md`](adr-115-canonical-trigger-rules-and-skip-semantics.md) |
| 116 | Cleanup via scoped de-abort (superseded by ADR #138) | [`adr-116-cleanup-scoped-deabort.md`](adr-116-cleanup-scoped-deabort.md) |
| 117 | Trigger rules trimmed from 11 to 5 (superseded by ADR #138) | [`adr-117-trigger-rule-trim-11-to-5.md`](adr-117-trigger-rule-trim-11-to-5.md) |
| 118 | `polyris-deploy --all` / `--only` — bulk deploy and destroy across directories | [`adr-118-polyris-deploy-all-only-bulk-deploy.md`](adr-118-polyris-deploy-all-only-bulk-deploy.md) |
| 119 | Unified DAG visualization node/edge builder (`generate_dag_json` + `_build_pipeline_metadata`) | [`adr-119-unified-dag-visualization-node-edge-builder.md`](adr-119-unified-dag-visualization-node-edge-builder.md) |
| 120 | Manual task actions: recorded input, synthetic output markers, asset events on Mark Successful, and safe direct Restart from `waiting_decision` | [`adr-120-manual-task-actions-recorded-input-synthetic.md`](adr-120-manual-task-actions-recorded-input-synthetic.md) |
| 121 | Restart correctness: stop both wrapper levels in the right order, reconstruct `task_config`/`outlets` from the registry, `restart-` name prefix | [`adr-121-restart-correctness-two-level-wrapper-stop.md`](adr-121-restart-correctness-two-level-wrapper-stop.md) |
| 122 | Registration fast-path resolves `deps_skip`/`deps_blocked`; `evaluate_deps` gates on `assets_ready` for wait_for coordination | [`adr-122-registration-fast-path-and-asset-coordination.md`](adr-122-registration-fast-path-and-asset-coordination.md) |
| 123 | XCom reliable data passing — separate input record + `xcom.push()` for service tasks | [`adr-123-xcom-reliable-data-passing.md`](adr-123-xcom-reliable-data-passing.md) |
| 124 | `polyris-build-lambda`: stamp XCom helpers into Lambda handler directories | [`adr-124-polyris-build-lambda-stamp-xcom-helpers.md`](adr-124-polyris-build-lambda-stamp-xcom-helpers.md) |
| 125 | In-SFN retry visibility (`retry_attempt` field + `TASK_RETRY_STARTED` event) | [`adr-125-in-sfn-retry-visibility.md`](adr-125-in-sfn-retry-visibility.md) |
| 126 | Pre-deploy resource existence check in `polyris-deploy` | [`adr-126-pre-deploy-resource-existence-check-polyris.md`](adr-126-pre-deploy-resource-existence-check-polyris.md) |
| 127 | Pre-deploy resource override check in `polyris-deploy` | [`adr-127-pre-deploy-resource-override-check-polyris.md`](adr-127-pre-deploy-resource-override-check-polyris.md) |
| 128 | `mark_success` wakes `wait_for` subscribers via direct Lambda invoke | [`adr-128-mark-success-wakes-wait-for.md`](adr-128-mark-success-wakes-wait-for.md) |
| 130 | Coordination layer: one reconciler, facts as state, stream-driven hints | [`adr-130-coordination-reconciler.md`](adr-130-coordination-reconciler.md) |
| 131 | `polyris-up` / `polyris-down`: install the platform from prebuilt release assets | [`adr-131-polyris-up.md`](adr-131-polyris-up.md) |
| 132 | Billing tags: one `polyris:` set on every resource AWS can tag | [`adr-132-billing-tags-polyris-set-every-resource.md`](adr-132-billing-tags-polyris-set-every-resource.md) |
| 133 | A run has one date: the date it was started or scheduled for | [`adr-133-run-has-one-date.md`](adr-133-run-has-one-date.md) |
| 134 | A config the user names is used, or the command stops | [`adr-134-named-config-used-or-command-stops.md`](adr-134-named-config-used-or-command-stops.md) |
| 135 | A run is told how its task ended by whoever ends it | [`adr-135-whoever-ends-task-answers-run.md`](adr-135-whoever-ends-task-answers-run.md) |
| 136 | The pipelines list says what its statuses rest on, and groups only what a DAG names | [`adr-136-pipelines-list-statuses-and-groups.md`](adr-136-pipelines-list-statuses-and-groups.md) |
| 137 | Tasks as rows: one dispatcher starts the tasks, one token per run | [`adr-137-tasks-as-rows.md`](adr-137-tasks-as-rows.md) |
| 138 | Trigger rules after the run-end change: nine rules over final outcomes | [`adr-138-trigger-rules-after-run-end-change.md`](adr-138-trigger-rules-after-run-end-change.md) |
| 139 | One loader runs a pipeline file for every command | [`adr-139-one-loader-for-pipeline-files.md`](adr-139-one-loader-for-pipeline-files.md) |
| 140 | The orchestration role makes the direct steps' calls | [`adr-140-orchestration-role-makes-direct-calls.md`](adr-140-orchestration-role-makes-direct-calls.md) |
