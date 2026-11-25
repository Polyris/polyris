# Design decisions

An ADR (architecture decision record) records one decision that is hard to reverse and states it as the rule in force today. These records are for contributors: they explain why the code is the way it is.

## How to find one

- `ADR #N` is the file `adr-N-<slug>.md` in this folder, and `§k` is its heading `k.`. Find the heading with `grep -n '^#\+ k\.' adr-N-*.md` and read that section.
- Five numbers name two decisions each: 65, 94, 106, 107 and 108. A citation of one names its slug word, for example "ADR #65, view sync".
- A number with no file here is retired: its row under *Retired* names what replaced it and the command that prints its old text.
- Numbers are never reused or changed. A new ADR takes the highest number + 1. Number 129 was never used.
- *Accepted* is the date of the decision; `by` a date means its first record, when the decision itself is not dated.

<!-- BEGIN GENERATED: python scripts/adr_index.py -->

## Engine

| ADR | Decision | Accepted |
|---|---|---|
| [#4](adr-4-asset-based-orchestration.md) | Asset-Based Orchestration (not ExternalTaskSensor) | by 2026-02-24 |
| [#5](adr-5-dynamodb-for-state.md) | DynamoDB for State (not Step Functions native, not CloudWatch metrics) | by 2026-02-24 |
| [#8](adr-8-jsonata-for-step-functions.md) | JSONata for Step Functions (not JSONPath) | by 2026-02-24 |
| [#10](adr-10-single-execution-id.md) | Single Execution ID Pattern + `pipeline_execution_short` | by 2026-02-24 |
| [#11](adr-11-backfill-failure-wait-decision-auto-fail.md) | Backfill Failure: Wait for Decision (not auto-fail) | by 2026-02-24 |
| [#14](adr-14-canonical-output-key-run-specific.md) | Canonical Output Key (not run-specific) | by 2026-02-24 |
| [#15](adr-15-upstream-read-time-truncation-25kb-per.md) | Upstream Read-Time Truncation (25KB per dependency) | by 2026-02-24 |
| [#17](adr-17-task-output-truncate-vs-s3-claim.md) | Task Output: Truncate vs S3 Claim Check | by 2026-02-24 |
| [#18](adr-18-standard-vs-express-step-functions.md) | Standard vs Express Step Functions | by 2026-02-24 |
| [#19](adr-19-zero-cost-waiting-waitfortasktoken-sync.md) | Zero-Cost Waiting (waitForTaskToken + .sync) | by 2026-02-24 |
| [#20](adr-20-transparency-step-functions-over-eventbridge-orchestration.md) | Transparency: Step Functions over EventBridge for Orchestration | by 2026-02-24 |
| [#21](adr-21-single-source-truth-date-variables.md) | Single Source of Truth for Date Variables | by 2026-02-24 |
| [#22](adr-22-data-flow-sfn-executes-dynamodb-stores.md) | Data Flow: SFN Executes → DynamoDB Stores → UI Reads | by 2026-02-24 |
| [#38](adr-38-error-visibility-product-requirement.md) | Error Visibility — Product Requirement | by 2026-05-02 |
| [#51](adr-51-backfill-unification-concept-endpoint-bulk-run.md) | Backfill Unification — one concept, one endpoint, one bulk-run SFN | 2026-05-20 |
| [#54](adr-54-bulk-backfill-sfn-standard-map-based.md) | Bulk-Backfill SFN — Standard, Map-based, fire-and-wait architecture | 2026-05-20 |
| [#55](adr-55-scheduled-pipeline-runs-direct-eventbridge-sfn.md) | Scheduled Pipeline Runs — direct EventBridge → SFN, not routed through bulk-backfill | 2026-05-20 |
| [#56](adr-56-backfill-status-model-six-states-aggregation.md) | Backfill Status Model — six states, aggregation rules, mapping to execution statuses | 2026-05-20 |
| [#57](adr-57-cascade-semantics-auto-all-none-non.md) | Cascade Semantics — auto / all / none with non-transitive propagation | 2026-05-20 |
| [#58](adr-58-partition-keys-granularity-aware-range-expansion.md) | Partition Keys & Granularity-Aware Range Expansion | 2026-05-20 |
| [#59](adr-59-backfill-options-propagated-child-sfn-skip.md) | Backfill options propagated to child SFN — skip_tasks, force, cascade_all, suppress_asset_event | 2026-05-20 |
| [#60](adr-60-runtime-enforcement-skip-backfill-task-flag.md) | Runtime enforcement of `skip_on_backfill` task flag | 2026-05-22 |
| [#81](adr-81-zombie-backfill-reconciliation-unblocks-concurrency-guard.md) | Zombie-backfill reconciliation unblocks the concurrency guard | 2026-05-28 |
| [#82](adr-82-derived-status-premature-terminal-fix-backfill.md) | Derived-status premature-terminal fix + backfill audit | 2026-05-28 |
| [#83](adr-83-backfill-status-consolidation-correctness-hardening.md) | Backfill status consolidation + correctness hardening | 2026-05-28 |
| [#87](adr-87-partition-mapping-upstream-smart-fill-adopt.md) | Partition mapping + upstream smart-fill — adopt Dagster's model as prior art | by 2026-05-29 |
| [#88](adr-88-upstream-smart-fill-implementation-architecture-python.md) | Upstream smart-fill — implementation architecture (Python-planned, SFN-trusted) | by 2026-05-29 |
| [#89](adr-89-upstream-smart-fill-deferred-risk-register.md) | Upstream smart-fill — deferred-risk register (no-surprises contract) | by 2026-05-29 |
| [#90](adr-90-upstream-smart-fill-phase-3-tiered.md) | Upstream smart-fill Phase 3 — tiered SFN execution (nested Map + tier gate) | by 2026-05-29 |
| [#92](adr-92-asset-backfill-lineage-awareness-same-pipeline.md) | Asset backfill lineage-awareness (same-pipeline frontier) + correction of ADR #88 | by 2026-05-29 |
| [#107](adr-107-wrapper-retry-loop.md) | In-place retry loop in the `run_task` wrapper (per-task `retries` / `retry_delay`) | by 2026-08-17 |
| [#112](adr-112-canonical-execution-status.md) | Canonical execution-status derivation and reconciliation | by 2026-08-17 |
| [#114](adr-114-intervention-first-failure-model.md) | Intervention-first failure model | by 2026-08-17 |
| [#115](adr-115-canonical-trigger-rules-and-skip-semantics.md) | Canonical trigger rules and skip semantics | by 2026-08-17 |
| [#120](adr-120-manual-task-actions-recorded-input-synthetic.md) | Manual task actions: recorded input, output markers, asset events on Mark Successful, direct Restart | by 2026-08-17 |
| [#121](adr-121-restart-correctness-two-level-wrapper-stop.md) | Restart correctness: two-level wrapper stop, `task_config`/`outlets` reconstruction, `restart-` name prefix | by 2026-08-17 |
| [#123](adr-123-xcom-reliable-data-passing.md) | XCom reliable data passing — separate input record, `xcom.push()` for service tasks | 2026-09-11 |
| [#125](adr-125-in-sfn-retry-visibility.md) | In-SFN retry visibility (`retry_attempt` field + `TASK_RETRY_STARTED` event) | by 2026-11-12 |
| [#130](adr-130-coordination-reconciler.md) | Coordination layer: one reconciler, facts as state, stream-driven hints | 2026-11-12 |
| [#133](adr-133-run-has-one-date.md) | A run has one date: the date it was started or scheduled for | by 2026-11-14 |
| [#135](adr-135-whoever-ends-task-answers-run.md) | A run is told how its task ended by whoever ends it | by 2026-11-14 |
| [#137](adr-137-tasks-as-rows.md) | Tasks as rows: one dispatcher starts the tasks, one token per run | 2026-11-17 |
| [#138](adr-138-trigger-rules-after-run-end-change.md) | Trigger rules after the run-end change: nine rules over final outcomes | 2026-11-17 |

## SDK

| ADR | Decision | Accepted |
|---|---|---|
| [#12](adr-12-explicit-skip-backfill-parameter-heuristic.md) | Explicit `skip_on_backfill` Parameter (not heuristic) | by 2026-02-24 |
| [#13](adr-13-decorator-parameter-duplication-ide-autocomplete.md) | Decorator Parameter Duplication (for IDE autocomplete) | by 2026-02-24 |
| [#16](adr-16-inlets-outlets-lineage-only-orchestration.md) | Inlets/Outlets for Lineage Only (not orchestration) | by 2026-02-24 |
| [#29](adr-29-generators-dry-dispatch-dict-shared-builders.md) | Generators DRY: Dispatch Dict, Shared Builders, Centralized Constants | by 2026-02-24 |
| [#31](adr-31-removal-config-arn-tool-polyris-accounts.md) | Removal of `config.arn()` and `[tool.polyris.accounts]` | by 2026-05-02 |
| [#39](adr-39-asset-enrichment-schema-declaration-asset-registry.md) | Asset Enrichment: Schema Declaration + asset_registry Removal | by 2026-05-02 |
| [#42](adr-42-asset-schema-2-0-typed-column.md) | Asset Schema 2.0: Typed `Column` Class with Platform-Agnostic Type System | by 2026-05-03 |
| [#43](adr-43-glue-schema-sync-demand-scheduled.md) | Glue Schema Sync: On-Demand, Not Scheduled | by 2026-05-03 |
| [#44](adr-44-schema-adapters-pyarrow-pydantic-glue-as.md) | Schema Adapters: pyarrow / pydantic / Glue as `Asset.from_*` Constructors | by 2026-05-03 |
| [#45](adr-45-cross-account-cross-region-glue-catalog.md) | Cross-Account / Cross-Region Glue Catalog Support | by 2026-05-04 |
| [#46](adr-46-ddl-schema-export-phased-architecture.md) | DDL/Schema Export — Phased Architecture | by 2026-05-08 |
| [#50](adr-50-asset-granularity-declarative-dsl-advisory-glue.md) | Asset Granularity — declarative DSL + advisory Glue auto-detect + drift detection | 2026-05-18 |
| [#52](adr-52-pipeline-granularity-runtime-cron-cadence-inference.md) | Pipeline Granularity — runtime cron cadence inference, no DSL change | 2026-05-20 |
| [#72](adr-72-ssot-enums-codegen-single-canonical-source.md) | SSoT enums codegen — single canonical source for status/rule enums | 2026-05-27 |
| [#91](adr-91-rename-backfill-cascade-downstream-deprecated-alias.md) | Rename backfill `cascade` → `downstream` (with deprecated alias) | by 2026-05-29 |
| [#94](adr-94-backfill-error-codes.md) | BackfillUpstream + backfill error-code registry onto the generated SSoT | by 2026-05-31 |
| [#106](adr-106-task-param-parity.md) | Parameter parity for `@task` decorators (the `task_config` ↔ `Run_Task_<X>` contract) | by 2026-08-17 |
| [#108](adr-108-task-config-key-constants.md) | `TaskConfigKey`: shared constants for the SDK↔wrapper `task_config` contract | by 2026-08-17 |
| [#109](adr-109-unified-task-decorator-kwargs.md) | Unified common parameters for `@task.<type>` decorators (`CommonTaskKwargs`) | by 2026-08-17 |
| [#124](adr-124-polyris-build-lambda-stamp-xcom-helpers.md) | `polyris-build-lambda`: stamp XCom helpers into Lambda handler directories | by 2026-11-12 |
| [#139](adr-139-one-loader-for-pipeline-files.md) | One loader runs a pipeline file for every command | by 2026-11-18 |

## Deploy

| ADR | Decision | Accepted |
|---|---|---|
| [#27](adr-27-unified-version-string-across-all-packages.md) | Unified Version String Across All Packages | by 2026-02-24 |
| [#30](adr-30-ssm-parameter-store-instead-terraform-remote.md) | SSM Parameter Store instead of Terraform Remote State for Pipeline Configuration | by 2026-05-02 |
| [#34](adr-34-aws-sam-instead-opentofu-shared-infrastructure.md) | AWS SAM instead of OpenTofu for Shared Infrastructure | by 2026-05-02 |
| [#36](adr-36-ssm-as-cross-stack-bridge-instead.md) | SSM as a Cross-Stack Bridge (instead of `Fn::ImportValue`) | by 2026-05-02 |
| [#37](adr-37-definitionuri-aws-serverless-statemachine-sfn-definitions.md) | `DefinitionUri` + `AWS::Serverless::StateMachine` for SFN Definitions | by 2026-05-02 |
| [#96](adr-96-cloudfront-security-response-headers-static-export.md) | CloudFront security response headers (static export) | by 2026-06-15 |
| [#118](adr-118-polyris-deploy-all-only-bulk-deploy.md) | `polyris-deploy --all` / `--only` — bulk deploy and destroy across directories | by 2026-08-17 |
| [#126](adr-126-pre-deploy-resource-existence-check-polyris.md) | Pre-deploy resource existence check in `polyris-deploy` | by 2026-11-12 |
| [#127](adr-127-pre-deploy-resource-override-check-polyris.md) | Pre-deploy resource override check in `polyris-deploy` | by 2026-11-12 |
| [#131](adr-131-polyris-up.md) | `polyris-up` / `polyris-down`: install the platform from prebuilt release assets | 2026-11-12 |
| [#132](adr-132-billing-tags-polyris-set-every-resource.md) | Billing tags: one `polyris:` set on every resource AWS can tag | by 2026-11-14 |
| [#134](adr-134-named-config-used-or-command-stops.md) | A config the user names is used, or the command stops | by 2026-11-14 |
| [#140](adr-140-orchestration-role-makes-direct-calls.md) | The orchestration role makes the direct steps' calls | by 2026-11-25 |

## Console API

| ADR | Decision | Accepted |
|---|---|---|
| [#1](adr-1-alerts-configured-in-ui.md) | Alerts Configured in the UI, Not the DSL | by 2026-02-24 |
| [#25](adr-25-complete-dal-migration-zero-direct-table.md) | Complete DAL Migration — Zero Direct Table Access in Routes | by 2026-02-24 |
| [#28](adr-28-specific-exception-types-route-handlers.md) | Specific Exception Types in Route Handlers | by 2026-02-24 |
| [#47](adr-47-asset-lineage-last-updated-enrichment-two.md) | Asset Lineage `last_updated` Enrichment — Two Endpoints, Two Scopes | by 2026-05-08 |
| [#53](adr-53-cost-preview-methodology-formula-sources-accuracy.md) | Cost Preview Methodology — formula, sources, accuracy disclaimer | 2026-05-20 |
| [#65](adr-65-api-tokens-and-auth-enforcement.md) | API Tokens (PAT) & Auth Enforcement | by 2026-05-27 |
| [#66](adr-66-per-token-scopes.md) | Per-Token Scopes (Granular Authorization) | by 2026-05-27 |
| [#73](adr-73-computation-moved-to-backend.md) | Computation moved to backend — derived status + per-partition aggregation | 2026-05-27 |
| [#74](adr-74-per-partition-retry-failed.md) | Per-partition retry-failed | 2026-05-27 |
| [#95](adr-95-unified-run-feed.md) | Unified Run/Activity feed — Backfills as first-class rows in `/api/runs` | by 2026-06-01 |
| [#103](adr-103-alert-config-and-notify-lambda.md) | Alert delivery is runtime config in the registry, read by one notify Lambda | 2026-08-17 |
| [#107](adr-107-notifications-one-per-run.md) | Notifications: one per run, two attention states (failure vs decision-required) | by 2026-08-17 |
| [#108](adr-108-runs-feed-inverted-gsi.md) | A pipeline's runs are read through an inverted GSI; the cross-pipeline feed keeps its per-date fan-out | by 2026-08-17 |
| [#113](adr-113-history-feed-started-at-cursor.md) | The History feed pages on a `started_at` cursor; `next` is the only end-of-feed signal | by 2026-08-17 |
| [#136](adr-136-pipelines-list-statuses-and-groups.md) | The pipelines list says what its statuses rest on, and groups only what a DAG names | by 2026-11-17 |

## UI

| ADR | Decision | Accepted |
|---|---|---|
| [#6](adr-6-polling-for-ui-updates.md) | Polling for UI Updates (simple and reliable) | by 2026-02-24 |
| [#7](adr-7-code-splitting-ui-bundle.md) | Code Splitting for UI Bundle | by 2026-02-24 |
| [#40](adr-40-responsive-layout-flex-chains-no-viewport.md) | Responsive Layout: Flex Chains, No Viewport Magic Numbers | by 2026-05-02 |
| [#41](adr-41-url-routing-cloudfront-function-window-history.md) | URL Routing: CloudFront Function + window.history for Deep State | by 2026-05-03 |
| [#48](adr-48-assetdetailpage-composition-tabs-as-independent-sub.md) | AssetDetailPage Composition — Tabs as Independent Sub-Components | by 2026-05-08 |
| [#49](adr-49-asset-matrix-view-cross-asset-temporal.md) | Asset Matrix View — Cross-asset temporal grid | 2026-05-12 |
| [#61](adr-61-task-subset-selection-backfill-ui.md) | Task-subset selection in backfill UI | 2026-05-22 |
| [#63](adr-63-backfill-detail-pipeline-dag-navigation.md) | Backfill detail → pipeline DAG navigation | 2026-05-27 |
| [#64](adr-64-standard-keyboard-shortcut-convention.md) | Standard keyboard shortcut convention | 2026-05-27 |
| [#65](adr-65-view-sync.md) | Top-level view registration — three-place sync | 2026-05-27 |
| [#66](adr-66-backfill-csv-export-removed.md) | Export CSV removed from BackfillDetailPage | 2026-05-27 |
| [#67](adr-67-client-side-derived-status-backfills.md) | Client-side derived status for backfills | 2026-05-27 |
| [#68](adr-68-backfill-ux-dx-bundle-partial-ratio.md) | Backfill UX/DX bundle — partial ratio, retry chain, notification source | 2026-05-27 |
| [#69](adr-69-derived-status-shared-util.md) | derivedStatus lifted to shared util + inline Cancel on list | 2026-05-27 |
| [#70](adr-70-active-filter-renamed-running.md) | "Active" filter renamed to "Running"; narrowed to running-only | 2026-05-27 |
| [#79](adr-79-backfill-icon-single-source-truth.md) | Backfill icon single source of truth | 2026-05-27 |
| [#80](adr-80-backfill-modal-viewport-overflow-dead-css.md) | Backfill modal viewport overflow + dead CSS cleanup | 2026-05-28 |
| [#86](adr-86-asset-centric-ux-over-task-dsl.md) | Product concept: asset-centric UX over an unchanged task-based DSL | by 2026-05-29 |
| [#94](adr-94-runtime-config-precedence.md) | Runtime config precedence (window.CONFIG over baked NEXT_PUBLIC_*) | by 2026-05-31 |
| [#106](adr-106-assets-current-state-date-scope.md) | Assets are a current-state surface; per-date history lives only on the matrix | by 2026-08-17 |
| [#111](adr-111-client-side-view-routing.md) | Client-side view routing via the History API | by 2026-08-17 |
| [#119](adr-119-unified-dag-visualization-node-edge-builder.md) | Unified DAG visualization node/edge builder | by 2026-08-17 |

## Open core

| ADR | Decision | Accepted |
|---|---|---|
| [#97](adr-97-plugin-route-registration.md) | Explicit plugin route registration (open-core seam) | by 2026-08-17 |
| [#98](adr-98-open-core-split-structure.md) | Open-core split structure (proprietary roots, no symlink) | by 2026-08-17 |
| [#99](adr-99-ui-open-core-exclusion.md) | UI open-core exclusion (generated active module) | by 2026-08-17 |
| [#100](adr-100-tier-entitlement.md) | Tier entitlement (team↔enterprise gated at runtime, not by strip) | by 2026-08-17 |
| [#101](adr-101-multi-tenant-readiness.md) | Multi-tenant readiness (entitlement is tenant-source-agnostic; tenancy deferred) | by 2026-08-17 |
| [#102](adr-102-repo-split-dagster-model.md) | Repo split (Dagster model): public core repo + private paid repo | by 2026-08-17 |
| [#104](adr-104-open-core-cli-split-backfill-nav.md) | Open-core CLI split, backfill nav-tab slot, and contract-drift cleanup | by 2026-08-17 |
| [#105](adr-105-asset-console-gated-team-open-core.md) | Asset console gated to Team with an open-core "coming soon" page; engine stays free | by 2026-08-17 |
| [#110](adr-110-intervention-tier-flip.md) | Task & execution intervention moved from Team to free; config mutation stays paid | by 2026-08-17 |

## Process

| ADR | Decision | Accepted |
|---|---|---|
| [#26](adr-26-pytest-mock-as-standard-test-mocking.md) | pytest-mock as Standard Test Mocking Library | by 2026-02-24 |

<!-- END GENERATED -->

## Retired

A retired ADR has no file. Its row says what replaced it, and the command prints its text as of the last release that had it.

| ADR | Decision | Why retired | Text |
|---|---|---|---|
| 2 | SFN-Based Dependency Notification (not polling, not EventBridge) | Superseded by ADR #137 (§1, §7): a task is a row the dispatcher decides; no task token waits for a dependency. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '26,41p'` |
| 3 | Separate Helpers (not monolithic wrapper) | Superseded by ADR #137 §1: the wrapper and the helpers it was split into are gone; `run_task` runs one attempt. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '45,67p'` |
| 9 | Pulumi for Pipelines, Terraform for Shared | Superseded by ADR #34 and ADR #131: the platform is a SAM stack that `polyris-up` installs; Pulumi and Terraform are gone. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '168,184p'` |
| 23 | DAG Snapshot Per Execution (not per deploy) | Superseded by ADR #137 §8: a run's graph is the spec version its run row names. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '475,492p'` |
| 24 | Pulumi Dynamic Provider for Pipeline Lifecycle (not EventBridge) | Superseded: Pulumi is gone; `polyris-deploy` registers a pipeline with boto3 calls (ADR #137 §8). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '494,514p'` |
| 32 | OpenTofu instead of Terraform + native S3 locking | Superseded by ADR #34: SAM replaced OpenTofu. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '670,683p'` |
| 33 | Terraform as a Public Module | Superseded by ADR #34: SAM replaced the Terraform module. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '687,698p'` |
| 35 | `polyris-deploy` as a Parallel Alternative to Pulumi | Superseded: Pulumi is gone, and `polyris-deploy` deploys through boto3 change sets (`polyris/cfn_changeset.py`, ADR #131). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '722,740p'` |
| 62 | Backfill cost estimate removed pending Pro tier | Its subject is gone: the backfill cost estimate it removed has no code left. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '4800,4896p'` |
| 71 | ExecutionStatus case normalization | Change record. The rule it set, one canonical `ExecutionStatus` vocabulary, is ADR #112's. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '5628,5733p'` |
| 75 | DAL repository pattern for all 4 helper Lambdas | Change record. The helper Lambdas it moved onto DAL repositories are gone (ADR #137). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '6114,6254p'` |
| 76 | print() → structured logger in all helper Lambdas | Change record. Lambdas log through `polyris/logger.py` (root `CLAUDE.md`, *Lambda Functions*). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '6256,6352p'` |
| 77 | TaskStatus class-level sets → generated module-level | Change record. Status sets are generated by `sync_enums` (ADR #72, Principle #27). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '6354,6473p'` |
| 78 | SFN template drift checker | Change record. The drift check is `polyris/codegen/check_sfn_templates.py` (`make check-sfn-templates`). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '6475,6603p'` |
| 84 | SDK / shared-constants delivery to Lambdas — guarded copy now, PyPI later | Change record, superseded by ADR #102: the Lambdas install the SDK from `requirements.txt`. | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '7316,7410p'` |
| 85 | Fix skip-task JSONata (`$contains` on array) + evaluate template JSONata in CI | Change record. Template JSONata is evaluated in CI, and `scripts/audit_jsonata.py` is the audit (`make audit-jsonata`). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '7412,7511p'` |
| 93 | UI status vocabulary consolidated onto generated enums (single source) | Change record. The UI's status vocabulary is generated into `ui/src/generated/enums.ts` (ADR #72, Principle #27). | `git show v1.3.2:docs/reference/DESIGN_DECISIONS.md \| sed -n '8274,8321p'` |
| 116 | Cleanup via scoped de-abort | Superseded by ADR #138: no de-abort is needed, since a final failure no longer aborts the run. | `git show v1.3.2:docs/reference/adr-116-cleanup-scoped-deabort.md` |
| 117 | Trigger rules trimmed from 11 to 5 | Superseded by ADR #138: nine trigger rules over final outcomes. | `git show v1.3.2:docs/reference/adr-117-trigger-rule-trim-11-to-5.md` |
| 122 | Registration fast-path resolves `deps_skip`/`deps_blocked`; `evaluate_deps` gates on `assets_ready` for wait_for coordination | Superseded by ADR #130 and ADR #137: readiness is decided from facts by the dispatcher. | `git show v1.3.2:docs/reference/adr-122-registration-fast-path-and-asset-coordination.md` |
| 128 | `mark_success` wakes `wait_for` subscribers via direct Lambda invoke | Superseded by ADR #130 and ADR #137: an asset fact wakes its waiters through the stream and the sweeper. | `git show v1.3.2:docs/reference/adr-128-mark-success-wakes-wait-for.md` |
