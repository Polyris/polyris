# Design Decisions

## Core Decisions

### 1. Alerts Configured in the UI, Not the DSL

**Decision:** Failure-alert delivery (Slack, PagerDuty) is configured per pipeline
in the Settings → Alerts UI and stored in the pipeline registry (`alert_config`),
not declared in the DAG. The old `alerts=` DAG parameter has been **removed**.

**Rationale:**
- Secrets (Slack webhook, PagerDuty routing key) must not live in pipeline source —
  they belong in SSM, written via the UI, never committed
- Alerting is an operational concern that changes without a code redeploy
- Browser notifications are free and automatic; external channels are Team-tier
- A single source of truth (the registry) that the notify Lambda reads at 3am,
  with no UI involvement at failure time

**Implementation:**
The DSL no longer defines an `alerts=` parameter; passing it raises a `TypeError`.
Non-secret config (channel, mentions, severity,
enabled channels) lives in `alert_config`; secrets go to SSM with only the
parameter name kept in the config. See ADR #103 and `docs/features/alerts.md`.

---

### 2. SFN-Based Dependency Notification (not polling, not EventBridge)

**Decision:** Use Step Function helper (`notify_dependents`) + `waitForTaskToken` for dependency resolution.

**Rationale:**
- No wasted compute (polling)
- Instant notifications via SFN callback tokens
- Full visibility in Step Functions execution history (vs EventBridge "black box")
- Scales automatically

**History:** v1-v50 used EventBridge events for notifications. Replaced with SFN-based approach in v51 for better observability and debugging. Converted to Express workflow in v55.2 for cost savings (~10x cheaper, no .sync or waitForTaskToken needed). Execution history preserved via CloudWatch Logs.

**Alternative considered:** DynamoDB polling every N seconds
**Why rejected:** Expensive, slow, doesn't scale

---

### 3. Separate Helpers (not monolithic wrapper)

**Decision:** Split wrapper into multiple small Step Functions.

**Rationale:**
- Easier testing
- Better error isolation
- Reusable components
- Cleaner logs

**Helpers (12):**
- registration — register task + check deps
- run_task — execute actual task (7 types)
- failure_handler — error handling + alerting
- pause_waiter — save pause token for callback
- notify_dependents — wake up waiting tasks **(EXPRESS)**
- notify_asset_consumers — cross-pipeline asset triggers **(EXPRESS)**
- restart_task — restart failed task **(EXPRESS)**
- restart_wrapper — restart wrapper execution **(EXPRESS)**
- ~~register_on_create~~ — removed, replaced by Pulumi Dynamic Provider (ADR #24)
- ~~interactive_choice_slack / pagerduty_alerter / pagerduty_resolver~~ — removed
  in ADR #103; interactive Slack and PagerDuty alerts/resolves are now
  `lambda:invoke` calls to the notify Lambda, not separate state machines

---

### 4. Asset-Based Orchestration (not ExternalTaskSensor)

**Decision:** Implement asset triggers.

**Rationale:**
- Decoupled pipelines
- No hardcoded DAG references
- Natural data lineage
- Better than sensors (no polling)

---

### 5. DynamoDB for State (not Step Functions native, not CloudWatch metrics)

**Decision:** Track task state in DynamoDB, not rely on SFN execution state or CloudWatch metrics.

**Rationale:**
- Query across executions (SFN state is per-execution, no cross-query)
- UI can show history and filter by date/pipeline/status
- Cross-pipeline visibility in one table
- SFN state is ephemeral — execution history expires after 90 days
- CloudWatch metrics are aggregated — can't query individual task status
- DynamoDB gives single-digit millisecond reads for UI responsiveness

**Why not SFN DescribeExecution:**
- One API call per task — at 200 tasks, that's 200 API calls per page load
- Rate limited (25 TPS default)
- No filtering by date, pipeline, or status
- No cross-pipeline aggregation

**Why not CloudWatch metrics:**
- Aggregated counters (succeeded: 5, failed: 2) — no per-task detail
- No task names, no timestamps, no error messages
- 1-minute granularity minimum — not real-time
- Can't power a dashboard that shows individual task status

**Tables:** 8 tables for different concerns
- pipeline_tokens (task state + canonical output)
- dependency_subscriptions (dependency tracking)
- task_events (timeline history)
- pipeline_registry (DAG metadata)
- asset_events, asset_subscriptions (asset orchestration)
- queued_asset_events (AND logic for multi-asset triggers)
- api_tokens (PATs — ADR #65)

---

### 6. Polling for UI Updates (simple and reliable)

**Decision:** Use polling (3s active / 30s idle) for UI updates.

**Rationale:**
- Simple implementation
- No WebSocket infrastructure needed
- Works through all proxies/firewalls
- Sufficient for dashboard use case

**Implementation:**
- Active polling (3s) when tasks are running
- Idle polling (30s) when no active tasks
- Auto-detect based on task statuses

---

### 7. Code Splitting for UI Bundle

**Decision:** Split vendor chunks (React, ReactFlow).

**Rationale:**
- Vendor chunks cached separately
- Faster subsequent loads
- Main bundle < 150KB

**Chunks:**
- index.js (~125KB) - app code
- vendor-react.js (~141KB) - React core
- vendor-flow.js (~238KB) - ReactFlow + dagre

---

### 8. JSONata for Step Functions (not JSONPath)

**Decision:** Use JSONata (QueryLanguage: "JSONata") throughout.

**Rationale:**
- More powerful expressions
- Cleaner syntax
- Better error handling
- Native date/time functions

**Example:**
```json
"Output": "{% $substringBefore($now(), 'T') %}"
```

---

### 9. Pulumi for Pipelines, Terraform for Shared

> **SUPERSEDED — Pulumi removed.** Pipelines now deploy via `polyris-deploy`
> (CloudFormation); shared infra via AWS SAM. There is no Pulumi path anymore
> (`polyris/pulumi.py` deleted, no `import pulumi` in the codebase). See
> CLAUDE.md rules #34–#35 and the "Pulumi removed" CHANGELOG entry. Kept below
> as a historical record only.

**Decision:** Use different IaC tools for different layers.

**Rationale:**
- Pulumi: Python-native, good for pipelines
- Terraform: Battle-tested, good for infra
- Clear separation of concerns

**Shared (Terraform):** DynamoDB, Lambdas, helpers, console
**Pipelines (Pulumi):** Individual Step Functions, schedules

---

### 10. Single Execution ID Pattern + `pipeline_execution_short`

**Decision:** Use deterministic execution names (task-date-short). Keep `pipeline_execution_short` as a core identifier.

**Rationale:**
- Prevents duplicate runs for same date
- Makes backfill idempotent
- Easy correlation in logs

**Format:** `{task}-{date}-{pipeline_short}`
Example: `scrape-2026-01-12-abc12345`

**`pipeline_execution_short` (keep decision):**
- 103 references across codebase — refactoring is 2-3 day effort
- Industry moving in same direction (short execution IDs)
- Used in: execution_name construction, DynamoDB keys, SFN input, UI display
- Collision probability at 20 chars: ~10^-19 at 1000 concurrent pipelines (safe)
- Revisit only if it causes a concrete bug

---

## Trade-offs Accepted

### Eventual Consistency

DynamoDB reads may be slightly stale (unless ConsistentRead). Accepted because:
- All critical writes use conditional expressions (no lost updates)
- evaluate_deps uses ConsistentRead for status checks
- Auto-registration via CloudTrail → EventBridge has ~1 second delay (acceptable)

### Data Passing via DynamoDB (not SFN native)

Task outputs are stored in DynamoDB under a **canonical key** (`output#pipeline#task#date`) —
stable, run-independent. Downstream reads this key via `Read_Upstream_Outputs` in run_task.

**Write:** Save_Success writes canonical output (up to 200KB per task, truncated automatically).
**Read:** Get_Dep_Output reads canonical key, truncates to 25KB per dependency (Step Functions 256KB payload limit).
**Skip:** Auto_Skip never touches canonical — previous output preserved for incremental backfill.

For larger data, tasks should write to S3 and pass the path as output.

### No Dynamic Tasks

Can't generate tasks at runtime. Accepted because:
- Step Functions Map state covers most cases
- Simpler to understand
- Matches established orchestrator model

---

### 11. Backfill Failure: Wait for Decision (not auto-fail)

**Decision:** When a task fails during backfill, suppress Slack/PagerDuty but still wait for user decision via UI.

**Rationale:**
- Auto-fail gave no way to restart/skip from UI
- Slack/PD noise during backfill is unwanted (dozens of dates)
- User still needs control over failed tasks
- 5h timeout same as normal runs — consistent behavior

**Implementation:** `Check_Is_Backfill` → `Wait_For_Decision` (skipping Interactive_Slack and Send_PagerDuty_Alert). Task shows `waiting_decision` in UI with skip/fail/restart buttons.

---

### 12. Explicit `skip_on_backfill` Parameter (not heuristic)

**Decision:** Tasks declare `skip_on_backfill=True` explicitly instead of UI guessing from inlets/outlets.

**Rationale:**
- Old heuristic: `isSource = outlets > 0 && inlets === 0` — broke when scrapers had inlets for lineage
- Explicit is better than implicit
- Developer controls behavior, not UI assumptions

**Implementation:**
```python
@task.sfn(arn=ARNS["scraper"], outlets=[raw_data], skip_on_backfill=True)
def scrape_pdp():
    pass
```

---

### 13. Decorator Parameter Duplication (for IDE autocomplete)

**Decision:** Duplicate common parameters (`retries`, `retry_delay`, `trigger_rule`, `wait_before`, `skip_on_backfill`) across all 7 task decorator methods.

**Rationale:**
- Python lacks good mechanism for "signature inheritance" between methods
- Alternatives (TypedDict, `**kwargs`, ParamSpec) all break IDE hints
- Established orchestrators use same pattern
- ~10 params × 7 decorators = acceptable duplication for perfect IDE experience

**Alternative considered:** Base class with shared params
**Why rejected:** No IDE autocomplete for inherited method signatures

---

### 14. Canonical Output Key (not run-specific)

**Decision:** Store task output under stable key `output#pipeline#task#date` instead of run-specific `task-date-executionShort`.

**Rationale:**
- Run-specific key breaks incremental backfill (skip writes empty `{}`, overwrites real output)
- Canonical key is run-independent — latest successful output always available
- Read_Upstream doesn't need to know execution details
- Auto_Skip never touches canonical — previous output preserved

**Cost:** One extra DynamoDB putItem per successful task in same transaction. ~$0.22/month at 60K tasks.

---

### 15. Upstream Read-Time Truncation (25KB per dependency)

**Decision:** Truncate each dependency output to 25KB when reading for downstream, not when writing.

**Rationale:**
- Step Functions has 256KB payload limit between states
- 10 deps × 25KB = 250KB = safe under limit
- Full output (200KB) preserved in DynamoDB — only in-flight payload trimmed
- Truncated output includes `_warning` field explaining what happened and how to fix
- Write-time truncation would lose data permanently; read-time preserves it

---

### 16. Inlets/Outlets for Lineage Only (not orchestration)

**Decision:** `inlets` and `outlets` are metadata for lineage tracking. They do not affect task execution, skipping, or scheduling.

**Rationale:**
- Old behavior: backfill used `skip_source_tasks` heuristic — tasks with no inlets were considered "source tasks" and auto-skipped
- Problem: scrapers that read from Athena views had inlets for lineage → no longer skipped during backfill
- Mixing lineage metadata with orchestration logic creates hidden coupling

**Resolution:** Removed `skip_source_tasks` heuristic. Replaced with explicit `skip_on_backfill=True` parameter (see #12). Clear separation of concerns:

| Parameter | Purpose | Affects execution? |
|-----------|---------|-------------------|
| `outlets` | Declares what task produces (asset events) | Yes (emits asset event) |
| `inlets` | Declares what task reads (lineage display) | No |
| `dependencies` | Task waits for these tasks (same pipeline) | Yes (blocks) |
| `skip_on_backfill` | Skip this task during backfill | Yes (skips) |

---

### 17. Task Output: Truncate vs S3 Claim Check

**Decision:** Truncate large outputs (200KB limit per task) instead of implementing S3 claim check pattern.

**Rationale:**
- Large outputs between tasks is an anti-pattern — data should flow through data layer (S3), not orchestration layer
- Task outputs should be metadata: paths, row counts, status flags
- S3 claim check adds complexity (Lambda between every task) and hides bad design

**If you hit the 200KB limit:**
1. Refactor your task to write data to S3 and return only the path
2. The truncation message `{"_truncated": true, "_size": 500000}` tells you the original size

**Example:**
```python
# ❌ Anti-pattern: passing data through orchestration
return {"rows": [{"id": 1, "data": "..."}, ...]}  # 500KB

# ✅ Correct: passing references
return {"output_path": "s3://bucket/output/2026-01-01/", "row_count": 50000}
```

---

### 18. Standard vs Express Step Functions

**Decision:** Use Standard for long-running/debuggable workflows, Express only for fire-and-forget helpers.

| Express (cheap, no history) | Standard (visible, debuggable) |
|----|---|
| notify_dependents | dependency_wrapper (hours, waitForTaskToken) |
| notify_asset_consumers | run_task (hours, .sync callbacks) |
| | registration (waitForTaskToken) |
| | failure_handler (invokes other SFNs) |

**Rationale:**
- Express: ~100x cheaper ($0.000001 vs $0.000025 per transition), but no execution history in console
- Standard: execution history visible in AWS console — critical for debugging stuck tasks
- notify_dependents is fire-and-forget (evaluate deps, send tokens) — Express OK
- run_task can run for hours (ECS/Glue jobs) — must be Standard
- Registration uses waitForTaskToken — Express doesn't support it

**Cost impact:** Express helpers save ~$5-10/month at 60K tasks. Small but adds up at scale.

---

### 19. Zero-Cost Waiting (waitForTaskToken + .sync)

**Decision:** Use Step Functions callback patterns for all waiting instead of polling.

**Rationale:**
- `waitForTaskToken`: wrapper waits for dependency resolution — $0 while waiting (no transitions)
- `.sync`: run_task waits for ECS/Glue/Lambda completion — $0 while waiting
- A task can wait for hours or days — cost is always 0 (Standard SFN charges per transition, not per second)

**Comparison at 2000 tasks/day, 1.5hr average:**
| Pattern | Monthly cost for waiting |
|---|---:|
| polyris (waitForTaskToken) | **$0** |
| Managed orchestrators (MWAA) | ~$4,950 |
| Prefect (polling agent) | ~$53 |
| Dagster (polling daemon) | ~$35 |

This is polyris's key architectural advantage — the longer tasks run, the more money is saved versus polling-based orchestrators.

---

### 20. Transparency: Step Functions over EventBridge for Orchestration

**Decision:** Migrate all orchestration logic from EventBridge+Lambda to Step Functions for visibility.

**History:**
- v1-v50: EventBridge events for dependency notifications + asset triggers
- v51: Replaced notify_dependents Lambda+EventBridge with SFN helper
- v55: Replaced asset_trigger Lambda+EventBridge with notify_asset_consumers SFN

**Rationale:**
- EventBridge is a "black box" — event goes in, something happens, hard to debug what/when/why
- Step Functions execution history shows every state, every input/output, every error
- When a task is stuck "waiting" — you can open SFN console and see exactly which dependency is pending
- When notification fails — you see which subscriber, which trigger_rule evaluation, which token

**Trade-off:** More state transitions = slightly higher cost (~$5/month). Worth it for debuggability in production.

---

### 21. Single Source of Truth for Date Variables

**Decision:** JSONata in `Prepare_Task_Input` is the single source for all date-derived variables. Python (backfill.py) only provides `current_date` and non-derivable extras.

**Variable ownership:**

| Source | Variables | Why |
|---|---|---|
| JSONata (always) | date_compact, date_slash, year, month, day, day_of_week, previous_date, next_date, minus_7/14/30_days, ALLOW_UNSUCCESSFUL_SPIDER_RUN | Derivable from current_date with simple arithmetic |
| Python (backfill only) | minus_1_month, minus_3_months, day_of_year, week_of_year, is_backfill, is_reprocess | Calendar math or special formats JSONata can't compute |

**Merge order:** `$merge([$dateVars, $vars])` — backfill/custom variables always override computed values.

**Rationale:** Before this, 15+ variables were duplicated between `backfill.py` (Python) and `Prepare_Task_Input` (JSONata). If logic changed in one place, the other would diverge silently. Now each variable is computed in exactly one place.

**Update:** the canonical source is now `polyris/variables.py` (name + JSONata expression); `Prepare_Task_Input`'s `$dateVars` block is **generated** from it by `polyris.codegen.sync_variables`, and `task_variables.py` re-exports it. The "one place" is now the registry, and drift is impossible (the template is generated, checked by `test_template_generated_from_registry`).

---

### 22. Data Flow: SFN Executes → DynamoDB Stores → UI Reads

**Decision:** All UI reads come from DynamoDB. Step Functions API is used only for control actions and reconciliation of running executions.

**Principle:** The execution engine writes, the database stores, the UI reads from the database.

**Step Functions (execution engine) — WRITE/CONTROL only:**
- Start execution, Stop/Abort execution
- Send task token (callbacks)
- Describe execution (reconcile running tasks only)

**DynamoDB (metadata database) — ALL reads:**
- Calendar, Sidebar stats, DAG view, All Tasks, All Runs
- Execution dropdown, Task Detail, Sparkline, SLA, Metrics

**Reconcile pattern:** Running executions may have stale status in DDB. UI reconciles by calling `sfn.describe_execution` for executions with `status=running`. This is the ONLY case where UI reads from SFN.

**Why not SFN API for reads:**
- `startDate` is physical (when AWS started execution), not logical (`current_date`)
- Calendar must show logical date — "data for Feb 13" not "button pressed Feb 19"
- SFN execution history expires after 90 days — old runs disappear
- `ListExecutions` has no date/status filtering — must fetch all then filter client-side
- Cross-pipeline queries require N API calls (one per pipeline)

**Cost:** DDB reads add ~$0.45/month for 5 users. SFN `ListExecutions` was free but returned wrong dates.

**Affected endpoints (migrated from SFN → DDB):**
- `get_pipeline_executions` (Calendar + Execution dropdown)
- `get_all_runs` (All Runs view)

### 23. DAG Snapshot Per Execution (not per deploy)

**Decision:** Each execution writes its own DAG snapshot to `tokens_table` with key `dag_snapshot::{execution_name}`. TTL: 120 days (AWS SFN history is 90 days + 30-day buffer).

**Problem:** After `polyris-deploy` with changed DAG, old executions in UI showed the *new* graph instead of the graph they actually ran with. Debugging "why did it fail" was impossible because the DAG structure was wrong.

**Alternatives considered:**
- Per-deploy snapshot (S3 or DDB) — doesn't tie to specific execution, overwrites on each deploy
- Git commit hash — requires git in deploy environment, no actual DAG data
- Hash-only indicator — shows "DAG changed" but not *how* it changed

**Why per-execution:** AWS Step Functions guarantees that each execution runs the ASL definition that was active at `StartExecution` time. The DAG metadata is hardcoded as a string literal in the ASL at `polyris-deploy` time. Each execution writes to a unique key (its own execution ARN) — no overwrites, no race conditions.

**API lookup priority:** snapshot → registry → inferred. Old executions (pre-feature) gracefully fall back to registry.

**Cost:** ~2-3 KB per execution × 60,000/month = ~2 GB/year = $0.50/month DDB storage. TTL auto-cleans after 120 days.

### 24. Pulumi Dynamic Provider for Pipeline Lifecycle (not EventBridge)

**Decision:** Use Pulumi Dynamic Provider (`PipelineRegistration`) for pipeline registration/deregistration instead of EventBridge + CloudTrail auto-registration.

**Problem:** EventBridge auto-registration had multiple failure modes:
- CloudTrail event propagation: 1-15 minute delay
- Fresh environments: no CloudTrail trail configured → registration never fires
- Asset-triggered pipelines: chicken-egg deadlock (needs subscriptions to trigger, needs execution to register)
- No cleanup on destroy: zombie pipelines in UI sidebar, orphaned asset subscriptions

**Dynamic Provider lifecycle:**
- `create`: `StartExecution(register_only=true)` — 2-3 second latency
- `diff`: compares `dag_hash` — skips if unchanged
- `update`: re-registers with new DAG
- `delete`: direct DDB cleanup (`pipeline_registry` + `asset_subscriptions`)

**Why not `apply()` callback:** `apply()` fires on every `polyris-deploy` (even no-change), has no error tracking, can't distinguish create from update, and doesn't work for destroy. Dynamic Provider gives Pulumi state tracking, retry on failure, and separate handlers for each lifecycle event.

**Migration:** EventBridge auto-registration (`auto_registration.tf`, ~300 lines + Lambda) removed in v69.1 after Dynamic Provider was verified in production.

### 25. Complete DAL Migration — Zero Direct Table Access in Routes

**Decision:** All 55 direct `boto3.Table()` operations in route files migrated to repository methods. Routes never call `table.get_item()`, `table.query()`, etc. directly.

**Problem:** DAL repos were introduced but routes still bypassed them with `repo.table.query(...)` or kept local table references. This defeated the purpose — schema changes, testing, and instrumentation still required touching every route file.

**Migration scope (v69.3):**
- 10 route files, 55 direct DDB calls → 0
- Patterns: `get_item` → `repo.get()`, `query` → `repo.query_by_date()` / `query_by_pipeline_execution()`, `update_item` → `repo.update()`, `put_item` → `repo.put()`, `scan` → `repo.scan()` / `list_all()`
- Raw queries (need `LastEvaluatedKey`, `Count`): `repo.query_by_date_raw(**params)`, `repo.scan_raw(**params)`
- Signature changes: `resolve_task_item()` dropped `table` param, `_query_pipeline_by_date_range()` and `_reconcile_running()` dropped `tokens_table` param

**Exception:** `health.py` circuit_breaker table — separate table with no repo. Acceptable — it's a monitoring-only table, not part of the core data model.

**What this enables:**
- Schema changes: edit 1 repo file instead of 10+ routes
- Testability: mock repos instead of boto3 internals
- Instrumentation: add logging/metrics at repo layer if needed
- Consistency: all conditional writes, pagination, error handling defined once

### 26. pytest-mock as Standard Test Mocking Library

**Decision:** All test files use `pytest-mock` (`mocker` fixture) for patching and mocking. `unittest.mock` is not used directly in tests.

**Rationale:**
- Auto-cleanup: patches reverted when test ends (fixture lifecycle), no `with patch()` context managers needed
- Less boilerplate: `mocker.patch('x.y')` vs `with patch('x.y') as mock:` + indentation
- Consistency: one pattern across all 448 tests instead of mixed approaches
- `mocker.spy()` for partial mocking when needed
- Better pytest integration: fixtures compose naturally with other fixtures

**Migration (v69.3):** 12 test files + 1 conftest migrated from `unittest.mock`. Patterns:
- `from unittest.mock import MagicMock, patch` → `mocker` fixture param
- `MagicMock()` → `mocker.MagicMock()`
- `with patch('x.y', mock):` → `mocker.patch('x.y', mock)`
- `@patch('x.y')` → `mocker.patch('x.y')` inside test body
- `patch.dict('os.environ', {...})` → `mocker.patch.dict('os.environ', {...})`
- `yield mock` in fixtures → `return mock` (mocker handles teardown)

**Exceptions (stdlib `unittest.mock` allowed):**
- `polyris/validation.py` — runtime module spoofing (not a test)
- `test_registration_provider.py` fixture — `sys.modules` bootstrap for Pulumi mock

**Dependency:** `pytest-mock>=3.0.0` in `pyproject.toml` `[project.optional-dependencies] dev`.

### 27. Unified Version String Across All Packages

**Decision:** `pyproject.toml`, `polyris/__init__.py`, and `ui/package.json` share the same version string. CI fails on mismatch.

**Rationale:**
- Single repo, single deploy → single version. No ambiguity about "which version is deployed?"
- UI previously on independent numbering (47.x) which diverged from backend (0.68.x) and CHANGELOG (v69.x)
- CI enforcement via `make check-versions` prevents drift

**Previous state:** Backend 0.68.1, UI 47.1.0, CHANGELOG v69.3 — three different numbers for the same release.

**Implementation:** `make check-versions` target + CI step in `python` job. Uses `grep` for Python files, `node -p` for package.json.

### 28. Specific Exception Types in Route Handlers

**Decision:** Route handlers use `except (ClientError, BotoCoreError)` instead of `except Exception`. Broad `except Exception` only at route-level top-level handlers.

**Rationale:**
- `except Exception` silently swallows unexpected errors (TypeError, KeyError, AttributeError) that indicate bugs
- `ClientError` catches AWS service errors (throttle, validation, not found)
- `BotoCoreError` catches network errors (timeout, connection refused, endpoint unreachable)
- Together they catch all AWS infrastructure issues but NOT programming bugs — those bubble up and get noticed
- 3 of 13 original catches were completely silent (`except Exception:` without `as e` or logging)
- Graceful degradation preserved: inner catches `continue` or return partial data

**Pattern:**
```python
from botocore.exceptions import ClientError, BotoCoreError

# ✅ Route-level: catch-all is correct (returns 500 to client)
def get_pipeline_executions(...):
    try:
        ...
    except Exception as e:
        log.error(...)
        return cors_response(500, {'error': str(e)})

# ✅ Inner: specific AWS errors
try:
    items = executions_repo.query_by_date(...)
except (ClientError, BotoCoreError) as e:
    log.error(...)
    continue  # Skip this date, try next

# ❌ Never: silent broad catch
except Exception:
    pass
```

**Migration (v69.4):** `pipelines_list.py` — 13 `except Exception` → 1. Added `from botocore.exceptions import ClientError, BotoCoreError`.

### 29. Generators DRY: Dispatch Dict, Shared Builders, Centralized Constants

**Context:** `generators.py` grew to 1545 lines with several DRY violations: `_generate_step_state` was a 238-line if/elif chain across 14 step types, wrapper input dicts were duplicated between Task and Step branches (15 identical fields), asset serialization used 3 different inline patterns (7 occurrences), and the same JSONata expressions and type lists appeared 2-5 times each.

**Decision:** Four targeted extractions, all verified by 60 ASL snapshot tests:

1. **Dispatch dict** — `_STEP_STATE_BUILDERS: Dict[str, callable]` maps `step_type → builder_function`. New step types need one function + one dict entry. Each builder is independently testable.

2. **Shared wrapper input** — `_build_wrapper_input()` returns the base dict with 15 common fields. `_build_task_branch` adds Task-specific extras (alerts, outlets, wait_for, trigger_rule, pipeline_execution_short, slack_mentions_formatted). `_build_step_branch` uses the base dict as-is. Wrapper SFN protocol changes now require one edit.

3. **Asset serialization helpers** — `_serialize_outlet(asset)` and `_serialize_inlet(asset)` replace 7 inline patterns. `_iter_dag_assets(dag)` yields `(task_id, asset, role)` tuples, shared between single-DAG and multi-DAG generators.

4. **Module constants** — `WRAPPER_STEP_TYPES`, `TRACKED_STEP_TYPES`, 8 `JSONATA_*` expressions. Inline definitions removed.

**Alternatives rejected:**
- Full module split (`generators/asl.py`, `generators/validation.py`, etc.) — premature at 1572 lines, would complicate imports and test paths
- `_build_task_config()` unification — Task and Step dataclasses have different field names (`glue_arguments` vs `arguments`, `container_overrides` vs `overrides`), a unified builder would need a mapping layer that adds complexity without reducing total lines
- `validate_asl` decomposition — pure function, rarely modified, 127 lines is acceptable for a validator with 5 checks

**Result:** Largest function reduced from 238 → 155 lines. 100% type annotation coverage. Zero business logic changes — 60 snapshot tests + 10-point business logic verification confirm identical ASL output.
---

### 30. SSM Parameter Store instead of Terraform Remote State for Pipeline Configuration

**Context:** Pulumi pipelines require ARNs of shared infrastructure (wrapper SFN, DynamoDB tables, IAM role).

**Previous approach:** `from_terraform_state()` — read S3 Terraform state file via IAM role assume. Required a dedicated IAM role, S3 access, and knowledge of bucket/key.

**Decision:** `from_ssm()` — Terraform automatically writes values to SSM Parameter Store under `/polyris/{stage}/` after `sam deploy`. Pulumi reads from there.

**Reasons:**
- Simpler UX: zero configuration for the user after `sam deploy`
- No separate IAM role needed for state reads
- SSM is the standard AWS pattern for cross-service configuration
- Transparent: values visible in AWS Console

**Consequences:** `from_terraform_state()`, `get_terraform_state()`, `TerraformState` — removed.

---

### 31. Removal of `config.arn()` and `[tool.polyris.accounts]`

**Context:** `config.arn()` constructed Step Function ARNs from account IDs in `pyproject.toml`.

**Decision:** Hardcode the full ARN with the `STAGE` variable directly in the pipeline:
```python
arn=f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-extract"
```

**Reasons:**
- Transparency: it is immediately clear what is being called
- Fewer abstractions — `pyproject.toml` becomes minimal
- `accounts` duplicated information that already lives in Terraform

**Consequences:** `config.arn()`, `config.accounts`, `_ConfigSection` — removed.

---

### 32. OpenTofu instead of Terraform + native S3 locking

**Context:** Terraform state locking required a DynamoDB table as an additional resource.

**Decision:** OpenTofu >= 1.7 with `use_lockfile = true` — native S3 locking via conditional writes, without DynamoDB.

**Reasons:**
- Single S3 bucket for everything (Terraform state + Pulumi state + locking)
- Fewer resources for the initial setup
- OpenTofu is open-source and actively maintained

**Consequences:** `provider.tf` updated, DynamoDB state lock table removed from documentation.

**Note:** Superseded by ADR #34 (AWS SAM replaces OpenTofu for shared infrastructure).

---

### 33. Terraform as a Public Module

**Context:** `terraform/shared/` was an internal directory not intended for direct use.

**Decision:** `sam/` — a public module with README, inputs/outputs documentation. `sam/` — example root module for users.

**Reasons:**
- Standard Terraform module pattern
- Users can consume the module directly via GitHub source
- Clear separation between module (reusable) and example (root)

**Note:** Superseded by ADR #34 (AWS SAM replaces OpenTofu for shared infrastructure).

---

### 34. AWS SAM instead of OpenTofu for Shared Infrastructure

**Context:** OpenTofu required an external state backend (S3 bucket), DynamoDB or S3 locking, and a separate CLI tool.

**Decision:** AWS SAM (`sam build && sam deploy`) + CloudFormation for shared infrastructure.

**Reasons:**
- AWS CloudFormation manages state natively — zero external dependencies
- SAM packages Lambda ZIP and uploads to S3 automatically
- `samconfig.toml` — one place for all parameters
- Launch Stack URL is possible for OSS onboarding
- Fewer tools: OpenTofu removed, SAM = AWS-native

**What stayed unchanged:**
- SSM Parameters — CFN writes to the same `/polyris/{stage}/` paths
- `from_ssm()` in Pulumi — works without changes
- Pulumi — remained for pipeline deploys (separate refactoring step, see ADR #35)

---

### 35. `polyris-deploy` as a Parallel Alternative to Pulumi

**Context:** Pulumi requires a separate CLI, account, and state backend. For new users this is an additional barrier.

**Decision:** `polyris-deploy` — a built-in CLI command that deploys pipelines via CloudFormation.

**What it does:**
1. Reads `dag.py` (the same file Pulumi reads)
2. Reads SSM `/polyris/{stage}/` (the same parameters)
3. Generates a CFN template (SFN + LogGroup + EventBridge)
4. `aws cloudformation deploy` — AWS manages state
5. Registers the pipeline via boto3

**What stayed unchanged:** *(Historical: at the time, Pulumi remained as an
equal second option.)* **Update — Pulumi was later removed entirely;
`polyris-deploy` (CFN) is now the only pipeline-deploy path.** See CLAUDE.md
#34–#35.

**Advantages of the CFN approach:** native AWS state, rollback, zero external dependencies.

---

### 36. SSM as a Cross-Stack Bridge (instead of `Fn::ImportValue`)

**Context:** Shared infra (SAM) and pipeline stacks need a way to pass ARNs between each other.

**Decision:** AWS SSM Parameter Store — `/polyris/{stage}/wrapper_arn` and similar.

**Why not `Fn::ImportValue`:**
- SSM is unified across all tools — Pulumi, polyris-deploy, CDK, boto3
- `Fn::ImportValue` ties to CFN — if a customer wants different IaC, it requires a rewrite
- SSM is human-readable: `aws ssm get-parameter --name /polyris/dev/wrapper_arn`

**The single downside of SSM vs ImportValue:**
`Fn::ImportValue` protects against deletion of a shared stack while dependent pipeline stacks exist.
SSM does not provide that protection — it must be controlled by process.

**If migrating fully to CFN (without Pulumi) in the future:**
SSM can be replaced with `Fn::ImportValue`. The SAM template already contains all the necessary Exports:

```yaml
# shared infra stack (already in sam/template.yaml)
Outputs:
  DependencyWrapperArn:
    Export:
      Name: !Sub "${Namespace}-${Stage}-polyris-wrapper-arn"
  OrchestrationRoleArn:
    Export:
      Name: !Sub "${Namespace}-${Stage}-polyris-orchestration-role-arn"

# pipeline stack — instead of reading SSM:
Resources:
  PipelineStateMachine:
    Properties:
      RoleArn: !ImportValue myorg-dev-polyris-orchestration-role-arn
      DefinitionSubstitutions:
        wrapper_arn: !ImportValue myorg-dev-polyris-wrapper-arn
```

CFN will additionally protect against an accidental `sam delete` while dependent pipeline stacks exist.

---

### 37. `DefinitionUri` + `AWS::Serverless::StateMachine` for SFN Definitions

**Context:** SFN definitions were originally inline in `template.yaml` as `DefinitionString`. This led to a problem: editing `sfn_templates/*.tpl.json` files had no effect — `sam deploy` did not see changes because it compared the hash of `template.yaml` (which was unchanged).

**Decision:** Convert all 13 SFN resources from `AWS::StepFunctions::StateMachine` + `DefinitionString` to `AWS::Serverless::StateMachine` + `DefinitionUri`.

**How it works:**
- `sam build` reads each `DefinitionUri` file and inlines it as `DefinitionString` in `.aws-sam/build/template.yaml`
- `sam package` (for releases) replaces local paths with S3 URLs — compatible with Tier 1 Launch Stack
- `DefinitionSubstitutions` works with `DefinitionUri` without changes

**Syntax changes** (`AWS::StepFunctions` → `AWS::Serverless`):
- `StateMachineName` → `Name`
- `StateMachineType` → `Type`
- `RoleArn` → `Role`
- `LoggingConfiguration` → `Logging`

**Exceptions:** `TestQuickSfn`, `TestSuccessSfn`, `TestFailureSfn` — remained
`AWS::StepFunctions::StateMachine` with inline `Definition` (simple single-step SFNs, no separate files).

**Result:** `sfn_templates/*.tpl.json` — single place for editing SFN definitions.
Edit the file → `sam build && sam deploy` → deployed.

---

### 38. Error Visibility — Product Requirement

**Context:** During development we found that errors in various layers of the system were silently lost.
Classic example: `query_subscriptions` returned `{'subscribers': []}` instead of an error when
it lacked permissions on a table — downstream tasks hung with no signal in UI or CloudWatch.

**Requirement:** Every error in the system must be visible to the operator. Visibility levels:

| Level | Visible to | When to use |
|-------|-----------|-------------|
| **UI (Notifications bell)** | All users | Task failures, infrastructure errors that block a pipeline |
| **CloudWatch Logs** | Operator/DevOps | All other errors, debug details |

**Lambda rules:**
1. Never swallow `AccessDeniedException` — always `raise`
2. Every `except Exception` block must log `error=str(e)` with context
3. If an error blocks downstream tasks — write a `_notify_warn_` record to `pipeline-tokens`
   so the Notifications bell automatically surfaces it in the UI

**SFN template rules:**
1. `Catch` blocks must preserve `$states.errorOutput` in Output — do not lose context
2. Errors in Express SFNs must either propagate via `Catch` → `failure_handler`, or be logged in CloudWatch (ALL level)

**`_notify_warn_` pattern implementation:**
```python
# When notify_dependents_via_sfn fails → downstream tasks stuck
executions_repo.put({
    'execution_name': f'_notify_warn_{execution_name}',
    'task_name': task_name,
    'pipeline_execution': pipeline_execution,
    'pipeline_name': pipeline_name,
    'date': date,
    'status': 'failed',
    'error': f'Downstream notification failed: {error}',
    'finished_at': datetime.now(timezone.utc).isoformat(),
    'ttl': int(datetime.now(timezone.utc).timestamp()) + 86400
})
```

**Important:** Records with `execution_name.startswith('_')` are internal/special.
All loops iterating `pipeline-tokens` items MUST filter such records:
```python
for item in items:
    if item.get('execution_name', '').startswith('_'):
        continue
```
This pattern is already applied throughout `console_api/routes/`.

**Special prefixes in `pipeline-tokens`:**
- `_pause_{pipeline_execution}` — pause state record
- `_notify_warn_{execution_name}` — infrastructure warning visible in UI

---

### 39. Asset Enrichment: Schema Declaration + asset_registry Removal

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

---

### 40. Responsive Layout: Flex Chains, No Viewport Magic Numbers

**Decision:** UI layout uses flex/grid chains for height inheritance. Mobile responsiveness is mandatory across all views via `_mobile.css` overrides or per-component `@media` queries.

**Context:** Several layout bugs traced to viewport magic numbers (`calc(100vh - 180px)`, `calc(90vh - 130px)`) that broke when:
- Browser zoom changed
- Header height changed
- Heavy components (ReactFlow, Gantt) were rendered inside scroll containers with capped height
- Mobile breakpoints were missing for new views (e.g. `AssetDetailPage` had zero `adp-*` mobile rules)

The bug pattern: a component with `height: 100%` sits inside a parent with `max-height: calc(100vh - Npx)`, so the inner component renders at a wrong size whenever `N` doesn't match the actual header height.

**Decision:**

1. **No viewport magic numbers.** Forbidden patterns:
   - `calc(100vh - Npx)` for height
   - `calc(100vw - Npx)` for width
   - Inline Tailwind `h-[calc(100vh-Npx)]`

   Allowed: parent-relative `calc(100% + Npx)` (for offsets), `calc(100vw - 1rem)` for dropdown-fit-on-mobile.

2. **Use flex chains.** A heavy component (ReactFlow, Gantt, table) inherits height like this:
   ```css
   .parent { display: flex; flex-direction: column; flex: 1; min-height: 0; }
   .heavy-component { flex: 1; min-height: 0; height: 100%; min-height: 400px; /* baseline */ }
   ```
   The `min-height: 0` is critical — without it, flex children won't shrink.

3. **Mobile breakpoints.** Three tiers in `_mobile.css`:
   - `≤1024px` — tablet: compact sidebars, hide breadcrumbs
   - `≤768px` — mobile: hamburger, sidebar overlay, modals → 95vw
   - `≤480px` — small mobile: minimal font sizes, 90vw sidebar

4. **Per-component mobile rules.** Components using CSS Modules (scoped class names) cannot be styled from `_mobile.css`. They must include their own `@media (max-width: 768px)` block at the end of their `.module.css` file.

5. **Modals.** Either use `BaseModal` (inherits `_mobile.css` rules for `.base-modal-content`) or add explicit `@media` block in own CSS module.

6. **Definite viewport height at root.** The `.app` shell must use `height: 100vh; overflow: hidden` — never `min-height: 100vh`. Reason: descendants with `flex: 1` or `height: 100%` resolve to content size when parent has no definite height. With `min-height` alone, ReactFlow/Gantt collapse to ~100×100px because their parent chain bottoms out at content-defined height somewhere up the tree. The `<body>` keeps `min-height: 100vh` so non-app content (LoginPage, error pages) still scrolls naturally. `.main` needs `min-height: 0` so flex children can shrink properly.

7. **TSX `className` strings vs CSS Module imports.** When a TSX file uses a plain string className like `className="alf-foo"` (without `import styles from './Foo.module.css'`), the class must be defined in a **global** CSS file (e.g. `_assets.css`). Editing a same-name class inside a `.module.css` file is **dead code** — CSS Modules hash the class name, so the global string never matches. Before editing any `.module.css`, verify the corresponding TSX actually imports it as a module:
   ```bash
   grep -E "import.*from.*Foo\.module\.css" Foo.tsx
   # If no import — that .module.css is dead code, edit the global CSS instead
   ```

**v0.70.8 cleanup:**
- 3 viewport magic numbers eliminated:
  - `_assets.css` — `.av-catalog max-height`
  - `_modals.css` + `BackfillModal.module.css` — `.bf-backfill-modal .modal-body max-height`
  - `AssetsView.tsx` — lineage tab inline Tailwind
- `AssetDetailPage` extracted from `.av-catalog` scroll container; rendered directly in `.av-main-content` flex column
- 5 components got mobile rules: `AssetDetailPage` (`adp-*`), `BackfillModal`, `CommandPalette`, `CalendarView`, `AssetLineageFlow` baseline `min-height`
- Lineage container desktop baseline: `min-height: 400px` (was only mobile rule)

**v0.70.9 root cause fix:** After v0.70.8, AssetDetailPage was still clipped and Lineage graph still collapsed. Root cause: `.app { min-height: 100vh }` instead of `height: 100vh`. Changed to definite height + `overflow: hidden`. Added `min-height: 0` to `.main`. Removed conflicting `grid-template-columns: 330px 1fr` from `.av-assets-view` (was clashing with `.av-assets-layout: 200px 1fr` since both classes always applied to same `<div>`).

**v0.70.10 lesson — TSX className/CSS Module mismatch:** `AssetLineageFlow.tsx` uses `className="alf-lineage-flow-container h-full w-full"` — plain string, NOT imported from `.module.css`. The component does not import its own `AssetLineageFlow.module.css`. Therefore the v0.70.8 edits to `.lineage-flow-container` inside `AssetLineageFlow.module.css` were dead code — the class was never used. The actual styling lives in global `_assets.css` under `.alf-lineage-flow-container`. After moving the fix to the correct location, the full `AssetDetailPage` flex chain still required `min-height: 0` and `display: flex; flex-direction: column` added to `.adp-body`, `.adp-main`, `.adp-content--lineage`. `panel-section` (used by AllTasksView/AllRunsView) also needed `flex: 1; min-height: 0` because `<main>` is flex column.

**Verification on UI changes:**
- `grep -rn "calc(100v[hw]" ui/src/` should return zero new matches
- `grep -rn "min-height: 100vh" ui/src/styles/` — should ideally only appear on `body` or auth gate root, not on `.app`
- For each `<div className="some-class">` in TSX, verify whether `some-class` is from imported CSS Module (`styles['some-class']`) or global CSS (plain string) — the styling source must match
- New views must have mobile rules covering at least `≤768px` breakpoint
- **Visual test** at viewport widths: 320px, 768px, 1024px, 1920px before merge — vitest runs in jsdom which doesn't apply real CSS, so automated tests cannot catch CSS layout bugs

### 41. URL Routing: CloudFront Function + window.history for Deep State

**Decision (v0.71.8):** Use a CloudFront Function for SPA URL rewriting at the edge, Next.js file-system routes for top-level navigation, and `window.history.pushState/replaceState` directly for deep state in URL search params.

**Context:** Until v0.70.x all top-level navigation lived in Zustand (`mainView` field). URLs looked like `/?view=assets&pipeline=etl_main` on root path. Two issues:
1. URLs read like an admin tool, not a product
2. Refresh-stability of deep state was fragile

We wanted product-grade URLs (`/pipelines/etl_main`) without infrastructure surprises.

**What didn't work (v0.71.0 through v0.71.7):**

Seven attempts tried frontend-only solutions:
- v0.71.0–3: store ↔ URL bidirectional sync hooks
- v0.71.4: path → query param hotfix
- v0.71.5: trailing-slash pathname normalization
- v0.71.6: trailing-slash target URLs + RootPage `[]` deps fix
- v0.71.7: full clean rebuild without sync hooks

Each version exposed a new bug in production. v0.71.7 fixed the loop but URL still didn't update on pipeline click and executions list disappeared. The pattern revealed by debugging: any client-side URL strategy fails because the server (S3 via CloudFront) cannot find files for path-based URLs.

**Root cause:**

The deployment is `next build` with `output: 'export'` → static files in S3, served via CloudFront with **OAC and S3 REST origin** (not the S3 website endpoint).

- S3 REST endpoint does NOT auto-resolve `index.html` inside folders.
  `GET /pipelines/` returns 404 because there's no S3 key matching that exact path; `index.html` resolution is a *website endpoint* feature.
- CloudFront's `CustomErrorResponses` (404 → /index.html) then serves **root** `/index.html` regardless of requested path.
- Next.js with `output: 'export'` only generates HTML for routes with corresponding `page.tsx`. `/pipelines/index.html` exists if `app/pipelines/page.tsx` exists, but `/pipelines/etl_main/index.html` does not.
- `router.push('/pipelines/etl_main')` triggers an RSC prefetch (`?_rsc=...`) for the manifest. The manifest doesn't include this dynamic path → fallback path → loop.

No frontend-only fix can resolve this. The server has to know how to serve `/pipelines/anything` → `/pipelines/index.html`.

**Options considered:**

1. **Switch S3 origin to website endpoint:** Has automatic `index.html` resolution but loses OAC (must use bucket policy with public read or signed URLs). Significant security regression.

2. **Lambda@Edge with URL rewrite:** Works but cold-start latency, more expensive, more complex IAM, more failure modes.

3. **Dynamic Next routes (`app/pipelines/[name]/page.tsx`):** Requires `generateStaticParams` to enumerate all pipeline names at build time. Pipeline names are runtime data, not build-time. Returning `[]` doesn't work with `output: 'export'`.

4. **CloudFront Function with URL rewrite (chosen):**
   - Runs at edge, ~1ms latency overhead
   - ~$0.10 per million requests (effectively free at our scale)
   - Inline JavaScript, no IAM role, no Lambda, no CloudWatch logs needed
   - Validated and published as part of CloudFormation deploy
   - Disassociation is one click in Console for instant rollback

**Implementation:**

```yaml
ConsoleUiUrlRewriteFunction:
  Type: AWS::CloudFront::Function
  Properties:
    AutoPublish: true
    FunctionConfig:
      Runtime: cloudfront-js-2.0
    FunctionCode: |
      function handler(event) {
        var req = event.request;
        var uri = req.uri;
        var match = uri.match(/^\/(pipelines|assets|tasks|runs)(\/[^.]*)?$/);
        if (match) {
          req.uri = '/' + match[1] + '/index.html';
        }
        return req;
      }
```

The regex matches `/pipelines`, `/pipelines/`, `/pipelines/foo`, `/pipelines/foo/bar` (any depth, any name) but NOT paths with file extensions (`.html`, `.js`, `.css` etc). `/_next/static/foo.js` passes through because it doesn't start with one of the four view prefixes anyway.

Associated as `viewer-request` event handler on the default cache behavior.

**Frontend pattern (v0.71.8):**

- Top-level routes: `app/pipelines/page.tsx`, `app/assets/page.tsx`,
  `app/tasks/page.tsx`, `app/runs/page.tsx`. Each renders `<App />`.
- `App.tsx` uses `usePathname()` to determine which view to render.
- `mainView` removed from Zustand store. Pathname is the source of truth.
- Top-level navigation: `router.push('/{view}/')`.
- Deep state in URL: `useUrlSync` writes via `window.history.pushState/replaceState` directly (no Next router involvement → no RSC prefetch → no loop).

**Why deep state via `window.history`:**

Even with the CloudFront Function in place, using `router.push` for search-param-only changes (e.g., `?pipeline=etl_main`) triggers Next's RSC prefetch logic. Static export doesn't generate the prefetch payloads → 404. We could disable prefetch but `window.history` is simpler — it just updates the URL bar with no network involvement, and our app reads search params on mount.

**URL examples:**

| URL | Meaning |
|---|---|
| `/pipelines/` | Pipelines view |
| `/pipelines/?pipeline=etl_main` | Pipelines view, etl_main selected |
| `/pipelines/?pipeline=etl_main&mode=gantt&date=2025-04-15&execution=exec-abc` | Specific run view |
| `/assets/` | Assets catalog |
| `/tasks/` | All tasks |
| `/runs/` | All runs |

**Trade-offs:**

- ✅ Path-based top-level URLs (product-grade UX)
- ✅ Refresh stable on every URL
- ✅ Browser back/forward works
- ✅ Cmd+click on tab opens new browser tab
- ✅ Stable on production CloudFront + S3 OAC + static export
- ⚠️ Adds infrastructure dependency (CloudFront Function)
- ⚠️ Asset detail (selectedPartition, selectedEvent) and filters
  (taskFilter, runFilter) are not in URL — deferred to future
- ⚠️ Deploy must be ordered: SAM first (Function active), then frontend.
  Reverse order = 404 loop until Function activates.

**Rollback paths:**

1. **Console disassociation** (fastest, ~5 min for edge propagation):
   AWS Console → CloudFront → distribution → Behaviors → Edit → remove
   Function association → Save. Then redeploy v0.70.18 frontend.

2. **CFN redeploy** (~10 min): Extract `polyris_baseline_v70_18.tar.gz`,
   `sam deploy && deploy.sh && invalidation`.

**Lesson for future routing changes:**

With static export + S3 + CloudFront REST origin, only routes that
have build-time HTML files can serve as path segments. Anything dynamic
(specific pipeline name, asset name) must go in URL search params,
updated via `window.history` (not Next router) to avoid RSC prefetch
404 loops.

If we ever want `/pipelines/etl_main` (asset name in path), the options
are: (a) extend the CloudFront Function to also serve `/pipelines/etl_main`
→ `/pipelines/index.html`, or (b) add a build step that pre-generates
HTML for known pipeline names. (a) is simpler if the in-app router can
read the pathname segment as the pipeline ID.

---

### 42. Asset Schema 2.0: Typed `Column` Class with Platform-Agnostic Type System

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

---

### 43. Glue Schema Sync: On-Demand, Not Scheduled

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

---

### 44. Schema Adapters: pyarrow / pydantic / Glue as `Asset.from_*` Constructors

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


### 45. Cross-Account / Cross-Region Glue Catalog Support

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


### 46. DDL/Schema Export — Phased Architecture

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


### 47. Asset Lineage `last_updated` Enrichment — Two Endpoints, Two Scopes

**Status:** Adopted in v0.75.5.

**Context:** The Console UI has two distinct needs related to asset
materialization timing, and a single endpoint would have to pick one:

  1. **Date picker scope** — "show me events that happened on May 7":
     drives the catalog "Recent Events" panel, the per-day execution
     filter, and date navigation. Needs a query indexed by date.
  2. **Catalog Status column** — "for this asset, when did it last
     materialize, ever": drives the Status column on the asset list
     and the freshness banner on the detail page. Needs the latest
     event per asset, independent of any date filter.

A single `/recent-events?date=...` endpoint conflated both. The
catalog Status column read from the same date-scoped result, so when
an operator opened the UI on a day with no executions yet (e.g. after
a fresh deploy + backfill, before the first scheduled run), every
asset rendered as if it had never run — even assets with thousands
of historical events visible on their detail pages. v0.75.3 made this
worse by substituting "Never" for the calculator's honest "No data"
label, an over-confident assertion that was demonstrably false.

**Decision:**

  - Keep `GET /api/assets/recent-events?date=YYYY-MM-DD` exactly as it
    was — date-scoped query against the asset-events GSI
    (`date-index`, key=`execution_date`). Powers anything that wants
    "what happened on day X".
  - Extend `GET /api/assets/lineage` to stamp each asset entry with
    `last_updated`: the ISO timestamp of the most recent event for
    that asset, looked up directly via the primary key
    (`asset_name` HASH + `event_time` RANGE, descending, LIMIT 1).
    Independent of any date scope. Powers the catalog Status column
    and the detail-page banner fallback.
  - The frontend staleness calculator gains a third resolution path
    that consumes this fallback: `recentEvents[name]` →
    `asset.last_updated` → unknown. Cell rendering reverts to the
    natural `staleness.label` (no `stalenessText` substitution).

**Why not "always return last_updated everywhere"?** The cost shape
matters. `last_updated` enrichment is N point-lookups (N = asset
count), parallelised, ~5ms each. For the 23-asset catalog Mike runs,
that's 50-100ms RTT-bounded — acceptable on a list-page load. Adding
the same enrichment to per-event responses (e.g. in the events list)
would multiply DDB reads by event count without UI benefit; the
events list already shows event_time per row.

**Why not pre-compute `last_updated` at write time and store it on
the pipeline_registry asset record?** Considered, rejected:

  - Write path would need to update two tables atomically, doubling
    write cost on every materialization
  - DynamoDB doesn't offer multi-table transactions cheaply
  - The read-side lookup is already cheap (~5ms PK query) so the
    write-time optimization saves negligible read time at the cost
    of write-time complexity and edge cases (transactional rollback,
    last-writer-wins races between concurrent task completions)

Read-time enrichment is the right tradeoff here — write path stays
simple, read cost is bounded.

**Why parallel point-lookups instead of a single GSI scan grouped by
asset?** The GSI we have (`date-index`) is keyed on `execution_date`,
not `asset_name`. Adding a second GSI keyed on asset would duplicate
storage and cost ~$0.25/GB/month for the asset-events table, which
grows unbounded. Per-asset PK lookup costs roughly the same per call
as the GSI alternative, parallelizes cleanly, and adds zero
infrastructure. Verified: 23 assets × 1 query @ ~5ms = ~5ms wall time
when fully parallelized, well within the Lambda's 30s budget.

**Resilience:** one failing lookup logs a warning and degrades to
`last_updated: ""` for that asset only. The endpoint never 500s
because of a single asset's transient DDB throttle. Tested in
`test_lineage_last_updated.py::test_one_failing_lookup_does_not_break_others`.

**Out of scope (deferred):**

  - **Caching the lookup map** — Lambda memoization across
    invocations would help but introduces stale-data risk (an asset
    materializes after a prior list-page load, the cached `last_updated`
    is now wrong). Skipped until measurable latency complaint exists;
    50-100ms is acceptable.
  - **Aggregated "last_updated" in pipeline_registry** — see above,
    rejected for write-cost / complexity reasons.
  - **Cross-region last_updated** — out of scope; Polyris is
    single-region per stack and `asset-events` table is per-stack.


### 48. AssetDetailPage Composition — Tabs as Independent Sub-Components

**Status:** Adopted in v0.75.8.

**Context:** Through v0.75.0–v0.75.7 the asset-detail view grew from a
modest component into a 1124-line monolith. All six tabs (Overview,
Schema, Partitions, Events, Checks, Lineage), the Glue drift panel,
the schema-copy actions, and the sidebar were inlined in
`AssetDetailPage.tsx`. The growth path was straightforward — each
feature (typed schemas, drift detection, copy buttons, conflict
banners, status banner refinements) added 50–200 lines to the same
file. By v0.75.7 the file was hitting the readability limit:

  - Any change required scrolling through ~1000 lines of unrelated tab
    code to find the right block.
  - Tab-internal helpers (e.g. `humanizeGlueError`, `renderMarkdown`,
    `DriftSection`) sat at the bottom of the file mixed with the
    component, blurring the line between page-level and tab-level.
  - Tests for the page exercised all tabs through one mounted component,
    making test failure messages ambiguous about which tab regressed.
  - The next feature on the backlog (interactive lineage editor, asset
    checks UI authoring) would push the file past 1500 lines.

**Decision:** Split the page into an orchestrator and one component per
tab, organised under `ui/src/components/asset-tabs/`:

```
ui/src/components/
├── AssetDetailPage.tsx        ← orchestrator (~370 lines):
│                                   header, tab nav, body dispatcher,
│                                   sidebar, page-scoped queries
└── asset-tabs/
    ├── types.ts               ← TabContext, AssetDerived
    ├── index.ts               ← barrel re-export
    ├── TabOverview.tsx        ← (~245 lines)
    ├── TabSchema.tsx          ← (~175 lines)
    ├── TabPartitions.tsx      ← (~125 lines)
    ├── TabEvents.tsx          ← (~85 lines)
    ├── TabChecks.tsx          ← (~30 lines, placeholder)
    ├── TabLineage.tsx         ← (~40 lines, wraps AssetLineageFlow)
    ├── GlueSyncPanel.tsx      ← (~205 lines, used by TabSchema)
    ├── SchemaCopyButtons.tsx  ← (~110 lines, used by TabSchema)
    └── glueHelpers.tsx        ← humanizeGlueError + DriftSection
```

**State allocation rules** (the heart of why the split works):

  - **Page-scoped state stays in the orchestrator.** This is anything
    that needs to outlive a tab switch:
    - `activeTab` — which tab is shown
    - `useAssetEventsQuery` — fetched once, consumed by 3 tabs
    - `useAssetGlueSchemaQuery` — re-mounting TabSchema must not
      re-trigger the Glue API call
    - `derived` (memoized AssetDerived) — same source of truth across tabs
  - **Tab-local state stays inside the tab.** This is interaction state
    that resets if the user navigates away:
    - `selectedPartition` — internal to TabPartitions
    - `selectedEvent` — internal to TabEvents
    - `copied` — internal to SchemaCopyButtons (UI affordance only)

The rule is simple: if losing the state on tab-switch would surprise
the user, hoist it to the orchestrator. If "I clicked back into this
tab and my selection is gone" is acceptable UX, the state belongs in
the tab.

**Props contract — `TabContext`:**

Every tab receives a `TabContext` (defined in `asset-tabs/types.ts`)
containing the seven fields used by 2+ tabs. Tab-specific extras
extend the context on the tab's own Props interface (e.g.
`TabSchemaProps extends TabContext` adds `derived`, `schemaConflicts`,
`glueQuery`). This pressure keeps the shared context narrow — when a
field is needed by exactly one tab, putting it on `TabContext` is the
wrong move because every other tab now has to accept a useless prop.

**Why not React Context for shared data?** Context would let tabs read
state without explicit props. Rejected:

  - Hides the dependency graph — you can't tell what data a tab uses
    by reading its signature; you have to read its body
  - Harder to test — every tab test needs a Context provider wrapper
  - Encourages "since we have it in context, we'll just use it from
    anywhere" creep, which breaks the page-vs-tab state rules above

Explicit props are the right tradeoff at this scale. If the page
grows to dozens of tabs and the prop list becomes unwieldy, revisit —
but that's a problem we'd be lucky to have.

**Why not lazy-load tabs?** Each tab is small. Code-splitting at the
tab boundary would save bundle size only if tabs were heavy (e.g.
embedded charts, large libraries). They aren't — biggest tab
(TabSchema) is ~175 lines and pulls only icons + utils. Deferred
until measurable bundle pressure exists.

**Adding a new tab:** the procedure is one file create + three small
edits. Documented in `docs/development/ADDING_ASSET_TABS.md` with a
working template.

**Refactor triggers — when to apply this pattern elsewhere:**

  - A component file exceeds ~800 lines AND mixes multiple distinct
    UI surfaces (not just lots of small helpers — distinct surfaces
    is the trigger). The page's natural "tabs", "sections", or "modes"
    are the seam.
  - A tab/section's internal helpers (counter, modal, formatter)
    start to outnumber the tab's own JSX. Extract them as siblings
    in a `components/` subdirectory.

The pattern is mechanical: identify the seams, create a directory,
move the chunks, pass shared state down as props. There is no
abstraction tax — each tab is just a function that takes props and
returns JSX, the same as before.


---

### 49. Asset Matrix View — Cross-asset temporal grid (v0.76.0)

**Context.** Recurring operator feedback (per tech-lead interviews from
Feb 2026) asks for a "global picture of assets across time" — answer to
the question *"what's broken, and when did it start?"* The existing
catalog view shows assets as a list with current freshness; the lineage
view shows graph relationships. Neither shows the temporal × asset
cross-section needed for outage forensics, partition-status review, or
backfill scoping. The Matrix view fills that gap.

#### Design history — honest record of the iteration

The implementation went through two passes before landing on the right
architecture. Recording both because future maintainers will think to
revisit the same alternatives and deserve to see why we picked what
we did.

**Pass 1 (initially shipped, then reverted in same release):
event-sourcing approach.** Every task with outlets emitted three asset
events to `asset_events`:

  - `event_type='started'` written before task execution
  - `event_type='materialized'` written on success (already existed pre-v0.76)
  - `event_type='failed'` written on failure path

The Matrix endpoint queried `asset_events.date-index` per date and
derived cell status from the latest event per (asset, date). Conceptually
elegant — a single source of truth, time-ordered, naturally handling
regenerate scenarios as "latest event wins."

**Why we rejected pass 1:**

  1. *Cost.* +4 SFN transitions and +1 DDB write per task with outlets,
     for every run including successes. At SaaS scale (100 customers ×
     500 tasks/day) this added ~$165/month — material against the
     project's "dramatically cheaper than managed orchestrators" positioning. The
     transitions weren't doing real work; they were duplicating state.
  2. *CLAUDE.md #12 violation.* Task status is already canonical in
     `pipeline-tokens`, updated by `Update_Status_Running` and
     `Save_Success`/`Save_Failed` on the SFN happy paths. The new
     event_type field stored the same information twice, with no new
     information added. The principle "before creating anything new,
     actively search for what already exists" was not honored.
  3. *Risk.* Pass 1 modified the core `RunTaskHelperSfn` definition that
     runs on every task in every pipeline. The reward (matrix view)
     didn't justify the blast radius (any bug in the new states could
     affect every pipeline).

**Pass 2 (current implementation): state-based derivation.** The Matrix
endpoint is a *projection* over existing canonical state. Nothing is
written to support it.

Sources:

  - `pipeline-tokens.date-pipeline-index` — projects `status`,
    `task_name`, `finished_at`, `running_at`, `error` per task per date
  - `pipeline_registry.tasks[].outlets` — which assets a task produces
    (already loaded by `_build_assets_from_pipelines`)
  - `pipeline_registry.asset_schedule` — which assets a DAG consumes
    (used for Queued cells; already loaded by other matrix logic)

The SFN template is **unchanged from v0.75.8**. The matrix is purely a
read-side concern.

#### Cell types

Five states. Four are derived from task records in `pipeline-tokens`;
one is derived from `asset_schedule` declarations:

| Cell | Trigger |
|------|---------|
| 🟢 Materialized | Latest producer-task record for (asset, date) has `status='success'` |
| 🔴 Failed | Latest producer-task record has `status='failed'` (or `aborted`/`stopped` — defensive: non-happy paths display as failed) |
| 🟡 Running | Any producer-task record has `status='running'` (running beats older finalized to give operator real-time visibility) |
| 🟠 Queued | No producer-task record exists for (asset, date), but at least one consumer DAG has this asset in its `asset_schedule` |
| ⚪ Missing | No producer record and no consumer waiting (returned as `null` to the UI) |

**Stale is excluded** — staleness is a "now" concept and would
misrepresent historical truth at each date. The Catalog tab continues
to show staleness as before.

#### Regenerate semantics

When backfill re-runs a date, `pipeline-tokens` ends up with multiple
records for the same (task_name, date) pair (each `start_execution`
creates a fresh `execution_name`). Resolution:

  1. If any matching record has `status='running'` → cell is 🟡 Running
     (operator sees what's happening now, not stale history).
  2. Otherwise pick the record with the latest `finished_at` and use
     its status. Example: orig run succeeded at 10:05, backfill failed
     at 11:05 → cell is 🔴 Failed with the backfill's error message,
     because that's the latest reality.

This naturally handles success→failed regenerations without timestamp
merging across tables (which the rejected pass-2 alternative would have
needed if we'd kept `asset_events` as the materialized source and only
moved running/failed to derivation).

#### Internal record filtering (CLAUDE.md mandate)

`pipeline-tokens` also holds non-task records (`_pause_*`,
`_notify_warn_*`) used for pause state and infra warnings. CLAUDE.md
explicitly mandates filtering these from every loop. `matrix.py` uses
`is_internal_record(execution_name)` on every record returned by
`query_by_date` — see `_fetch_date_safely`. Without this, a
`_notify_warn_` record with a real `task_name` field would be picked up
during cell derivation and produce false failed cells.

#### Multi-producer assets

The reverse index `asset_to_tasks` (built once per request from
`pipeline_registry`) maps each asset to a *list* of producer tasks.
Rare but real: partitioned writes where multiple tasks contribute to
the same asset name. `_derive_cell` accepts records from any task in
the list and picks the latest by the resolution rules above.

#### Cost — measured outcome

At SaaS scale of 100 customers × 500 tasks/day, this implementation
adds approximately:

  - $0 SFN transition cost (no template changes)
  - $0 DDB write cost (no new writes)
  - ~$3/month DDB read cost (matrix endpoint queries `pipeline-tokens`
    and `pipeline_registry` once per render; React Query polls every
    30 s)

Versus pass 1's $165/month — roughly 98% saving. This is the kind of
gap that matters for the project's competitive positioning.

#### Endpoint surface

```
GET /api/assets/matrix?from=YYYY-MM-DD&to=YYYY-MM-DD&group=&include_views=false

200 OK:
  {
    "dates": ["2026-04-15", "2026-04-16", ...],
    "rows": [{
      "asset_name": "acme/orders",
      "group": "acme",
      "type": "asset",
      "cells": {
        "2026-04-15": {
          "status": "materialized",
          "event_time": "2026-04-15T10:05:00Z",
          "source_task": "extract",
          "source_dag": "acme-daily",
          "error": ""
        },
        ...
      }
    }],
    "summary": {
      "total_assets": 42,
      "materialized_count": 580,
      "failed_count": 2,
      "running_count": 1,
      "queued_count": 3,
      "missing_count": 102
    },
    "range": {"from": "2026-04-15", "to": "2026-04-28"}
  }
```

Range bounds: default 14 days, hard maximum 60 days. Parallel queries
fan out one-per-date (capped at 14 workers). Partial DDB failures on a
date degrade that date to missing cells; the rest of the matrix still
renders. AccessDenied re-raises and surfaces an IAM-fix hint per ADR #38.

#### Frontend

`AssetMatrixView` is a third tab alongside Catalog and Lineage. The
cell payload shape is identical to the v0.76 pass-1 attempt, so no UI
changes were needed when we replaced the backend. One added field
(`error`) is consumed by the failed-cell tooltip to show the failure
reason without requiring a click through to task detail.

Mobile responsiveness follows ADR #40: cell sizes shrink at ≤1024px,
≤768px (default range cut to 7 days), ≤480px (default range 5 days).
Per CLAUDE.md #10, all CSS uses BEM-prefixed globals (`amv-*` in
`_assets.css` and `_mobile.css`); no `.module.css` files were created.

#### Out of scope for v1

These are listed explicitly so they don't re-appear as "did we forget X?"
questions during future work:

  - Per-tenant permissions (waiting for SaaS launch)
  - Pre-computed daily snapshot (premature; current cost is fine)
  - SSE / WebSocket real-time updates (30 s polling is the project standard)
  - Cron-aware "scheduled but missed" distinction (today's Queued cell
    treats every date in range as a potential consumer-waiting date;
    refining this needs cron introspection)
  - Bulk cell select for batch backfill
  - Column-header click filters
  - CSV / image export


### 50. Asset Granularity — declarative DSL + advisory Glue auto-detect + drift detection (v0.77.0)

**Status:** Accepted.
**Date:** May 2026.

#### Context

ADR #49 shipped the Asset Matrix as a daily-only operational view —
columns are calendar dates, cells are task status on that date. After
deployment we received feedback from tech lead Myroslav: granularity
is hardcoded as day, but reality is more complex — some pipelines run
weekly, twice daily, hourly. How should the matrix show multiple
partition values per day? How to aggregate? How does the filter let
operators choose which to display?

This was a fair critique — v0.76 made an implicit assumption (every
asset is daily) that wouldn't survive contact with non-daily pipelines.
We needed a path forward without descending into Dagster-level
complexity (Multi-dimensional Partitions, Dynamic Partitions, etc.).

#### Decision

Add a **declarative `granularity` field** to the `Asset` DSL, defaulting
to `"daily"` so existing code keeps working with zero boilerplate. The
allowed set is closed: `hourly | daily | weekly | monthly`. Anything
more exotic (quarterly, region-partitioned, dynamic) stays out of scope
until concrete user demand appears.

```python
Asset("acme/orders")                                  # default daily
Asset("acme/weekly_summary", granularity="weekly")
Asset("acme/monthly_report", granularity="monthly",
      partition_start="2024-01")
Asset("acme/hourly_events", granularity="hourly")
```

The matrix endpoint accepts a `granularity` query param and renders
columns in the corresponding format (`YYYY-MM-DD` / `YYYY-Www` /
`YYYY-MM` / `YYYY-MM-DDTHH`). The frontend gets a granularity dropdown.
Cells carry per-status counts (`partition_count`, `success_count`,
`failed_count`, `running_count`) so tooltips surface hidden re-runs.

Three companion features round out the change:

1. **Filter mode for cross-granularity views.** The matrix shows only
   assets matching the requested granularity. Other assets are hidden
   — UI dropdown switches. Keeps the column axis consistent without
   "spanning cells" complexity (one row's cell covering 7 columns to
   represent a week). Unified multi-granularity view feasible but
   deferred to v0.78 if real users ask.

2. **Glue auto-detect at deploy time.** For Glue-backed assets,
   `polyris-deploy` reads `PartitionKeys` and infers granularity from
   naming: `year/month/day` → daily, `year/month/day/hour` → hourly,
   `year/week` → weekly, `year/month` → monthly. Inference is
   **advisory** — declared values win, but mismatches print warnings.

3. **Drift detection** via new endpoint `GET /api/assets/drift`.
   For each asset with declared granularity, count successful
   materializations over last 30 days and compare to expectations
   (`hourly:720, daily:30, weekly:4, monthly:1`). Severity:
   `≥0.5` → healthy, `0.25-0.5` → warning, `<0.25` → critical.
   UI binds the result to a per-row badge.

#### Why declarative (not auto-detect)

Both Dagster (`partitions_def`) and other orchestrators chose declarative despite their larger engineering teams. Their
reasoning matches ours: partition cadence is *intentional* (the user's
design choice), not observable. A pipeline that hasn't run for a week
looks "monthly" to an inference engine but might be daily-and-broken.

We chose declarative as source of truth, hybrid for DX:

  - **Default = `"daily"`** — zero-boilerplate simple cases. The vast
    majority of batch ETL is daily, and `Asset("name")` keeps working.
  - **Glue auto-detect is advisory** — confirms or contradicts
    declaration at deploy time; never overrides. Unconventional naming
    gets a warning users can ignore.
  - **Drift detection** — runtime "did the user lie?" check that
    complements deploy-time Glue check. Catches *behavioral* drift
    (declared weekly, no materializations for two months) that Glue
    metadata can't see.

#### Why filter mode (not unified)

Three options considered for cross-granularity rendering:

  - **A — Filter mode (chosen).** One granularity at a time; others
    hidden. UI dropdown switches.
  - **B — Unified view.** All granularities in one matrix; weekly cells
    span 7 daily columns. Visually richer but introduces "cells of
    different sizes" complicating rendering and click handlers.
  - **C — Per-asset granularity axis.** Each row has its own axis.
    Hardest to read, defeats the purpose of a 2D grid.

Filter mode is simplest correct answer for v0.77. If users ask for B,
it's an additive change — endpoint already returns granularity per row.

#### Why drift detection (not "trust the user")

Without drift detection, silent daily default is dangerous: user who
forgot `granularity="weekly"` sees weekly asset as 6 missing + 1
materialized every week. Technically correct but misleading — they'll
think their pipeline is broken.

Drift detection catches this: 4 weekly materializations vs 30 daily
expectation → ratio 0.13 → critical drift badge. Tooltip suggests
"update the asset's granularity declaration" — exactly the right fix.

Cost: one Scan + 30 parallel Queries per drift refresh (5-min polling).
Small project: negligible (~$0.13/month per 100 customers).

#### Backward compatibility

  - **Asset DSL:** `granularity`/`partition_start` are kwargs with
    defaults. Existing code unchanged.
  - **pipeline_registry:** v0.77+ records include `granularity`;
    pre-v0.77 records default to "daily".
  - **Matrix endpoint:** `granularity` query param defaults to daily.
  - **Cells:** new `partition_count`/`success_count`/`failed_count`/
    `running_count` fields — clients ignoring extras keep working.

No SFN template changes. No DDB schema changes. No new tables.

#### Out of scope for v0.77 (explicit non-goals)

  - Multi-dimensional partitions (region × date) — Dagster has, edge
    case for our target audience
  - Dynamic partitions (sensors creating runtime keys)
  - Static partitions (per-region/customer slices)
  - Unified multi-granularity view — deferred to v0.78 if requested
  - Data quality on cells — separate subsystem; integrate with Great
    Expectations or dbt tests if/when shipped
  - `partition_value` ≠ `execution_date` semantics — deferred to v0.79,
    requires SFN template change
  - Quarterly/yearly granularities — YAGNI

#### What the user sees

Hover the warning triangle:
> "Declared daily, but only 4 materializations in 30 days
> (expected 30). Check your pipeline or update the asset's
> granularity declaration."

Hover a cell with re-runs:
> "acme/orders @ 2026-05-12
>  Materialized at 20:00 UTC
>  Source: acme-daily.extract
>  Runs today: 3✓ (3 total)"

### 51. Backfill Unification — one concept, one endpoint, one bulk-run SFN (v0.78.0)

> **Deferral notes (v0.78.0 implementation):**
>
> One design element is not shipped in v0.78 and is marked as deferred:
>
> 1. **`target.type='batch'` (multi-target atomic backfill)** — rejected
>    at validation with 400 `invalid_target_type`. The engine-level
>    Map-of-Maps machinery added ~30% complexity for a use case no user
>    has yet. Re-add when a concrete demand appears; write a follow-up
>    ADR documenting the new contract. All `batch`-related sections
>    below remain as the original design — they are the starting point
>    for v0.79+ when batch lands.
>
> **`options.force` and `options.incremental` are accepted by the API
> but are no-ops in v0.78** — `force` is redundant with backfill
> semantics (a backfill by its nature bypasses scheduled-run dependency
> waits, it's a user-initiated run for a specific date); `incremental`
> was a v0.77 concept that didn't carry forward into the unified model.
> Kept in the schema so existing callers don't 400, but they don't
> change behavior. Documented in `_apply_options` defaults block.
>
> **Real implementation of skip_completed:** `_scan_completed_partitions`
> does live DDB queries to identify partitions where every expected
> task has SUCCESSFUL status. This is the only gate — SFN does not
> have a Check_If_Done state. Bypassed above PREFLIGHT_MAX_PARTITIONS=100
> to stay within API Gateway 29s timeout.
>
> **Child SFN options honored end-to-end (run_task helper):**
> `skip_tasks` (from task_subset), `_suppress_asset_event` (from
> cascade='none'), and `cascade_all` (from cascade='all') are now read
> by the run_task helper template. The plumbing chain is:
> API → bulk_backfill SFN Input → child pipeline ASL Input →
> per-task run_task invocation → Choice states that branch on the
> flags. Default branches preserve existing scheduled-run behavior
> when flags are absent. Contract tests in
> tests/sdk/test_run_task_template.py pin this behavior so future
> edits can't silently strip the support.

#### Context

Pre-v0.78, six distinct code paths perform what is conceptually the same
operation — "start a slice of pipeline work":

1. `POST /api/runs/{name}` — single manual run for one date
2. `POST /api/pipeline-backfill?name={name}` — pipeline backfill across date range
3. `POST /api/assets/backfill` — asset backfill (find producers, run them)
4. `POST /api/dags/{name}/force-trigger` — single force-trigger ignoring deps
5. `usePipelineActions.runFromHere/toHere/onlyThis` — task-level run for one date
6. Matrix cell click — `AssetBackfillModal` invocation for one date

Each path has its own handler, own UI modal, own response shape, own SFN
input format, own `triggered_by` value. They share little code. This
causes:

- **Silent inconsistencies.** Asset-backfill omits `is_backfill=true`,
  so its tasks bypass the run_task SFN's Slack-suppression check — every
  asset-backfill execution spams Slack alerters. Asset-backfill also has
  no `max_parallel` parameter — 5 consumers × 30 days = 150 simultaneous
  `start_execution` calls → ThrottlingException territory.
- **No tracking unit.** A 30-day backfill creates 30 independent SFN
  executions linked only by name pattern (`{pipeline}-{date}-backfill-
  {HHMMSS}`). Cannot pause as a group, cannot cancel pending dates, cannot
  retry only failed dates, cannot show progress (X/30).
- **No partition awareness.** All paths iterate by daily date even when
  the target asset has `granularity="weekly"` or `"monthly"` (ADR #50).
  Weekly asset over 90 days produces 90 daily executions instead of 13
  weekly ones — 86% wasted SFN transitions.
- **Naming collision in DDB.** Existing task records use a field named
  `run_id` whose value is the parent `pipeline_execution`. This was named
  for PagerDuty event grouping but semantically conflicts with the new
  "Backfill record ID" concept introduced here.
- **No cost preview.** User clicks "Backfill 90 days" and discovers
  post-factum what the AWS bill looks like. Competitors (Dagster) also
  lack this; we can do better as a serverless platform where costs are
  knowable per-operation.

#### Decision

Introduce **"Backfill"** as a first-class operation: one endpoint
(`POST /api/backfill`), one orchestrator SFN (`polyris-bulk-backfill`),
one persisted record per operation, one UI modal with seed-based pre-fill
for all six entry points.

**Why "Backfill" not "Run":** the existing `/runs` page in the UI shows
all pipeline executions (cron-driven, manual, asset-event-triggered) and
users associate the word "run" with a single execution. Renaming would
break established habits; the new bulk-operation concept gets a distinct
name. "Backfill" is also industry-standard (Dagster, dbt use
this term for the same concept).

##### Backfill = unit of work

A Backfill is `(target, partition_subset, task_subset, cascade_option,
options)`:

- **target** — what gets materialized:
  - `{type: "pipeline", name: "..."}` — direct pipeline target
  - `{type: "asset", name: "..."}` — asset target, resolved to its
    producer pipeline(s) via outlets in `pipeline_registry`
  - `{type: "batch", items: [...]}` — multiple (asset, partitions) pairs
    in one Backfill (e.g., Matrix multi-cell selection)
- **partitions** — which time slices:
  - `{start, end}` — date range, expanded per target granularity
  - `{keys: [...]}` — explicit list (used by Matrix multi-cell, retry-failed)
- **tasks** — optional task subset within the pipeline (null = all tasks).
  Used by Matrix click → producer task only; by Task Detail Modal "Run from
  Here" → subset of downstream tasks; by manual backfill with task filter.
- **cascade** — applies only when `target.type == "asset"`:
  - `auto` (default) — emit normal asset events; downstream consumers
    decide based on their AND/OR/freshness rules
  - `all` — force-trigger direct consumers regardless of rules
  - `none` — suppress asset event emission (silent re-materialization)
  - Non-transitive: only direct consumers are force-triggered under `all`.
- **options** — operational knobs:
  - `force: bool` — bypass producer's own asset-dependency checks
  - `skip_completed: bool` (default true) — skip partitions where all
    target tasks already succeeded
  - `incremental: bool` — within a partition, skip already-succeeded tasks
  - `max_parallel: int` (default 5, range 1-10) — Map concurrency
  - `allow_concurrent: bool` (default false) — allow starting partitions
    that are currently running in another backfill
  - `variables: dict` — user-defined variables passed to task input

##### One concept replaces six

Each old entry point becomes a seed configuration for the universal
`POST /api/backfill`:

| Entry point | Seed |
|---|---|
| Pipeline page → "Run" | `{target: pipeline, partitions: [today]}` |
| Pipeline page → "Backfill" | `{target: pipeline}` (user fills range) |
| Asset page → "Backfill" | `{target: asset, cascade: "auto"}` |
| Matrix click 1 cell | `{target: asset, partitions: [key], cascade: "auto"}` |
| Matrix multi-select | `{target: batch, items: [...], cascade: "auto"}` |
| Task Detail → Run from Here | `{target: pipeline, tasks: [task, ...downstream], partitions: [today]}` |
| Task Detail → Run to Here | `{tasks: [...upstream, task], partitions: [today]}` |
| Task Detail → Run Only This | `{tasks: [task], partitions: [today]}` |
| Force-trigger | `{target: pipeline, partitions: [today], force: true}` |
| Retry failed of bf-X | `{target: same as X, partitions: {keys: X.failed}, parent_backfill_id: X}` |

UI: six entry points remain as user-facing shortcuts. Backend: one
endpoint, one handler, one resolver, one execution engine.

#### Architecture

```
POST /api/backfill
       ↓
   runs.py::start_backfill
       ↓
   resolve target → producer pipeline(s)
   expand partitions per target granularity
   compute task subset
   pre-flight: skip_completed scan, allow_concurrent check
   validate (limits: soft 500, hard 1000)
   estimate cost
       ↓
   write backfill record to pipeline-tokens
   start polyris-bulk-backfill SFN
       ↓ (returns immediately with backfill_id)
   
polyris-bulk-backfill (Standard SFN):
  Initialize
    ├─ capture pipeline_dag_hash from pipeline_registry
    └─ update backfill record (status=running, hash, partition list)
  Map (MaxConcurrency = options.max_parallel)
    Iterator per partition_key:
      ├─ Check_Backfill_Canceled (DDB read backfill record)
      │     if canceled → succeed iteration (skip)
      ├─ Skip_If_Done (if skip_completed, DDB query for partition status)
      ├─ Build_Child_Input (JSONata: backfill_id, partition_key,
      │     variables, skip_tasks, is_backfill, current_date for legacy
      │     compat with run_task)
      ├─ Start_Child_SFN (Standard sync, child = pipeline's own SFN)
      ├─ Catch ThrottlingException → exponential backoff retry x3
      └─ Update_Counter (DDB UpdateItem ADD completed_count/failed_count)
  Finalize
    └─ status = completed | failed | partial | canceled (computed from counters)
```

##### Data model — `pipeline-tokens` extensions

DynamoDB is schemaless; new attributes are added sparsely:

| Attribute | Type | On row type | Purpose |
|---|---|---|---|
| `record_type` | S | All new rows | `"backfill"` or `"execution"`; sentinel for `is_internal_record` |
| `backfill_id` | S | Execution + Backfill rows | Link execution to parent Backfill (sparse on legacy/cron) |
| `partition_key` | S | All new rows | Granularity-aware partition identifier |
| `cascade_source_backfill_id` | S | Cascade-triggered executions | Lineage to upstream Backfill that emitted the asset event |
| `pipeline_dag_hash` | S | Backfill records only | DAG hash at Initialize (for redeploy detection) |

New GSI on `pipeline-tokens`: **`backfill-id-index`** (PK: `backfill_id`).
Sparse — legacy/cron executions with null `backfill_id` are not indexed.

Existing `run_id` attribute (currently equal to `pipeline_execution`, used
for PagerDuty grouping) is **renamed to `parent_execution_id`** across SFN
templates, `utils.py`, and DDB writes. This frees the semantic space for
`backfill_id` and removes the naming collision. Since no production exists,
the rename is a clean atomic change with no migration path needed.

##### Backfill record shape

```
{
  execution_name = backfill_id (e.g., "bf-a1b2c3d4")
  pipeline_name = "_polyris_bulk_backfill"  # sentinel
  record_type = "backfill"
  
  # User intent
  target_seed = {type, name | items, ...}        # original request for audit
  target_pipeline = "acme/daily_orders"          # resolved producer
  task_subset = ["extract", "transform"] | null
  partition_keys = ["2024-01-15", ...]
  cascade = "auto" | "all" | "none" | null
  options = {force, skip_completed, max_parallel, ...}
  
  # Lifecycle
  status = "pending" | "running" | "completed" | "failed" | "partial" | "canceled"
  total_partitions = 30
  completed_partitions = 18    # incremented by Map iteration
  failed_partitions = 2
  skipped_partitions = 5
  
  # Context
  pipeline_dag_hash = "a1b2c3d4"   # captured at Initialize
  started_by = "makskoval@..."     # from Cognito claim
  started_at = "2026-05-20T07:55:00Z"
  finished_at = "2026-05-20T08:12:30Z"
  parent_backfill_id = "bf-xyz789" | null   # set if this is retry-failed of another
  
  # Operational
  sfn_arn = "arn:aws:states:...:execution:polyris-bulk-backfill:bf-a1b2c3d4"
  child_executions = ["acme-daily-2024-01-15-...", ...]  # populated as they start
  ttl = epoch + 30 days
}
```

#### API contract

##### `POST /api/backfill`

Request body:

```json
{
  "target": {
    "type": "pipeline" | "asset" | "batch",
    "name": "acme/daily_orders",
    "items": [{type, name, partitions}, ...]   // for batch only
  },
  "tasks": ["transform", "load"] | null,
  "partitions": {
    "start": "2024-01-01",
    "end": "2024-01-31"
  },
  "cascade": "auto" | "all" | "none" | null,
  "options": {
    "force": false,
    "skip_completed": true,
    "incremental": false,
    "max_parallel": 5,
    "allow_concurrent": false,
    "variables": {}
  }
}
```

Response 202 Accepted:

```json
{
  "backfill_id": "bf-a1b2c3d4",
  "target_pipeline": "acme/daily_orders",
  "granularity_inferred": "daily",
  "partition_count_requested": 31,
  "partition_count_skipped_completed": 23,
  "partition_count_skipped_running": 2,
  "partition_count_to_run": 6,
  "task_subset": ["extract", "transform", "load", "publish"],
  "cascade": "auto",
  "estimated_sfn_cost_usd": 0.006,
  "estimated_duration_minutes": 4,
  "warnings": [
    "weekly_summary uses .within(hours=6) — events older than 6h will not trigger it"
  ],
  "ui_url": "/backfills/bf-a1b2c3d4"
}
```

Error responses:

| Status | Code | Cause |
|---|---|---|
| 400 | `target_not_found` | Pipeline or asset doesn't exist in registry |
| 400 | `multi_producer_asset` | Asset has >1 producer; user must pick one (returned with producer list) |
| 400 | `no_producer` | Asset has no producer pipeline (external-only) |
| 400 | `invalid_partition_format` | partition key doesn't match granularity format |
| 400 | `partition_start_violation` | requested range below asset's `partition_start` |
| 400 | `range_too_large` | >1000 partitions (hard limit) |
| 400 | `range_outside_target` | end < start, or both in future >7 days |
| 400 | `invalid_cascade_for_pipeline_target` | cascade option set when target is pipeline (cascade applies to asset only) |
| 422 | `nothing_to_run` | After skip_completed, 0 partitions remain (preview-only condition) |
| 503 | `throttled` | SFN start_execution throttled; retry with exponential backoff |

##### Other endpoints

- `GET /api/backfills` — list (filters: status, target, started_by, time range; sort: started_at desc)
- `GET /api/backfills/{id}` — detail with child executions, progress, cascade warnings
- `POST /api/backfills/{id}/cancel` — mark status=canceled in DDB; bulk-run Map's `Check_Backfill_Canceled` skips remaining iterations
- `POST /api/backfills/{id}/retry-failed` — create new backfill with `partitions.keys = failed`, `parent_backfill_id = id`

##### Deprecated endpoints (deleted, since no production)

The following are removed entirely in v0.78:
- `POST /api/runs/{name}`
- `POST /api/pipeline-backfill`
- `POST /api/assets/backfill`
- `POST /api/dags/{name}/force-trigger`

UI components that called them are also removed:
- `BackfillModal.tsx`
- `AssetBackfillModal.tsx`
- `usePipelineActions::handleBackfill`
- `useBackfillMutation`, `useAssetBackfillMutation`, `useForceTriggerMutation`, `useRunPipelineMutation`

#### Cascade semantics (overview)

Detailed rules in ADR #57. Summary here:

When `target.type == "asset"`, the endpoint:

1. Resolves asset → producer pipeline(s) via `pipeline_registry` outlets.
   If >1 producer found, returns 400 with producer list (user must pick).
2. Expands partitions per asset's granularity (ADR #58).
3. Runs producer pipeline for each partition (one child SFN per partition,
   via Map).
4. Applies cascade per producer execution success:
   - `auto`: producer emits normal asset event; downstream consumers
     evaluate their AND/OR/freshness rules and trigger (or not) accordingly.
     Late events (event_time hours behind now) fail `within()` checks —
     this is correct behavior, not a bug; surfaced in preview warnings.
   - `all`: after producer success, directly invoke notify_asset_consumers
     SFN with `force=true` flag (new field), which bypasses freshness/AND
     checks and starts downstream pipelines regardless.
   - `none`: producer execution completes but the asset event emission
     step is skipped (new flag `_suppress_asset_event=true` in execution
     input, consumed by notify_dependents).

Cascade is **non-transitive** — only direct consumers are affected.
If `asset_A → asset_B → asset_C` and Backfill on A uses `cascade=all`,
B's producer runs (force), B emits its own event normally, C's reaction
follows C's rules (whether it triggers depends on C's setup, not on
A's cascade choice).

Cascade `lineage`: cascade-triggered executions (under `auto` or `all`)
carry `cascade_source_backfill_id` in their input → stored in DDB →
visible in `/runs` row as "🔗 cascade from bf-a1b2c3d4". Click on the
link navigates to the originating Backfill detail page. Question #7 (A).

#### Edge cases — matrix

| Scenario | Behavior |
|---|---|
| Asset has 1 producer | Standard flow |
| Asset has N producers | 400 with `multi_producer_asset` + list; user picks one |
| Asset has 0 producers (external) | 400 with `no_producer`; user may use `target=batch` with `cascade=all` for event-only emission (future ADR) |
| `partition_start` is mid-bucket | Round down to bucket boundary (ADR #58); preview shows clamped range |
| Requested range below `partition_start` | 400 with `partition_start_violation` |
| Pipeline has no schedule | Cron inference defaults to "daily" with no warning |
| Pipeline schedule is ambiguous (`0 8 * * 1-5`) | Inference defaults to "daily" with explicit warning in preview |
| Pipeline outlets mixed granularities | Disallowed at deploy time (ADR #52); cannot occur at runtime |
| Concurrent backfill on same partition | Default: pre-flight skip; opt out via `allow_concurrent=true` |
| Cancel race (iteration started between cancel + check) | Up to N race-through (where N = max_parallel); documented as acceptable |
| Mid-run pipeline redeploy | `pipeline_dag_hash` captured at Initialize; if changed by run end, Backfill Detail UI shows warning banner |
| Consumer paused mid-cascade | Auto mode: events queue (existing behavior); shown in cascade summary |
| Consumer freshness gate fails | Auto mode: consumer skipped silently; surfaced in cascade summary; preview warns proactively |
| `consecutive(days=N)` partial coverage | Auto mode: consumer not triggered until N consecutive days exist; surfaced in cascade summary |
| `skip_completed=true` + 0 to_run | 422 `nothing_to_run` with body explaining why; UI shows "Nothing to do" before submit |
| Bulk-run SFN itself throttled at start | 503 `throttled`; UI retries with backoff transparently |

#### Migration

Since no production deployment exists, no backward compatibility is
preserved. All old endpoints, UI modals, hooks, and DDB field semantics
are replaced atomically in v0.78.0.

The `run_id` → `parent_execution_id` rename is a synchronized change
across:
- `sam/sfn_templates/dependency_wrapper/sfn.tpl.json` (4 sites)
- `sam/sfn_templates/helpers/run_task/sfn.tpl.json` (5 sites)
- `sam/sfn_templates/helpers/pagerduty_alerter/sfn.tpl.json` (1 site)
- `sam/sfn_templates/helpers/pagerduty_resolver/sfn.tpl.json` (1 site)
- `sam/lambdas/console_api/utils.py` (3 sites)

The new `backfill_id` field is added to the same SFN templates (sparse —
written only when execution is part of a Backfill).

Snapshot tests under `tests/snapshots/` are re-baselined as part of the
implementation; PR review verifies the diff matches the rename + addition
pattern.

#### Limits

Hard-coded constants in `sam/lambdas/console_api/constants.py::BackfillLimits`:

- `PARTITION_SOFT_LIMIT = 500` — preview shows warning, allows submit
- `PARTITION_HARD_LIMIT = 1000` — endpoint returns 400; for larger backfills,
  user must chunk
- `MAX_PARALLEL = 10` — Map state MaxConcurrency upper bound (lower for safety)
- `RECORD_TTL_DAYS = 30` — same as executions, for consistency
- `BULK_BACKFILL_SFN_TIMEOUT = 24h` — Standard SFN max wall time

#### CLI

New `polyris.run_cli` module exposes `polyris backfill` and `polyris
backfills` subcommands:

```
polyris backfill pipeline acme/daily_orders \
    --partitions 2024-01-01:2024-01-31 \
    --max-parallel 5

polyris backfill asset acme/orders \
    --partitions 2024-01:2024-03 \
    --cascade auto

polyris backfills list [--status running] [--target ...] [--user ...]
polyris backfills show bf-a1b2c3d4
polyris backfills cancel bf-a1b2c3d4
polyris backfills retry bf-a1b2c3d4
```

Thin wrapper over `/api/backfill`; AWS credentials via standard boto3
profile chain. Used by CI pipelines, cron jobs on dev machines, and
power users.

#### Why these decisions

| Decision | Rationale |
|---|---|
| Single concept "Backfill" | Eliminates 6 code paths with shared logic; reduces maintenance surface ~30%; matches industry term |
| Separate DDB table not used | Schema is sparse and clean inside `pipeline-tokens` via sentinel + `record_type`; one less migration concern; one less DAL repo file (until #4 below) |
| Separate `backfills_repo.py` DAL | Question #4 (A); different concept lifecycle, cleaner tests, future-proof for separate TTL/audit table |
| Standard SFN for bulk-backfill | Question #2; simpler than Express+watcher; cost $0.01/backfill negligible at any volume |
| Cron inference at runtime | Question #3 (B); no DSL change; fallback to daily with warning for ambiguous cron |
| `pipeline_dag_hash` captured | Question #5 (D); leverages existing `generate_dag_hash`; honest visibility of mid-run redeploys without false promise of point-in-time execution |
| `/runs` page unchanged, `/backfills/{id}` added | Question #6; preserves user habits; filter chip + lineage column gives drill-in path |
| Cascade lineage with UI | Question #7 (A); complete feature, not stubbed; uses existing infrastructure (notify_asset_consumers SFN); low LOC |
| Soft 500 / Hard 1000 | Question #8 (A); guardrail + protection; based on real payload size + reasonable cognitive limit |
| 30-day TTL | Question #9 (A); consistency with executions; audit needs older than 30 days are out of scope |
| CLI in v0.78 | Question #10 (A); commodity feature; serves CI/automation use case from launch |
| Schedule runs NOT through bulk-backfill | Question #6 implication; preserves existing direct EventBridge → SFN path; `/runs` shows them as before |
| Force-trigger collapsed into Backfill | One-day-partitions backfill with `force=true` is functionally identical; no need for separate concept |

#### Alternatives considered (rejected)

1. **Keep three separate handlers, fix bugs in-place** — addresses the 5
   immediate hardening items from v0.77.2 audit but leaves the
   no-tracking-unit problem, granularity ignorance, and code duplication.
   Doesn't unlock cancel/retry/cost-preview features.

2. **Express SFN with separate watcher** — 110× cheaper per backfill
   ($0.0001 vs $0.0091), but requires separate poller SFN, fragments
   state across two engines, and edge-cases on 5-min timeout for large
   backfills. Cost saving is meaningless at our scale.

3. **Bulk-backfill in Lambda (no SFN)** — eliminates SFN cost entirely
   but loses native cancel, concurrency control, retry, observability.
   Custom code burden offsets the saving. Also fails CLAUDE.md "SFN-first
   architecture" (memory item: async push model shelved).

4. **Rename `/runs` → `/executions`, new `/runs` for Backfills** —
   considered until rejected by user (sees existing /runs page in
   production usage). Habit break is real UX cost.

5. **"Backfill" not "Run" naming** — chosen over "Run" because (a)
   conflicts with existing UI terminology, (b) industry-standard term
   for this concept (Dagster, dbt), (c) avoids re-education
   cost for users coming from those tools.

#### Cost

Per backfill (90-day daily example):
- Bulk-backfill Standard SFN: ~365 transitions × $0.025/1000 = **$0.0091**
- DDB writes: Backfill record (1 PutItem) + 90 counter updates + 90
  child execution rows = ~$0.00015
- Lambda invocations: 1 × `/api/backfill` handler = ~$0.0000002

**Total: ~$0.01 per 90-day backfill.**

Per month (estimated 50 backfills at this size):
- Backfill operations: 50 × $0.01 = $0.50
- New GSI `backfill-id-index`: $0.15/month flat (sparse storage)
- **Total: +$0.65/month over baseline.**

For 10× volume (500 backfills/month): +$5.15/month. Cost scales linearly
and remains marginal.

#### Open questions deferred to subsequent ADRs

- ADR #52: pipeline granularity validation rules
- ADR #53: cost preview formula and update mechanism
- ADR #54: bulk-backfill SFN state machine detailed design
- ADR #55: scheduled runs (out-of-scope confirmation, not migration)
- ADR #56: backfill status model + mapping to execution statuses
- ADR #57: cascade semantics — full edge case enumeration
- ADR #58: partition keys format and range expansion algorithm

#### What the user sees

##### Triggering a backfill (asset target)

1. Asset Detail page, click "Backfill" button
2. Modal opens, pre-filled with `target=asset`, `cascade=auto`
3. User picks date range
4. Preview updates live as user changes inputs:
   - "31 partitions requested, 23 already complete, 8 to run"
   - "Estimated SFN cost: $0.0028"
   - "Estimated duration at max_parallel=5: ~3 min"
   - Cascade warnings if any consumer has `within()` or `consecutive()`
5. Click "Start Backfill"
6. Modal closes; toast: "Backfill bf-a1b2c3d4 started — 8 partitions to run"
7. Navigate to `/backfills/bf-a1b2c3d4` to watch progress live

##### Viewing a backfill in progress

Heatmap grid + table:

```
bf-a1b2c3d4   running   3/8 ✓ 1/8 ✗ 4/8 ⏳   started 2 min ago by makskoval
Target: acme/orders  Cascade: auto
Pipeline DAG hash: a1b2c3d4 (captured at start; matches current)

[Cancel]  [Retry Failed]

Partitions:
  2024-01-15  ✓ success    acme-daily-2024-01-15-...  ↗
  2024-01-16  ✓ success    acme-daily-2024-01-16-...  ↗
  2024-01-17  ✗ failed     acme-daily-2024-01-17-...  ↗
  2024-01-18  ⏳ running   acme-daily-2024-01-18-...  ↗
  ...

Cascade summary (so far):
  Asset events emitted: 3
  Consumer triggers:
    weekly_summary — 0 triggered (waiting for full week)
    monthly_report — 0 triggered (waiting for full month)
```

##### Viewing executions with cascade lineage

In `/runs` row:

```
weekly_summary    success    2 min ago    🔗 cascade from bf-a1b2c3d4
```

Click on `bf-a1b2c3d4` link → navigate to Backfill detail.

#### Implementation order

Phase order from Discovery report (file-by-file walkthrough in
`docs/redesign/run/DISCOVERY.md`):

1. Resolve naming collision: rename `run_id` → `parent_execution_id`
   in SFN templates + utils.py + tests. **Standalone commit, mergeable
   independently**. Confirms no production breakage before adding new
   concept.
2. SAM template: add `backfill-id-index` GSI, new attributes,
   `BulkBackfillSfn` resource. Deploy + smoke test.
3. SDK: `polyris/partitions.py`, `polyris/granularity.py`. Unit tests.
4. Backend: `routes/backfill.py` (replacing deleted `routes/backfill.py`
   for the old endpoint), `dal/backfills_repo.py`.
5. Bulk-backfill SFN template.
6. UI: `BackfillModal.tsx`, `BackfillDetailPage.tsx`, `/backfills/{id}`
   route, hooks consolidation.
7. Entry points wiring: Pipeline page, Asset page, Matrix, Task Detail
   Modal.
8. Tests: integration suite, snapshot re-baseline.
9. CLI: `polyris.run_cli`.
10. Docs: ADRs #52-#58 written iteratively; user-facing docs rewrite.
11. v0.78.0 release.

### 52. Pipeline Granularity — runtime cron cadence inference, no DSL change (v0.78.0)

#### Context

Backfill (ADR #51) needs to know how to expand a date range into
partition keys for a `target.type == "pipeline"` request. For asset
targets, granularity is declared explicitly (`Asset(granularity="weekly")`,
ADR #50). For pipeline targets, no equivalent declaration exists.

Pre-v0.78 backfill code always expands ranges as daily — pipeline
`weekly_summary` (cron `0 8 * * MON`) backfilled over 30 days creates 30
daily executions, of which only 4 fall on Mondays. The other 26 execute
on days the pipeline normally would not run, producing redundant work
and silent data inconsistency.

Three design options were considered:

1. **Add `@dag(granularity="weekly")` field to DSL** — explicit declaration
   matching `Asset.granularity`. Symmetric, but requires user education
   and a new DSL field with validation logic.
2. **Strict cron-to-outlet validation at deploy** — derive granularity
   from outlets, error if cron doesn't match (e.g., daily cron + weekly
   outlet → deploy fails). Catches inconsistencies early.
3. **Runtime inference with daily fallback** — backend inspects
   `pipeline.schedule` (cron string) at backfill start time, infers
   cadence, falls back to daily for ambiguous cases with warning surfaced
   in preview.

Option 3 chosen (Question #3 (B)). Rationale: no DSL change burden, no
deploy-time gating, fallback is safe default, ambiguity surfaced
transparently in UI.

#### Decision

Add `polyris/granularity.py` with `infer_cron_cadence(cron_string)`
function. Called from `routes/backfill.py::start_backfill` when target is
a pipeline. Result feeds `polyris.partitions.expand_range`.

##### Inference rules

```python
def infer_cron_cadence(cron: str) -> Optional[str]:
    """
    Returns one of: "hourly", "daily", "weekly", "monthly", or None
    (ambiguous → caller falls back to "daily" with warning).
    
    Standard 5-field cron: minute hour day-of-month month day-of-week
    """
    # Pure daily — every day, fixed hour
    "0 8 * * *"           → "daily"
    "30 0 * * *"          → "daily"
    
    # Hourly — every hour or sub-hourly fixed
    "0 * * * *"           → "hourly"
    "*/15 * * * *"        → "hourly"  # quarter-hourly counted as hourly bucket
    
    # Weekly — fixed day of week, no day-of-month
    "0 8 * * MON"         → "weekly"
    "0 8 * * 1"           → "weekly"  # numeric form
    "0 8 * * SUN"         → "weekly"
    
    # Monthly — fixed day of month, no day-of-week
    "0 8 1 * *"           → "monthly"
    "0 8 15 * *"          → "monthly"
    
    # Ambiguous cases (return None, caller falls back daily with warning)
    "0 8 * * 1-5"         → None  # weekdays only — 5 per week, neither daily nor weekly cleanly
    "0 8 1,15 * *"        → None  # twice monthly — no clean granularity
    "0 8 * 1,4,7,10 *"    → None  # quarterly — no granularity bucket for it
    "*/5 9-17 * * 1-5"    → None  # business-hours frequency — neither hourly nor daily
    
    # Manual-only pipeline
    None or ""            → "daily"  # default for no schedule
```

##### Algorithm

```python
def infer_cron_cadence(cron: Optional[str]) -> Optional[str]:
    if not cron or not cron.strip():
        return "daily"  # manual-only pipelines default to daily for backfill
    
    parts = cron.split()
    if len(parts) != 5:
        return None  # invalid cron format → ambiguous
    
    minute, hour, day_month, month, day_week = parts
    
    # Hourly: hour field is wildcard or wildcard-step
    if hour in ("*", "*/1") or _is_wildcard_step(hour):
        return "hourly"
    
    # If hour is fixed (single value), continue checking date fields
    if not _is_single_value(hour):
        return None  # multiple hours → ambiguous (every 6h, etc.)
    
    # Daily: both day fields wildcard, hour fixed
    if day_month == "*" and day_week == "*":
        return "daily"
    
    # Weekly: day-of-week fixed (named or numeric), day-of-month wildcard
    if day_month == "*" and _is_single_value(day_week):
        return "weekly"
    
    # Monthly: day-of-month fixed, day-of-week wildcard
    if _is_single_value(day_month) and day_week == "*":
        return "monthly"
    
    return None  # everything else is ambiguous
```

Helper predicates:

- `_is_single_value(field)` — `True` if field is a single number, named
  day, or zero — `False` for ranges, lists, steps, wildcards.
- `_is_wildcard_step(field)` — `True` for `*/N` patterns.

Implementation uses regex matching, ~80 LOC including helpers and named
day translation (MON→1, TUE→2, etc.). No external library dependency.

#### Integration with backfill

`routes/backfill.py::start_backfill` flow when `target.type == "pipeline"`:

```python
pipeline = pipelines_repo.get(target.name)
schedule = pipeline.get("schedule")  # cron string from registry

inferred = infer_cron_cadence(schedule)

if inferred is None:
    granularity = "daily"
    warnings.append({
        "code": "cron_ambiguous",
        "message": (
            f"Cannot reliably infer cadence from cron '{schedule}'. "
            f"Defaulting to daily. {expected_count} partitions will be "
            f"created in the requested range; some may execute on days "
            f"this pipeline doesn't normally run."
        )
    })
else:
    granularity = inferred

partition_keys = expand_range(start, end, granularity, ...)
```

For asset target, granularity comes from `asset.granularity` (no
inference needed). For batch target, each item resolves independently.

#### Why not strict validation at deploy

The rejected option 2 ("strict cron-to-outlet validation") was attractive
because it catches inconsistencies early. Rejected because:

1. **Many cron patterns are legitimately ambiguous.** `0 8 * * 1-5`
   (weekdays only) is a real pattern users write. It's neither daily
   (only 5 of 7 days) nor weekly (5 per week). Strict validation forces
   user to either pick a misleading granularity or refactor their cron.
   Neither is good UX.

2. **No outlets is a valid pipeline.** Some pipelines are pure side-effect
   (S3 cleanup, config sync, alert delivery) and declare no assets. Strict
   validation against outlets has nothing to check.

3. **Backfill UX wants to be helpful, not blocking.** A user kicking off
   a backfill for a weekdays-only pipeline should get reasonable behavior
   (daily expansion with warning) — not "deploy failed because your cron
   is non-standard."

4. **Decoupling cron from granularity is fine in practice.** Asset
   granularity (ADR #50) is the source of truth for *data shape*. Pipeline
   cadence is just *when triggers fire*. Mismatch between them is a soft
   issue that drift detection (ADR #50) catches at runtime, not a deploy
   blocker.

#### Why not @dag(granularity=...) override

The rejected option 1 was attractive because it gives explicit user
control. Rejected because:

1. **Adds DSL surface area.** New field, new validation, new docs,
   new test coverage. CLAUDE.md #2 (no duplication) — `Asset.granularity`
   already exists; pipeline-level field is parallel concept.

2. **Not needed for the 80% case.** Standard daily/weekly/monthly cron
   patterns infer correctly. Only ambiguous crons need disambiguation —
   handled by warning rather than mandatory declaration.

3. **Inference works at runtime when needed.** No need to materialize
   granularity into the registry until backfill is requested. Lazy and
   cheap.

If real cases emerge where users want to override the inference (e.g.,
"my weekdays cron should be treated as daily for backfill purposes"), a
narrow `@dag(backfill_granularity="daily")` field can be added in a
follow-up ADR without breaking changes — it would only suppress the
warning.

#### Edge cases

| Cron | Inferred | Notes |
|---|---|---|
| `0 8 * * *` | daily | Standard daily |
| `0 */6 * * *` | hourly | Every 6 hours — bucketed as hourly |
| `*/15 9-17 * * 1-5` | None → daily + warn | Multiple ambiguities (range hours, weekdays) |
| `0 8 * * MON` | weekly | Day-of-week fixed |
| `0 8 * * MON,WED,FRI` | None → daily + warn | Multiple days of week |
| `0 8 1 * *` | monthly | Day 1 of month |
| `0 8 L * *` | monthly | "L" = last day; treated as monthly |
| `0 0 ? * MON#1` | None → daily + warn | First Monday of month — quarterly-ish, no clean bucket |
| `@daily` (shorthand) | daily | Equivalent to `0 0 * * *` |
| `@weekly` | weekly | Sunday at midnight |
| `@monthly` | monthly | First of month at midnight |
| `rate(1 hour)` | hourly | AWS EventBridge rate expression |
| `rate(1 day)` | daily | AWS EventBridge rate expression |
| Empty / null | daily | Manual-only pipeline, daily default |
| Invalid format | None → daily + warn | Malformed cron, can't parse |

EventBridge `rate(N units)` expressions are mapped via separate helper
since they're not standard cron. `@shorthand` patterns are mapped via
lookup table.

The implementation handles only what's listed above. Pathological cases
("first Monday of every third month") fall to `None` → daily + warn —
not pretending to support what we don't.

#### Update to pipeline_registry — none

The original plan was to write `granularity` to `pipeline_registry` at
deploy time. Dropped because inference is runtime-cheap (~1 µs per call,
no AWS calls needed) and avoiding deploy-time write keeps `polyris-deploy`
simpler.

If profiling later shows inference is hot, cache result in `pipeline_registry`
as denormalization — easy follow-up.

#### Testing

`tests/sdk/test_granularity.py` (new, ~150 LOC):

- 20+ cases from the edge cases table above
- Test that inference doesn't crash on malformed input
- Test that warnings surface correctly in `/api/backfill` response
- Test fallback to daily for None

No drift test needed — `infer_cron_cadence` is pure function, no shared
schema with SFN templates.

#### Out of scope

- Quarterly granularity (every 3 months, named explicitly via cron) —
  current 4-granularity set (hourly/daily/weekly/monthly) from ADR #50
  is closed. If quarterly use case emerges, extension goes through ADR
  amendment.
- AWS Schedule expressions beyond `rate()` and standard cron — out of
  scope; if user uses fancy EventBridge syntax they get daily + warn.
- Cron disambiguation UI (helper to suggest "did you mean weekly?") —
  out of scope for v0.78.

#### Cost

Pure runtime function call, no infrastructure. Zero monthly cost added.


### 53. Cost Preview Methodology — formula, sources, accuracy disclaimer (v0.78.0)

#### Context

ADR #51 introduces backfill cost preview in `POST /api/backfill` response
(`estimated_sfn_cost_usd`) and in the UI before user submits the
operation. This requires a methodology — what's included, what's excluded,
how accurate, and how surfaced.

Competing orchestrators (Dagster) don't preview costs; they don't have to,
since their pricing is hosting-based (you pay for the orchestrator
cluster regardless of operations). polyris is serverless — every
operation costs incremental dollars — so cost preview is meaningful and
adds real value as a differentiator.

#### Decision

Cost preview is **best-effort estimate**, surfaced as a single dollar
value in the API response and a one-line summary in the UI. The
methodology covers AWS Step Functions transitions for the bulk-backfill
SFN itself and the spawned child pipeline executions. Lambda invocations
and DynamoDB writes are excluded — they fall under AWS Free Tier for
realistic volumes and would noise the estimate.

Estimate is computed in `polyris/partitions.py::PartitionRange.cost_estimate()`
and called from the request handler before responding.

##### Formula

```
sfn_cost_usd = (bulk_backfill_transitions + child_execution_transitions) 
             * STANDARD_SFN_PRICE_PER_TRANSITION

where:
  STANDARD_SFN_PRICE_PER_TRANSITION = 0.000025  # $0.025 per 1000

  bulk_backfill_transitions = (
      INITIALIZE_TRANSITIONS               # 5 (constant)
    + N_PARTITIONS * PER_ITERATION         # 4 per Map iteration
    + FINALIZE_TRANSITIONS                 # 3 (constant)
  )

  child_execution_transitions = (
      N_PARTITIONS * AVG_TASKS_PER_PIPELINE * EXPRESS_PER_TASK
    + N_PARTITIONS * STANDARD_WRAPPER_OVERHEAD
  )
  
  Where:
    EXPRESS_PER_TASK = 0  # Express SFN per-task is per-request, not transition
    STANDARD_WRAPPER_OVERHEAD = ~50 transitions per pipeline execution
                                (dependency_wrapper invocations, state changes)
```

Specifically per partition for a typical 4-task pipeline:
- Bulk-backfill iteration: 4 transitions
- Child pipeline (Standard SFN): ~50 transitions (Initialize, 4× Wrap+Run+Resolve+Notify, Finalize)
- Total: ~54 transitions per partition

For 90 partitions: 5 + 90×4 + 3 + 90×50 = 4868 transitions × $0.025/1000 =
**$0.122**.

Of that:
- Bulk-backfill contribution: $0.009 (the overhead we're adding)
- Child pipeline contribution: $0.113 (the work itself, would happen
  regardless of bulk-backfill)

##### What's included vs excluded

**Included** (surfaced to user):
- All AWS Step Functions Standard transitions across bulk-backfill +
  child pipelines for the partitions to be run

**Excluded** (free tier or negligible):
- Lambda invocations (~$0.0000002 per invocation; 1M free/month)
- DynamoDB writes (~$1.25 per million write requests; backfill of 90
  partitions = ~400 writes = $0.0005)
- DynamoDB reads (pre-flight skip_completed scan = ~$0.0001)
- CloudWatch Logs (~$0.50/GB ingested; backfill logs are KB-scale)
- S3 storage for task outputs (user-specific, can't estimate generically)
- Athena/Glue costs for tasks that query data (user-specific)
- Data egress (cross-region, NAT gateway, etc.)
- IAM/STS calls

Excluded items are listed in `/backfills/{id}` Detail Page in a footnote:
"Cost estimate covers Step Functions only. Lambda, DynamoDB, S3, and
data processing costs are not included."

##### Accuracy

The estimate is **±20% accurate** for typical backfills, with these
sources of error:

1. **AVG_TASKS_PER_PIPELINE assumption.** Formula uses the pipeline's
   actual task count from `pipeline_registry`. Accurate per pipeline.
2. **STANDARD_WRAPPER_OVERHEAD constant.** Calibrated empirically against
   smoke pipelines (5 measured runs, mean 48 transitions, σ 4). May
   underestimate for pipelines with many conditional branches or asset
   subscriptions.
3. **Tasks that skip via `skip_tasks` parameter.** Backfill input includes
   `skip_tasks`, but wrapper still transitions through the skip-decision
   states. Cost is paid for skipped tasks (~5 transitions per skipped
   task instead of 50).
4. **Retries.** If a task fails and retries (per-task retry policy),
   actual transitions exceed estimate by retry count × 50. Estimate does
   not account for failure-driven retries.
5. **Mid-run redeploy** (ADR #51, Question #5/D). If pipeline changes
   structure mid-backfill, transitions per partition may differ. Surfaced
   via DAG hash warning, not in cost.

Estimate is labeled "Estimated" in UI and copy reads "approximate based
on current pipeline structure." Users with cost-sensitive workflows are
directed to AWS Cost Explorer for precise post-hoc accounting.

##### What the user sees

In modal preview:

```
Estimated SFN cost:        $0.012
Estimated duration:        ~3 min at max_parallel=5
```

Hover tooltip on "Estimated SFN cost":
> "Covers Step Functions Standard transitions for bulk-backfill plus
>  child pipeline executions. Excludes Lambda, DynamoDB, S3, and data
>  processing (Athena, Glue) costs. Estimate is approximate based on
>  current pipeline structure."

In `/backfills/{id}` Detail Page footer:

```
Estimated: $0.012   |   Track actual: AWS Cost Explorer
```

No actual cost is computed post-hoc — that would require parsing
CloudWatch metrics or Cost Explorer API per backfill, an entire
subsystem. Out of scope for v0.78.

##### Cost preview update flow

The preview updates as user changes inputs in the modal:

- Date range changes → partition count changes → cost updates
- Skip completed toggle → partition count changes → cost updates
- Max parallel changes → duration changes (cost unchanged, since total
  work is identical)
- Task subset changes → child transitions per partition change → cost
  updates

Frontend `RunModal.tsx` calls `POST /api/backfill?preview=true` (idempotent
query param that runs pre-flight only, returns same response shape but
does not start the backfill) on debounced input change (300ms). API
returns within 500ms typical (DDB scan for skip_completed is the
bottleneck).

##### Alternative considered: real-time cost via AWS Pricing API

Rejected. AWS Pricing API queries are slow (~2s), the prices for
Step Functions Standard are stable enough that hardcoding the
$0.025/1000 constant is fine. If AWS changes pricing, we update the
constant in `polyris/partitions.py` — a single source location.

##### Out of scope

- Real-time actual cost tracking (would require periodic Cost Explorer
  polling per backfill) — out of scope; AWS Cost Explorer handles this
  natively
- Cost trends across many backfills ("you spent $X on backfills this
  month") — out of scope; out-of-band reporting
- Multi-region cost differences (SFN pricing is uniform across most
  regions but not all) — out of scope; assume current region pricing
- Reserved capacity / savings plans — out of scope; not applicable to
  SFN

### 54. Bulk-Backfill SFN — Standard, Map-based, fire-and-wait architecture (v0.78.0)

#### Context

ADR #51 calls for a `polyris-bulk-backfill` SFN that orchestrates one
"slice of work" per partition for a backfill. This ADR specifies its
internal structure: state machine layout, JSONata expressions, IAM
permissions, error handling, cancel mechanism, and operational
characteristics.

#### Decision

**Type:** Standard SFN (not Express).
**Pattern:** Single Map state with synchronous child invocations
(`.sync` integration pattern).
**State count:** ~12 states (Initialize, Map iterator with 5 sub-states,
Finalize, plus error paths).
**Naming:** `polyris-bulk-backfill` (one SFN, namespace-scoped via SAM
template substitution).
**Execution naming:** `{backfill_id}` (e.g., `bf-a1b2c3d4`); matches the
DDB Backfill record's `execution_name`/`backfill_id` field for 1:1
correspondence.

##### Why Standard SFN, not Express

Question #2 (Standard).

| Factor | Standard | Express |
|---|---|---|
| Per-iteration cost | ~$0.0001 | ~$0.000001 |
| Total cost for 90-partition backfill | $0.009 | $0.0001 |
| Max execution time | 1 year | 5 minutes |
| `.sync` integration support | Native | Limited |
| AWS Console visibility | Full history | 24h retention |
| State logging | CloudWatch Logs | EXPRESS log only |

Express is 100× cheaper but caps at 5 minutes. For our use case
(backfills with `.sync` waiting on child pipelines that may take hours
each), Express would require a separate watcher SFN to poll completion,
adding architectural complexity. The 1¢ savings per backfill is not
worth the complexity. Standard chosen.

##### State machine layout

```
Initialize (Task: DynamoDB.PutItem)
   ↓ writes backfill record with status=running, dag_hash from pipeline_registry
   
LoadPipelineDagHash (Task: DynamoDB.GetItem)
   ↓ fetches current pipeline registry record to capture dag_hash
   
UpdateBackfillWithHash (Task: DynamoDB.UpdateItem)
   ↓ writes pipeline_dag_hash to backfill record
   
Map (Iterator on partition_keys, MaxConcurrency=options.max_parallel)
   ↓ for each partition:
   
   CheckBackfillCanceled (Task: DynamoDB.GetItem)
     ↓ reads backfill record
   
   IsCanceledChoice (Choice)
     ├─ if status==canceled → SkipPartition (Succeed iteration)
     └─ continue →
   
   SkipIfDone (Choice)  [only if skip_completed=true]
     ├─ if all target tasks succeeded → SkipPartition + IncrementSkipped
     └─ continue →
   
   StartChildSFN (Task: StepFunctions.StartExecution.sync)
     ↓ starts child pipeline SFN, waits for completion
     Catch: ThrottlingException → BackoffRetry (Wait 2s → retry x3)
     Catch: All other → IncrementFailed
   
   ChildSucceededChoice (Choice)
     ├─ if status==SUCCEEDED → IncrementCompleted
     └─ else → IncrementFailed
   
   IncrementCompleted | IncrementFailed | IncrementSkipped 
     (Task: DynamoDB.UpdateItem with ADD expression)
   
End of Map iteration.

Finalize (Task: DynamoDB.UpdateItem)
   ↓ reads counters, sets final status based on completed/failed/skipped:
     - if failed > 0 and completed > 0: status = "partial"
     - if failed > 0 and completed == 0: status = "failed"
     - if canceled at any point: status = "canceled"
     - else: status = "completed"
   ↓ writes finished_at timestamp
```

Total states: 3 init + 6 iterator + 1 finalize = 10 unique states.

##### Map state configuration

```json
"BulkBackfillMap": {
  "Type": "Map",
  "ItemsPath": "$.partition_keys",
  "MaxConcurrencyPath": "$.options.max_parallel",
  "ItemSelector": {
    "backfill_id.$": "$.backfill_id",
    "partition_key.$": "$$.Map.Item.Value",
    "target_pipeline.$": "$.target_pipeline",
    "task_subset.$": "$.task_subset",
    "options.$": "$.options",
    "cascade.$": "$.cascade",
    "variables.$": "$.options.variables"
  },
  "ItemProcessor": { ... },
  "ResultSelector": {
    "iterations_complete.$": "$$.Map.Length"
  },
  "Next": "Finalize"
}
```

`MaxConcurrencyPath` is dynamic — reads from input, allowing user to set
max_parallel per backfill (1-10 range, validated by handler).

##### Child SFN invocation (StartExecution.sync)

```json
"StartChildSFN": {
  "Type": "Task",
  "Resource": "arn:aws:states:::states:startExecution.sync:2",
  "Parameters": {
    "StateMachineArn.$": "$.target_pipeline_sfn_arn",
    "Name.$": "States.Format('{}-{}', $.target_pipeline_name, $.partition_key)",
    "Input": {
      "backfill_id.$": "$.backfill_id",
      "partition_key.$": "$.partition_key",
      "current_date.$": "$.partition_key",   // legacy compat for run_task
      "is_backfill": true,
      "skip_tasks.$": "$.skip_tasks_computed",
      "variables.$": "$.variables"
    }
  },
  "TimeoutSeconds": 86400,
  "Retry": [
    {
      "ErrorEquals": ["States.TaskFailed"],
      "ErrorRegex": ".*ThrottlingException.*",
      "IntervalSeconds": 2,
      "MaxAttempts": 3,
      "BackoffRate": 2.0
    }
  ],
  "Catch": [
    {
      "ErrorEquals": ["States.ALL"],
      "Next": "IncrementFailed",
      "ResultPath": "$.error"
    }
  ],
  "Next": "ChildSucceededChoice"
}
```

`startExecution.sync:2` waits for child to complete and returns
`Output.Status` field reflecting child's final state (SUCCEEDED / FAILED).
`TimeoutSeconds: 86400` (24h) covers worst-case long-running pipelines.

For non-daily granularity, `current_date` in the input is set to the
**daily anchor** of the partition (e.g., weekly partition `2024-W03` →
Monday of that week, `2024-01-15`). This preserves backward compatibility
with `run_task` SFN template's `Prepare_Task_Input` state which expects
daily YYYY-MM-DD. The `partition_key` is the canonical identifier for
tooling/UI/DDB; `current_date` is the runtime anchor for JSONata date math
inside tasks. ADR #58 specifies the translation.

##### Cancel mechanism

Question #1 (A) — cancel races up to N partitions (where N = max_parallel)
through despite cancel signal. Acceptable. Implementation:

```json
"CheckBackfillCanceled": {
  "Type": "Task",
  "Resource": "arn:aws:states:::dynamodb:getItem",
  "Parameters": {
    "TableName": "${PipelineTokensTable}",
    "Key": {
      "execution_name": {"S.$": "$.backfill_id"},
      "pipeline_name": {"S": "_polyris_bulk_backfill"}
    },
    "ConsistentRead": true
  },
  "ResultSelector": {
    "status.$": "$.Item.status.S"
  },
  "ResultPath": "$.backfill_status",
  "Next": "IsCanceledChoice"
}
```

The `ConsistentRead: true` flag ensures cancel signal is seen within
the AWS replication window (single-digit milliseconds). Map iterations
that have already started their `StartChildSFN` cannot be canceled (they
complete naturally); only iterations that haven't reached the check yet
will skip.

Cancel handler in `routes/backfill.py::cancel_backfill`:

```python
def cancel_backfill(backfill_id, event):
    repo.update_item(
        Key={execution_name=backfill_id, pipeline_name=SENTINEL},
        UpdateExpression="SET #status = :canceled",
        ConditionExpression="#status IN (:pending, :running)",
        ExpressionAttributeValues={
            ":canceled": "canceled",
            ":pending": "pending",
            ":running": "running",
        },
        ExpressionAttributeNames={"#status": "status"}
    )
    return {"backfill_id": backfill_id, "status": "canceled"}
```

No `sfn.stop_execution()` call. The bulk-backfill SFN continues running
through its remaining states (Finalize will set status to "canceled" if
that's already in DDB), but new partitions stop starting. Already-started
child pipelines are not interrupted.

Why not `sfn.stop_execution()`? It would mark the bulk-backfill execution
as ABORTED, but in-flight child SFNs would still complete (no cascading
stop), DDB record would not reflect the cancel cleanly, and the Map's
counter updates would be inconsistent. Cooperative cancel via DDB flag
is cleaner.

##### IAM permissions

The bulk-backfill SFN's execution role needs:

1. **DynamoDB on PipelineTokensTable** — `PutItem`, `GetItem`, `UpdateItem`
   for backfill record + child execution rows (for skip_completed check).
2. **DynamoDB on PipelineRegistryTable** — `GetItem` for fetching
   pipeline DAG hash and target_pipeline_sfn_arn.
3. **States:StartExecution.sync** on **all pipeline SFNs in the same
   namespace** — wildcard `arn:aws:states:*:*:stateMachine:{Namespace}-{Stage}-*`.
4. **CloudWatch Logs** — write permissions for SFN logging.

The OrchestrationRole (existing role used by other helper SFNs) already
has scopes 1, 3, 4 via wildcards. Scope 2 needs explicit addition.

##### Error handling

| Failure mode | Behavior |
|---|---|
| DDB Initialize fails | Bulk-backfill execution fails immediately; no backfill record exists → user sees 500 from API; retry by user |
| DDB GetItem on cancel check fails | Iteration retries 3x with backoff; if persistent, iteration fails-fast with logged error; counter increments failed |
| StartChildSFN throttled | Retried 3x with exponential backoff (2s, 4s, 8s); if still throttled, iteration counts as failed; user retries via UI |
| Child SFN fails | Iteration's `Catch` block sets ResultPath, increments failed_count, continues to next partition |
| Child SFN times out (24h) | Treated as failure |
| Finalize fails | Bulk-backfill state machine fails; backfill record stuck in "running" status — handled by reconciliation cron (out of scope, would be ADR-future) |

##### Observability

- **AWS Console:** full execution history visible per backfill. Map
  iterations show as fan-out diagram.
- **CloudWatch Logs:** all state transitions logged. Searchable by
  `backfill_id`.
- **CloudWatch Metrics:** `ExecutionsSucceeded`, `ExecutionsFailed`,
  `ExecutionTime` per state machine — standard SFN metrics.
- **X-Ray tracing:** enabled (matches existing helper SFNs); shows
  bulk-backfill → child pipeline call graph.
- **API Detail page:** mirrors above in friendly UI, updated via
  3-second polling against `/api/backfills/{id}`.

##### Performance characteristics

Per Map iteration (excluding child SFN runtime):
- CheckBackfillCanceled DDB read: ~5ms
- SkipIfDone DDB query: ~10ms (if enabled)
- StartChildSFN: ~20ms to start (then synchronous wait for child)
- IncrementCounter DDB write: ~10ms

Iteration overhead: ~45ms. For 90-partition backfill at max_parallel=5,
effective serialization is 90/5 × 45ms = ~800ms total overhead beyond
child pipeline runtime.

Map MaxConcurrency=10 (hard upper) prevents AWS-side throttling: SFN
StartExecution has a 1000/s account-wide rate limit and 200 burst. With
10 concurrent + ~45ms iteration overhead, peak rate is ~220/s — safely
under burst limit.

##### Limits enforcement

`routes/backfill.py::start_backfill` enforces limits before invoking SFN:

- `len(partition_keys) > PARTITION_HARD_LIMIT` → 400 `range_too_large`
- `len(partition_keys) > PARTITION_SOFT_LIMIT` → append warning in response, allow
- `options.max_parallel not in 1..MAX_PARALLEL` → 400 invalid input

SFN itself does not re-validate (trust handler). Defense-in-depth at
schema level: SAM template's `BulkBackfillSfn` resource has
`Definition.Parameters` validated by cfn-lint.

##### Why one Map state, not multiple

Considered alternative: split partitions into batches, one Map per batch
(Standard SFN supports up to 25 distributed Map iterations per execution).
Rejected. One Map is simpler, AWS handles concurrency natively via
MaxConcurrency, and we never expect to exceed 1000 iterations (hard
limit).

##### Why fire-and-wait, not fire-and-forget

Considered alternative: bulk-backfill starts all child SFNs via async
`StartExecution` (no `.sync`), then polls completion via separate Express
SFN watcher. Rejected because:

1. **Complexity.** Two SFNs to maintain, two failure modes.
2. **Counter accuracy.** Fire-and-forget requires the watcher to track
   completion and update counters; race conditions on DDB writes possible.
3. **Cost saving negligible.** ~$0.001 per backfill not worth the
   architectural cost.

`.sync` pattern keeps state machine self-contained and intuitive in
AWS Console.

##### Migration

Since no production exists (ADR #51), bulk-backfill SFN is a new resource
added via SAM template diff. No existing infrastructure changes.

Smoke test plan:
1. Deploy SAM update with new resource to test environment
2. Run `polyris backfill pipeline acme/daily_orders --partitions
   2026-05-01:2026-05-03` (3 partitions)
3. Verify in AWS Console:
   - bulk-backfill execution visible, Map shows 3 iterations
   - 3 child pipeline executions created
   - DDB Backfill record updates progressively
4. Cancel mid-run, verify pending iterations skip
5. Trigger backfill with one task forced to fail, verify retry-failed
   creates new backfill with only failed partition

##### Open questions deferred

- Dead Letter Queue for bulk-backfill failures — out of scope; CloudWatch
  Alarms on `ExecutionsFailed` metric is sufficient observability for v0.78
- Backfill of backfill (running backfill that re-runs failed of another)
  — supported via `parent_backfill_id` field, no special SFN handling
  needed; ADR #51 covers this in retry-failed flow

#### Cost

Per backfill (covered in ADR #53):
- 90-partition backfill: ~$0.009 of bulk-backfill SFN transitions
- Child pipelines: separate cost, would happen for any execution path

Monthly fixed:
- Backfill-id-index GSI: $0.15/month (sparse, ~10K rows including legacy)
- bulk-backfill log group: ~$0.05/month at expected log volume
- Total: ~$0.20/month flat baseline


### 55. Scheduled Pipeline Runs — direct EventBridge → SFN, not routed through bulk-backfill (v0.78.0)

#### Context

EventBridge cron rules currently invoke pipeline SFNs directly. The
v0.78 redesign asks: should scheduled (cron-triggered) executions also
create Backfill records, routing through `polyris-bulk-backfill`?

Question #6 was discussed in detail. Conclusion (from user feedback):
preserve existing `/runs` UI which lists all executions including cron;
add `/backfills/{id}` as a new drill-in for bulk operations only.
Scheduled runs are **not** Backfills.

#### Decision

EventBridge → SFN direct path remains unchanged. Scheduled pipeline
executions create regular `pipeline-tokens` rows with `record_type` =
absent (or "execution" by convention; UI treats absence as execution).
They do **not** have `backfill_id`. They do **not** appear in
`/api/backfills` list. They appear in `/runs` page as before.

##### Rationale

1. **UI habit preservation.** Existing `/runs` page is the canonical
   "what ran today" view. Users associate the URL with this data. Moving
   cron-triggered executions out of `/runs` (e.g., to `/executions`)
   would break habits.
2. **Cron + manual + asset-event flows are conceptually "executions",
   not "backfills".** Each is a single materialization. Backfill is
   explicitly a bulk operation (1+ partitions intentionally batched).
   Conflating them removes the value of distinct concepts.
3. **No code path needs unification.** Cron triggering is one
   EventBridge → SFN call. Bulk-backfill is a Map of N such calls.
   Wrapping a single call in a bulk-backfill SFN would add ~$0.01 per
   scheduled run × 600 scheduled/month × 20 pipelines = ~$120/month
   overhead for no functional gain.
4. **Lineage from Backfill cascade is preserved differently.** When a
   Backfill triggers downstream consumers via cascade=auto/all, those
   consumer executions get `cascade_source_backfill_id` (ADR #51,
   Question #7/A). This connects scheduled-like executions (event-triggered)
   back to their source bulk-backfill without making them Backfills
   themselves.

##### What's deferred (not done in v0.78)

If at some point we want full symmetry — every execution traces back to
a Backfill record — that's a future ADR. Not now. Concrete reasons:

- /runs page would explode in volume (one Backfill row per cron run);
  filtering and pagination behavior would need redesign
- Cancel/retry-failed UI on scheduled-source backfills is mostly
  meaningless (cron will retrigger tomorrow anyway)
- Manual one-day runs (today's `useRunPipelineMutation` → maps to
  Backfill `partitions=[today]`) are the explicit case where a user
  initiated a single execution — they get a Backfill record. Scheduled
  is by definition not user-initiated.

##### What `/runs` shows

| Source | record_type | backfill_id | Shown in /runs |
|---|---|---|---|
| Cron trigger | absent | absent | Yes (as today) |
| Manual one-day run | absent | bf-X (its own Backfill) | Yes (with link to /backfills/bf-X) |
| Manual backfill (date range) | absent | bf-X | Yes (each partition row with same bf-X) |
| Asset event trigger (org→consumer) | absent | absent | Yes |
| Cascade-triggered (from Backfill) | absent | absent + cascade_source_backfill_id=bf-X | Yes (with link to /backfills/bf-X for cascade source) |
| Force-trigger | absent | bf-X (single-day Backfill) | Yes |
| Backfill record (sentinel) | "backfill" | bf-X (==execution_name) | **Excluded** from /runs (filtered out by record_type) |

UI filter on /runs: `WHERE record_type != "backfill"` or absent. This
excludes only the Backfill summary rows; all execution rows visible.

UI filter chip "Backfill" allows quick narrowing: shows only executions
with non-null `backfill_id`.

##### What `/backfills/{id}` Detail shows

Backfill drill-in shows only that Backfill's execution rows (filtered by
`backfill_id == id`), plus cascade-triggered executions (filtered by
`cascade_source_backfill_id == id`). Cron-triggered or other unrelated
executions are not shown.

#### Migration

No migration. Existing EventBridge configuration unchanged. Existing
pipeline SFN definitions unchanged. Only new is that some executions
have `backfill_id` attribute set (Backfill-initiated) and some have
`cascade_source_backfill_id` (cascade-triggered). Both are sparse new
attributes; legacy executions without them work normally.

`is_internal_record()` in `utils.py` is updated to recognize Backfill
records (sentinel `pipeline_name=_polyris_bulk_backfill` AND
`record_type=backfill`) and exclude them from execution-list iterations.
Without this update, Backfill records would leak into UI counts. (See
Discovery Phase B, file `utils.py` entry.)

#### Out of scope

- Future symmetry where all executions get a Backfill ancestor — not
  rejected, just not in v0.78. ADR amendment if/when needed.
- Cron rule management UI (pause cron, edit schedule) — separate feature,
  unrelated.

#### Cost

Zero. No new infrastructure. Logic-only changes in `utils.py` and UI
filter; no AWS dimension affected.


### 56. Backfill Status Model — six states, aggregation rules, mapping to execution statuses (v0.78.0)

#### Context

ADR #51 introduces Backfill as a unit of work with its own lifecycle.
Backfill status is not the same as execution status — a Backfill
aggregates N executions and has its own state machine reflecting bulk
operation semantics (cancel, partial completion). This ADR specifies
the status enum, transitions, computation rules, and how it relates to
the existing execution status enum.

#### Decision

##### Backfill status enum (6 values)

```python
class BackfillStatus(str, Enum):
    PENDING   = "pending"     # Backfill record created, bulk-backfill SFN not yet started Map
    RUNNING   = "running"     # Map iterating; at least one partition in flight
    COMPLETED = "completed"   # All partitions succeeded
    FAILED    = "failed"      # All attempted partitions failed (zero succeeded)
    PARTIAL   = "partial"     # Some succeeded, some failed (mixed outcome)
    CANCELED  = "canceled"    # User-initiated cancel via /api/backfills/{id}/cancel
```

##### Status transitions

```
[Initial: backfill record created in /api/backfill handler]
                 ↓
              PENDING
                 ↓ (bulk-backfill SFN Initialize state writes status=running)
              RUNNING ──────────── (cancel signal) ──────────→ CANCELED
                 ↓
              [Map iterations complete, Finalize state runs]
                 ↓ (aggregation rules below)
                 ├── COMPLETED  (completed_partitions > 0 AND failed_partitions == 0)
                 ├── FAILED     (completed_partitions == 0 AND failed_partitions > 0)
                 └── PARTIAL    (completed_partitions > 0 AND failed_partitions > 0)
```

Once a terminal status is set (COMPLETED, FAILED, PARTIAL, CANCELED), no
further transitions occur. The DDB record is immutable except for TTL.

##### Aggregation rule (in Finalize state)

```python
def compute_final_status(record):
    if record["status"] == "canceled":
        return "canceled"  # cancel preserved even if some partitions ran
    
    completed = record.get("completed_partitions", 0)
    failed = record.get("failed_partitions", 0)
    skipped = record.get("skipped_partitions", 0)
    total = record["total_partitions"]
    
    # Skipped don't count as failed; they were intentionally skipped via skip_completed
    if completed + failed + skipped != total:
        # In-flight state — shouldn't reach Finalize, but defensive default
        return "failed"
    
    if failed == 0:
        return "completed"
    elif completed == 0:
        return "failed"
    else:
        return "partial"
```

##### Cancel preservation

User cancels mid-run. By that point, e.g., 3 partitions succeeded and 2
failed. What's the final status?

**Decision: CANCELED preserves over outcome.** The cancel signal is what
the user explicitly requested; the per-partition outcomes are
informational. UI shows:

```
Status: canceled
  Completed: 3
  Failed: 2
  Pending (canceled): 5
```

This avoids ambiguity. If a backfill is canceled with 3/10 done, calling
it "partial" suggests the cancel was incidental — but cancel was the
user's deliberate action. Better to be honest: canceled, with details.

##### Mapping to execution status

Each Backfill child is a separate pipeline execution with its own status.
Execution status enum (existing, unchanged):

```python
class ExecutionStatus(str, Enum):
    PENDING        = "pending"          # SFN started, dependency_wrapper init
    RUNNING        = "running"          # In-flight tasks
    SUCCESS        = "success"          # All tasks succeeded
    FAILED         = "failed"           # At least one task failed (no skip recovery)
    UPSTREAM_FAILED = "upstream_failed" # Skipped due to upstream task failure
    SKIPPED        = "skipped"          # Skipped via skip_tasks parameter
    STOPPED        = "stopped"          # User stop_execution call
    ABORTED        = "aborted"          # SFN ABORTED (rare; race conditions)
```

Mapping for Backfill aggregation purposes (in bulk-backfill SFN
ChildSucceededChoice):

```
Execution status → Counter increment
─────────────────────────────────────
SUCCESS          → completed_partitions++
FAILED           → failed_partitions++
UPSTREAM_FAILED  → failed_partitions++
STOPPED          → failed_partitions++  (counts as failed for aggregation)
ABORTED          → failed_partitions++  (defensive)
SKIPPED          → completed_partitions++  (all tasks intentionally skipped == "did nothing successfully")
PENDING/RUNNING  → Never observed; .sync waits for terminal status
```

Aggregation question: a SKIPPED execution (all tasks user-skipped via
skip_tasks parameter) counts as completed. This matches user intent: "run
nothing for this partition" should not be a failure.

##### Partial completion handling

When Backfill ends in PARTIAL state:
- UI shows Backfill record with status=partial, prominent retry-failed
  button
- `/api/backfills/{id}/retry-failed` creates new Backfill with
  `partitions.keys = [failed list]` and `parent_backfill_id = original`
- Original Backfill keeps status=partial; new Backfill linked via parent
- User can chain retries; each retry creates new Backfill, all linked

##### Cancel handling — what is "still running" at cancel time

When user POSTs /api/backfills/{id}/cancel:
1. Handler updates DDB record status = canceled
2. Bulk-backfill SFN's Map iterations check status in
   CheckBackfillCanceled at each iteration start
3. Iterations that have already entered StartChildSFN cannot be
   interrupted — they wait for child completion (could take hours)
4. New iterations don't start (Map respects MaxConcurrency from this
   point, but iterations are short-circuited by IsCanceledChoice)
5. Finalize runs at the end; reads counters that include any partitions
   that completed/failed despite cancel; sets final status=canceled
   (preserving user intent)

UI during this transitional state shows:

```
Status: canceling...
  Completed: 3
  Failed: 1
  In progress (will finish before cancel takes effect): 1
  Skipped (canceled before start): 5
```

After bulk-backfill Finalize:

```
Status: canceled
  Completed: 4 of 10
  Failed: 1 of 10
  Skipped: 5 of 10
```

The "canceling..." intermediate state is computed client-side in UI as:
`(record.status == "running" AND user_requested_cancel == true)`. Not
stored in DDB.

##### Why no IN_PROGRESS state separate from RUNNING

Considered but rejected. Two states would require a separate transition
from PENDING → IN_PROGRESS → RUNNING; in practice the bulk-backfill SFN
Initialize writes status=running atomically before Map starts. The
distinction adds complexity without value.

##### Status comparison with other systems

| System | Bulk states |
|---|---|
| **Managed orchestrators** | success / failed / running (only 3) |
| **Dagster Backfill** | requested / in_progress / completed / failed / canceled (5) |
| **polyris** | pending / running / completed / failed / partial / canceled (6) |

polyris adds `partial` because at our scale (90-day backfills), partial
success is the **common** outcome, not exceptional. Conflating it with
"failed" is misleading; with "completed" hides the failures. Distinct
state captures the reality and drives the retry-failed flow naturally.

##### UI status badges

CSS classes for each status (`_runs.css` BEM):

```
.bf-status--pending   { background: gray-200; }
.bf-status--running   { background: blue-100; }
.bf-status--completed { background: green-100; }
.bf-status--failed    { background: red-100; }
.bf-status--partial   { background: yellow-100; }
.bf-status--canceled  { background: gray-300; }
```

Icons (lucide-react):
- pending: `Clock`
- running: `Activity` (spinning animation)
- completed: `CheckCircle2`
- failed: `XCircle`
- partial: `AlertTriangle`
- canceled: `Ban`

##### API responses with status

`/api/backfills/{id}` response includes:

```json
{
  "backfill_id": "bf-a1b2c3d4",
  "status": "partial",
  "total_partitions": 10,
  "completed_partitions": 7,
  "failed_partitions": 2,
  "skipped_partitions": 1,
  ...
}
```

`/api/backfills` list response includes status per item with same shape.

##### Reconciliation

Edge case: bulk-backfill SFN crashes or is manually stopped via AWS
Console. DDB record remains in `running` state indefinitely.

**Reconciliation:** out of scope for v0.78. The 30-day TTL eventually
cleans up stale records. Alternative (future): a daily reconciliation
Lambda that scans for `status=running` records whose SFN execution is
ABORTED/FAILED in AWS, and updates them to `failed`. Not built now.

#### Migration

No data migration needed (no production). New `status` field on Backfill
records (sparse on legacy execution rows — they don't have it). All
backfill code paths consume the enum starting v0.78.

#### Testing

`tests/sdk/test_backfill_status.py` (new, ~120 LOC):
- All 6 transitions (PENDING → ..., RUNNING → ..., etc.)
- Aggregation rules for each combination of counters
- Cancel preservation (cancel during running → final status canceled)
- Mapping table for each execution status
- Retry-failed flow (failed → new Backfill, parent_backfill_id correct)

#### Cost

Zero infrastructure. Pure model + enum. Counter updates already covered
in ADR #54 bulk-backfill SFN's `IncrementCompleted` / `IncrementFailed`
/ `IncrementSkipped` states.


### 57. Cascade Semantics — auto / all / none with non-transitive propagation (v0.78.0)

#### Context

ADR #51 introduces `cascade` option for asset-target backfills. This ADR
specifies exactly what happens to downstream consumers under each
cascade mode, edge case handling, and the implementation pattern.

#### Decision

Three cascade modes, applicable only when `target.type == "asset"`:

```python
class CascadeMode(str, Enum):
    AUTO = "auto"   # Default. Emit normal asset events; consumers decide.
    ALL  = "all"    # Force-trigger direct consumers regardless of rules.
    NONE = "none"   # Suppress asset event emission entirely.
```

For pipeline-target backfills, `cascade` is invalid (400 error).
Cascade applies only when materializing an asset, which by definition
emits a downstream-visible event.

##### Mode semantics in detail

###### `cascade=auto` (default)

For each partition's producer execution that succeeds, the normal asset
event is emitted. Downstream consumers receive the event via the existing
`notify_asset_consumers` SFN + `notify_asset_subscribers` Lambda
infrastructure. The consumer pipeline triggers (or doesn't) based on its
declared rules:

- `AssetRef(asset, freshness_hours=N)` — consumer triggers only if event
  age ≤ N hours. For backfill events with `event_time` in the past
  (e.g., backfilling Jan 2024 events in May 2026), age >> N → consumer
  skipped silently.
- `AssetAll([A, B])` / `AssetAny([A, B])` — consumer triggers when AND/OR
  condition over assets is met. Backfill partial coverage may not satisfy
  AND.
- `asset.consecutive(days=7)` — consumer requires 7 consecutive days
  materialized. Backfill of 3 of 7 days won't trigger consumer.

This is **respect the contract the user declared**. If user wrote
`within(hours=6)`, they meant it. Backfill should not bypass it silently.

Lineage: cascade-triggered consumer executions get
`cascade_source_backfill_id` set in their input → DDB. UI shows the link.

###### `cascade=all`

Producer runs; on success, asset event is emitted with an extra flag
`_cascade_all=true`. The receiving `notify_asset_consumers` SFN
recognizes this flag and **force-triggers** direct consumers, bypassing:

- Freshness checks (`within()`)
- AND/OR conditions (`AssetAll`, `AssetAny`)
- Consecutive day requirements (`consecutive()`)

Consumers receive `is_cascade_forced=true` in their input → tasks can
detect cascade-forced runs and adjust behavior (skip notifications,
write to alternate prefix, etc.). New input field.

Force-triggered consumers carry `cascade_source_backfill_id` same as
auto mode.

##### `cascade=none`

Producer runs; on success, asset event emission is **suppressed**. No
notification to consumers, no event row written to AssetEvents table.

Implementation: producer execution input carries `_suppress_asset_event=true`
flag. The `notify_dependents` step in the pipeline SFN template checks
this flag and skips its emission state.

Use case: silently re-materialize historical data without disturbing
downstream pipelines. Useful for data corrections that user wants
isolated.

##### Non-transitive cascade

Cascade applies only to **direct consumers** of the backfilled asset.
Transitive propagation through asset chains is not done by cascade —
each consumer's own asset emissions follow normal rules.

Example: `asset_A → consumer_B (produces asset_B) → consumer_C`.

Backfill on asset_A with cascade=all:
1. asset_A's producer runs for each partition
2. consumer_B is force-triggered (per cascade=all)
3. consumer_B succeeds → emits asset_B event normally (no cascade flag)
4. consumer_C reacts based on its own rules to asset_B event (no force)

If user wants asset_C also force-rebuilt, they backfill asset_C
explicitly. This is intentional bounded scope — prevents "cascade=all on
upstream of long chain accidentally triggers 50 downstream pipelines."

##### Multi-producer asset

If an asset has more than one producer pipeline (e.g., `acme/orders`
produced by both `daily_etl` and `manual_correction`), backfill on that
asset returns 400 `multi_producer_asset` with the producer list:

```json
{
  "error": "multi_producer_asset",
  "message": "Asset 'acme/orders' has multiple producers",
  "producers": [
    {"pipeline": "acme/daily_etl", "outlets": [...]},
    {"pipeline": "acme/manual_correction", "outlets": [...]}
  ],
  "suggestion": "Use target.type=pipeline with a specific producer"
}
```

User must pick. No multi-producer auto-pick or sequential run. Future
ADR can add UI helper to pick from the list and re-submit.

##### Edge cases matrix

| Scenario | auto | all | none |
|---|---|---|---|
| Consumer freshness gate fails (old event_time) | skip silently | force-trigger | N/A (no event) |
| Consumer AND condition partial (1/2 deps materialized) | skip (await full) | force-trigger | N/A |
| Consumer consecutive(days=N) partial | skip (await full) | force-trigger | N/A |
| Consumer is paused | event queues; resumes when unpaused | force-trigger ignored (paused respects) | N/A |
| Consumer has no rules (always triggers on event) | trigger | trigger (same) | N/A |
| Consumer triggers other downstream | normal asset-event chain | normal asset-event chain | N/A |
| Backfill failure on producer partition | no event emitted for that partition | no event for that partition | no event |
| Asset has no consumers | no-op cascade | no-op cascade | no-op cascade |
| Consumer is the same pipeline as the asset's producer | logical loop; reject at validation | logical loop; reject | N/A (no event) |

##### Preview warnings

The `/api/backfill` response includes `warnings[]` for cascade-related
issues detected at pre-flight:

```json
"warnings": [
  {
    "code": "freshness_gate_will_fail",
    "consumer": "weekly_summary",
    "message": "weekly_summary uses .within(hours=6); events for partitions older than 6h won't trigger it."
  },
  {
    "code": "consecutive_partial_coverage",
    "consumer": "monthly_report",
    "message": "monthly_report uses .consecutive(days=7); selected range covers only 3 of 7 days for week 2024-W03."
  },
  {
    "code": "consumer_paused",
    "consumer": "alerts_pipeline",
    "message": "alerts_pipeline is currently paused; cascade events will queue until resumed."
  }
]
```

Warnings are informational; do not block submission. User decides
whether to proceed.

##### Asset event payload changes

Existing event payload (from notify_dependents SFN):

```json
{
  "asset_name": "acme/orders",
  "event_time": "2024-01-15T12:00:00Z",
  "source_execution": "acme-daily-2024-01-15-...",
  "source_task": "publish"
}
```

New fields added in v0.78:

```json
{
  ...existing,
  "partition_key": "2024-01-15",
  "_cascade_all": true,                       // present if cascade=all
  "_suppress_emission": false,                // never serialized; flag at producer
  "cascade_source_backfill_id": "bf-a1b2c3d4" // set if cascade-originated
}
```

`partition_key` is added unconditionally (every emission includes it for
matrix/drift compatibility).

##### Implementation locations

1. **producer execution input** (built by bulk-backfill SFN Build_Child_Input
   state): if `cascade=all`, set `is_cascade_all=true` in input.
   If `cascade=none`, set `_suppress_asset_event=true`. Both fields are
   sparse (omitted in normal scheduled runs).

2. **notify_dependents SFN template** (per-task emission): reads
   `_suppress_asset_event` flag from execution input. If true, skip the
   emission state. Add one Choice state at the start.

3. **notify_asset_consumers SFN template**: reads `_cascade_all` from
   event payload. If true, skip the freshness/AND check in its Choice
   states, proceed directly to force-trigger.

4. **Consumer execution input** (set by notify_asset_consumers):
   propagates `cascade_source_backfill_id` from event. New attribute
   in input.

5. **dependency_wrapper template** (per-task wrap of consumer): reads
   `is_cascade_forced` and `cascade_source_backfill_id` from execution
   input, writes both to DDB on first task event for the execution.

##### CSS / UI surface

In `/runs` row for cascade-triggered execution:

```
weekly_summary  success  5m ago  🔗 cascade from bf-a1b2c3d4
```

The 🔗 icon + bf-id link clickable, navigates to /backfills/bf-a1b2c3d4.

In Backfill Detail page Cascade Summary panel:

```
Cascade summary:
  Mode: auto
  Asset events emitted: 8 of 8 partitions
  Consumer triggers (live):
    weekly_summary    2 triggered / 0 queued / 0 skipped (freshness)
    monthly_report    0 triggered / 0 queued / 1 skipped (consecutive partial)
    alerts_pipeline   0 triggered / 5 queued (paused)
```

##### Out of scope

- Per-consumer cascade override (e.g., "cascade=all to weekly_summary but
  cascade=none to alerts_pipeline") — not supported. Cascade applies
  uniformly to all direct consumers of the backfilled asset.
- Cascade-aware retry-failed — retry-failed inherits the original
  cascade mode. Out of scope to change cascade per retry.
- Cascade depth limit (max transitive levels) — non-transitive by
  design, so no limit needed.

#### Testing

`tests/integration/test_cascade.py` (new, ~250 LOC):
- Three modes × at least 5 scenarios each from the edge cases matrix
- Lineage propagation verified end-to-end via DDB inspection
- Suppression mode verifies no event in AssetEvents table
- Force mode verifies bypass of freshness check
- Multi-producer rejection
- Preview warnings populated correctly

#### Migration

No migration. Cascade is a new option on `/api/backfill`. Existing
notify_dependents and notify_asset_consumers SFN templates are extended
with new Choice states; existing consumers (executing without cascade
input fields) work unchanged.

#### Cost

Zero infrastructure. New input fields are sparse, no cost. Choice states
in SFN templates add ~2 transitions per emission/notify; ~$0.00005 per
backfill incremental.


### 58. Partition Keys & Granularity-Aware Range Expansion (v0.78.0)

#### Context

ADR #51 introduces `partition_key` as a first-class field on backfill
records and child executions. This ADR specifies:
- Format per granularity
- Range expansion algorithm
- partition_start clipping
- Translation between granularities (for cascade=all on multi-granularity
  consumers)
- Compatibility with existing `current_date` field

#### Decision

##### Partition key format per granularity

| Granularity | Format | Example | Boundary |
|---|---|---|---|
| `hourly` | `YYYY-MM-DDTHH` | `2024-01-15T14` | UTC hour-start |
| `daily` | `YYYY-MM-DD` | `2024-01-15` | UTC midnight |
| `weekly` | `YYYY-Www` | `2024-W03` | ISO 8601 week (Mon-start) |
| `monthly` | `YYYY-MM` | `2024-01` | First of month UTC |

ISO 8601 conformance:
- `weekly`: `YYYY-Www` is ISO 8601 week date (week 1 = week containing
  Jan 4). Already used by Asset DSL `partition_start` validation
  (ADR #50).
- `monthly`: `YYYY-MM` is ISO 8601 reduced precision.
- `daily`: `YYYY-MM-DD` is ISO 8601 calendar date.
- `hourly`: `YYYY-MM-DDTHH` is **shortened** from full ISO 8601
  (`YYYY-MM-DDTHH:MM:SS+ZZ`). We use UTC implicitly and omit minute/second.

All times are UTC. No timezone in the format; documented as UTC by
convention.

##### Range expansion algorithm

```python
# polyris/partitions.py
class PartitionRange:
    @classmethod
    def expand(
        cls,
        start: str,         # ISO date or partition_key
        end: str,
        granularity: Literal["hourly", "daily", "weekly", "monthly"],
        partition_start: Optional[str] = None,  # asset's partition_start (clipping)
    ) -> "PartitionRange":
        # 1. Parse start and end into a normalized comparable form
        start_dt = _parse_to_datetime(start, granularity)
        end_dt = _parse_to_datetime(end, granularity)
        
        # 2. Clip start to asset's partition_start if applicable
        if partition_start:
            ps_dt = _parse_to_datetime(partition_start, granularity)
            if ps_dt > start_dt:
                start_dt = ps_dt  # don't go earlier than asset's history
        
        # 3. Normalize to bucket boundary (round down)
        start_dt = _floor_to_bucket(start_dt, granularity)
        end_dt = _floor_to_bucket(end_dt, granularity)
        
        # 4. Iterate bucket-by-bucket
        keys = []
        current = start_dt
        while current <= end_dt:
            keys.append(_format_key(current, granularity))
            current = _advance(current, granularity)
        
        return cls(keys=keys, granularity=granularity)
```

Helpers:

```python
def _parse_to_datetime(s: str, granularity: str) -> datetime:
    if granularity == "hourly":
        # Accept YYYY-MM-DDTHH or YYYY-MM-DD (defaults to hour 0)
        if "T" in s: return datetime.strptime(s, "%Y-%m-%dT%H")
        return datetime.strptime(s, "%Y-%m-%d")  # hour=0
    
    if granularity == "daily":
        return datetime.strptime(s, "%Y-%m-%d")
    
    if granularity == "weekly":
        if "W" in s:
            # Parse YYYY-Www to ISO Monday-of-week
            year, week = s.split("-W")
            return datetime.fromisocalendar(int(year), int(week), 1)
        # Accept YYYY-MM-DD; clip to that date's ISO week start (Monday)
        d = datetime.strptime(s, "%Y-%m-%d")
        return d - timedelta(days=d.weekday())
    
    if granularity == "monthly":
        if "-" in s and len(s) == 7:  # YYYY-MM
            return datetime.strptime(s, "%Y-%m")
        d = datetime.strptime(s, "%Y-%m-%d")  # accept full date, clip to month
        return d.replace(day=1)

def _floor_to_bucket(dt: datetime, granularity: str) -> datetime:
    if granularity == "hourly":   return dt.replace(minute=0, second=0, microsecond=0)
    if granularity == "daily":    return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "weekly":   return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0)
    if granularity == "monthly":  return dt.replace(day=1, hour=0, minute=0, second=0)

def _advance(dt: datetime, granularity: str) -> datetime:
    if granularity == "hourly":   return dt + timedelta(hours=1)
    if granularity == "daily":    return dt + timedelta(days=1)
    if granularity == "weekly":   return dt + timedelta(weeks=1)
    if granularity == "monthly":
        if dt.month == 12: return dt.replace(year=dt.year+1, month=1)
        return dt.replace(month=dt.month+1)

def _format_key(dt: datetime, granularity: str) -> str:
    if granularity == "hourly":   return dt.strftime("%Y-%m-%dT%H")
    if granularity == "daily":    return dt.strftime("%Y-%m-%d")
    if granularity == "weekly":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "monthly":  return dt.strftime("%Y-%m")
```

##### Edge cases

| Input | Granularity | Expansion |
|---|---|---|
| `2024-01-15` to `2024-01-15` | daily | `["2024-01-15"]` (1 partition) |
| `2024-01-15` to `2024-01-15` | weekly | `["2024-W03"]` (clipped to week start; 1 partition) |
| `2024-01-15` to `2024-01-31` | daily | 17 partitions |
| `2024-01-15` to `2024-01-31` | weekly | `["2024-W03", "2024-W04", "2024-W05"]` (3 weeks, partial week 5) |
| `2024-01-31` to `2024-02-01` | monthly | `["2024-01", "2024-02"]` (2 months, both partial) |
| `2024-W03` to `2024-W05` | weekly | `["2024-W03", "2024-W04", "2024-W05"]` |
| Input `2024-12-30` | weekly | ISO calendar: that's week 2025-W01 |
| Reverse order (end < start) | any | Error: `invalid_partition_range` |
| `partition_start=2024-06-01` + request `2024-01-01:2024-12-31` daily | daily | Clipped start to `2024-06-01`; 214 partitions, with warning surfaced in response |

##### Compatibility with `current_date`

Existing pipeline SFN templates (`run_task::Prepare_Task_Input`) use
`$states.input.current_date` to compute date variables for tasks:
- `current_date` → user code via `{{ current_date }}` template variable
- `yesterday`, `tomorrow` → date arithmetic in JSONata
- `minus_1_month`, `day_of_year`, `week_of_year` → calendar math
  (handled at ingest by `routes/backfill.py`, see ADR #51)

After v0.78 redesign, **bulk-backfill SFN translates `partition_key` to
`current_date`** when invoking child pipeline:

| Partition key | current_date (daily anchor) |
|---|---|
| `2024-01-15` (daily) | `2024-01-15` |
| `2024-01-15T14` (hourly) | `2024-01-15` (truncate to date) |
| `2024-W03` (weekly) | Monday of W03 = `2024-01-15` |
| `2024-01` (monthly) | First of month = `2024-01-01` |

This preserves backward compatibility — task code reading `current_date`
sees a YYYY-MM-DD string as it always did.

For tasks that need the original partition_key (e.g., to construct
weekly file paths like `s3://bucket/year=2024/week=03/data.parquet`),
the input also carries the original `partition_key` field unchanged.
Task code accesses it via `{{ partition_key }}` template variable
(new, added to the `polyris/variables.py` registry).

##### Translation for cascade=all on multi-granularity consumers

When `cascade=all` and the backfilled asset (e.g., daily orders) is
consumed by a pipeline of different granularity (e.g., weekly summary),
the consumer must receive a partition_key in **its** granularity, not
the source's.

Translation algorithm:

```python
def translate_to(
    source_keys: List[str],
    source_granularity: str,
    target_granularity: str,
) -> List[str]:
    if source_granularity == target_granularity:
        return source_keys
    
    # Convert each source key to datetime, floor to target bucket,
    # dedupe, format.
    target_dts = set()
    for key in source_keys:
        dt = _parse_to_datetime(key, source_granularity)
        floored = _floor_to_bucket(dt, target_granularity)
        target_dts.add(floored)
    
    sorted_dts = sorted(target_dts)
    return [_format_key(dt, target_granularity) for dt in sorted_dts]
```

Example:
- Source: daily, keys = `["2024-01-15", "2024-01-16", "2024-01-22"]`
- Target: weekly
- Result: `["2024-W03", "2024-W04"]` (Jan 15-16 → W03, Jan 22 → W04)

Each unique target partition is triggered exactly once, even if multiple
source partitions map to it.

##### Validation rules

`POST /api/backfill` validates partition input:

1. **Format match.** If `partitions.start`/`partitions.end` provided as
   strings, parser must accept under the resolved target granularity.
   Mismatched formats → 400 `invalid_partition_format`.
2. **Range non-empty.** start ≤ end. Otherwise 400.
3. **Range bounded.** end - start ≤ 5 years to prevent typo-induced
   massive expansions. Otherwise 400 `range_too_large`.
4. **partition_start clipping.** If asset's `partition_start` > start,
   silently clip start (with warning, not error). Reasonable user
   forgiveness.
5. **Hard partition cap.** After expansion, `len(keys) ≤ 1000`. Else 400
   `range_too_large` (ADR #51, Question #8).

##### Out of scope

- Custom partition definitions (e.g., "every full moon", arbitrary
  irregular partitions) — not supported. Asset must have one of 4
  granularities.
- Per-asset partition_freq override (e.g., daily granularity but only
  twice a day) — out of scope; if needed, hourly granularity covers most
  sub-daily cases.
- Time zones other than UTC — out of scope. All partition keys are UTC.
  If user's data is in another timezone, they normalize at ingest.

#### Testing

`tests/sdk/test_partitions.py` (~350 LOC):
- Format validation per granularity (positive + negative cases)
- Range expansion for all 4 granularities including edge cases (year
  boundaries, ISO week edge weeks, leap day)
- partition_start clipping
- Reverse range rejection
- Translation between granularities (daily→weekly, daily→monthly,
  hourly→daily, dedup correctness)
- 1000-partition limit triggers correct error
- Single-partition cases (start == end)

Coverage target: 100% of `polyris/partitions.py` (small surface, no
external dependencies, achievable).

#### Migration

No migration. `partition_key` is a new field on all new backfill records
and child executions. Legacy executions without it work normally. UI
falls back to `current_date` for display if `partition_key` absent.

`polyris/partitions.py` is a new module; no existing module is replaced.

#### Cost

Zero infrastructure. Pure-Python computation. Sub-microsecond per
expansion call.


---

### 59. Backfill options propagated to child SFN — skip_tasks, force, cascade_all, suppress_asset_event (v0.78.0)

#### Context

The Backfill Unification (ADR #51) defined a contract where the API
accepts options like `tasks` (subset), `force`, and `cascade` (auto/all/none).
The bulk-backfill SFN (ADR #54) was updated to pass these through, but
**the child `run_task` helper SFN was never modified to read them**.
Result: 5 of 8 API options were silently no-ops at runtime — UI/API
accepted them, child execution ignored them.

External code review surfaced this gap. The half-implemented options
violate CLAUDE.md #7 ("Finish what you start"), #9 ("Docs current"),
and #13 ("Tests must verify integration contracts").

#### Decision

Extend the shared `run_task/sfn.tpl.json` helper with **additive**
Choice states that read these fields from input and short-circuit
appropriately. All changes are backwards-compatible: missing/falsy
input fields fall through to existing flow.

#### Contract

Fields in `run_task` input (set by bulk-backfill SFN's ItemSelector
when present, absent for normal scheduled runs):

| Field | Type | Default | Effect |
|---|---|---|---|
| `skip_tasks` | `string[]` | `[]` | If current `task_id` in list → skip entire task, emit status='skipped' |
| `_suppress_asset_event` | `boolean` | `false` | If true → skip `Emit_Asset_Events` state (no downstream wake-up via assets) |
| `force` | `boolean` | `false` | If true → skip `Check_Has_Dependencies` wait (run even if upstream incomplete) |
| `cascade_all` | `boolean` | `false` | Read by `notify_dependents` helper (separate template), broader notify semantics |

#### Implementation

Three Choice states added to `run_task/sfn.tpl.json` plus one
modification:

1. **`Check_Should_Skip_Task`** — new entry state. Choice on `skip_tasks`
   membership. Skip path → `Task_Skipped` Pass state → End.
2. **`Check_Should_Suppress_Asset_Events`** — inserted before existing
   `Emit_Asset_Events`. Choice on `_suppress_asset_event`. Skip path
   → `Notify_Asset_Subscribers` (bypass emit).
3. **`Check_Has_Dependencies`** — existing state, modified Choice
   condition to OR with `force=true` to bypass dep wait.

One Choice state added to `notify_dependents/sfn.tpl.json` for
`cascade_all` semantics.

#### Backwards compatibility

All existing scheduled pipeline runs invoke `run_task` without these
input fields. Each Choice state has a `Default` branch routing to the
existing flow. ASL snapshot tests pin this — if any default branch
diverges, snapshots fail in CI.

#### Cost

Per Standard SFN pricing ($0.025 / 1k transitions):
- Each new Choice state evaluation: $0.000025 per execution
- Worst case (4-task pipeline, all 4 options checked): +$0.0001 per
  execution
- 1000 executions/month: +$0.10/month worst case

Negligible. The existing $31/month infra budget unaffected.

#### Tests

- `tests/sdk/test_run_task_template.py` — 4+ contract tests pinning
  the new states and their default-fall-through behavior
- Existing 60 ASL snapshot tests — verify no regression in
  generated pipeline definitions
- New backend test cases in `TestSfnInputContract` — verify backend
  passes the right fields through bulk-backfill SFN ItemSelector

#### Alternatives considered

1. **Strip broken options from API** — honest but loses ADR #51 scope
   (task_subset, cascade='all'/'none' are key differentiators vs
   competing orchestrators).
2. **Document as known limitations** — dishonest, leaves false claims
   in API docs.
3. **Reimplement child SFN from scratch** — disproportionate.

Option chosen because (a) cost is negligible, (b) blast radius mitigated
by additive Choice states with safe defaults, (c) snapshot tests catch
regression, (d) restores integrity to ADR #51 contracts.

### 60. Runtime enforcement of `skip_on_backfill` task flag (v0.78.1)

#### Context

Tasks have a `skip_on_backfill: bool = False` parameter in the DSL.
It's documented as "Skip this task during backfill runs" and used in
real pipelines (`pipelines/acme/daily/dag.py:292,297,302` for three
scraper tasks). The flag is stored in pipeline-registry's
`tasks_metadata.skip_on_backfill` field, displayed in UI types, and
covered by an SDK smoke test that verifies DSL-level set/get.

External code review (2026-05-22) and direct verification revealed
that **the flag was declared, stored, and never enforced at runtime**.
A grep across the entire codebase for any path that reads
`skip_on_backfill` outside of declaration/storage/test sites returned
zero results. Backfill runs were executing every task in the
pipeline regardless of the flag.

The bug was hidden because:
- DSL test verifies `task.skip_on_backfill is True` (declaration), not
  runtime behavior.
- Real pipelines using the flag tolerated the bug since scrapers also
  succeed on re-run (just wasteful API calls).
- No backfill snapshot or contract test asserted the flag's effect.

#### Decision

Enforce `skip_on_backfill` at the backfill API boundary by computing
`skip_task_ids` as a union of two sources:

  (a) **Task-subset complement** — when the user supplies a positive
      `tasks: [...]` list, skip everything NOT in the subset.
  (b) **`skip_on_backfill=True` flag** — read from
      `pipeline_registry.tasks[*].skip_on_backfill` and added to
      `skip_task_ids` regardless of whether a user subset is provided.

User-explicit subset takes precedence over the flag. If a developer
passes `tasks=['scraper_t1']` and `scraper_t1.skip_on_backfill=True`,
the task runs — the user said so explicitly. The flag is a safety
default for "I just clicked Backfill", not an unconditional ban.

#### Contract

| Scenario | task_subset | skip_on_backfill tasks | skip_task_ids |
|---|---|---|---|
| Default backfill, no flags | None | none | `[]` |
| Default backfill, flags set | None | t1, t3 | `[t1, t3]` |
| Subset, no flags | `[t1, t2]` | none | complement |
| Subset includes flagged | `[t1]` (t1 flagged) | t1, t3 | `[t2, t3, t4]` |
| Subset excludes flagged | `[t2, t4]` | t1, t3 | `[t1, t3]` |

Order in `skip_task_ids` matches order in the registry's task list
(stable, deterministic).

#### Implementation

Helper `_compute_skip_task_ids(target_pipeline, task_subset)` in
`sam/lambdas/console_api/routes/backfill.py`. Returns list of task
IDs to skip. Called from `start_backfill` after task_subset
validation, before building the SFN input. The pipeline SFN (built by
`polyris.generators`) already consumes `$states.input.skip_tasks` via
`JSONATA_SKIP_TASKS` — the Choice state in each task short-circuits
if its task_id appears in the list. No SFN-template change needed.

#### Backwards compatibility

Pipelines without `skip_on_backfill=True` on any task: unchanged
(empty skip list, same as before). Pipelines that already use the
flag: scrapers now correctly skip — this is the intended behavior
the DSL promised since v0.71. Counts as a **bug fix, not a breaking
change** because no production behavior depended on the flag being
silently ignored.

#### Cost

Zero. Same SFN structure, same Lambda invocations. The flag merely
adds task IDs to an existing list field.

#### Tests

Four contract tests in
`sam/lambdas/console_api/tests/routes/test_backfill.py::TestSkipOnBackfillFlag`:

- `test_flag_skips_scrapers_when_no_task_subset` — canonical case.
- `test_flag_ignored_for_tasks_in_explicit_subset` — explicit override.
- `test_flag_with_subset_unions_correctly` — union semantics.
- `test_no_flagged_tasks_yields_empty_skip_list` — guards against
  spurious flag enforcement when no task is flagged.

#### Alternatives considered

1. **Enforce in generated SFN ASL via a new Choice state per task** —
   would couple every task generator to backfill semantics. Rejected:
   API boundary is the simpler injection point and re-uses the
   existing `skip_tasks` plumbing.
2. **Enforce in bulk-backfill SFN via JSONata over Map input** —
   would need DDB lookup per partition iteration, costly.
3. **Strip the flag entirely** — silently breaks the existing DSL
   contract and forces every user to migrate to subsets. Rejected.

### 61. Task-subset selection in backfill UI (v0.78.1)

#### Context

The Backfill API has supported task_subset (`tasks: ['t1', 't2']`) since
v0.78.0 (ADR #51), but no UI surfaced this control. Users wanting to
re-run only a subset of tasks within a pipeline (e.g. retry a single
processing step without firing scrapers, or re-run the tail of a DAG
after fixing a downstream bug) had to use `curl` directly.

External code review (2026-05-22) flagged this as a feature gap:
"backend ready, UI missing". Combined with ADR #60's runtime
enforcement of `skip_on_backfill`, surfacing task selection lets users
explicitly override the default-skip behavior when they need to.

#### Decision

Add a task-subset multi-select to `BackfillModal`, visible **only for
pipeline targets** and **inside the Options accordion** (collapsed by
default so casual users never see it).

Asset targets do not get this control. Backfilling an asset means
"materialize this asset for the partition"; the producer task is
uniquely determined by the asset's outlets, and any "subset" larger
than that one task is meaningless.

UI contract:

| State | Header label | Sent to API |
|---|---|---|
| Default (no clicks) | "All tasks" | `tasks: null` |
| User unchecks all | "0 selected" | `tasks: []` (still valid; no-op for the pipeline) |
| User picks subset | "N selected" | `tasks: [task_ids]` |
| "None" button | "0 selected" | `tasks: []` |
| "All" button | "All tasks" | `tasks: null` |

Each row shows `task_id`. Rows for tasks with `skip_on_backfill=True`
display a `skip-on-backfill` badge — explicit visual signal that the
default-skip flag applies and ticking the box overrides it (per ADR
#60's user-override semantics).

#### Data source

New lightweight hook `usePipelineTasksList(pipelineName)` fetches
`/api/pipeline-dag?name=X` and projects to
`[{task_id, skip_on_backfill?}, ...]`. Cached 60s in React Query.
Distinct from `usePipelineDetailQuery` (which also pulls execution
status and re-fetches frequently) — for picker purposes only static
metadata is needed.

#### Implementation

State: `selectedTasks: string[] | null` in `BackfillModal`. `null`
means "default (run all)", non-null is the positive subset. Built
into the request just before submit:

```ts
let tasksField: string[] | null = seed.tasks ?? null;
if (target.type === 'pipeline' && selectedTasks && selectedTasks.length > 0) {
    tasksField = selectedTasks;
}
```

`seed.tasks` (from task-detail entry point) remains the pre-fill;
user explicit selection in the modal overrides it.

#### Tests

Five vitest tests in `BackfillModal.test.tsx::task subset selection`:

- Selector renders for pipeline targets (header + 3 rows).
- `skip_on_backfill` rows display the flag badge.
- Selector is **absent** for asset targets.
- Default state shows "All tasks".
- Clicking a checkbox transitions from default to "N selected".

#### Backwards compatibility

Pure additive UI. No API change. `tasks: null` (the default) preserves
v0.78.0 behavior — full pipeline backfill subject to `skip_on_backfill`
enforcement (ADR #60).

#### Alternatives considered

1. **Show selector for all target types** — semantically wrong for
   assets (one task = one outlet). Confusing UX.
2. **Expose selector outside Options accordion** — overwhelms casual
   users. The whole point of "ideal for any user" (Mike, 2026-05-22)
   is that the simple case stays simple; subset selection is power-
   user territory.
3. **Separate "Advanced backfill" modal** — splits cognitive load
   across two entry points. Rejected: one modal that grows by
   expansion is simpler than two modals.

### 62. Backfill cost estimate removed pending Pro tier (v0.78.2)

#### Context

ADR #53 (v0.78.0) introduced `estimated_sfn_cost_usd` in the `/api/backfill`
response, surfaced in `BackfillsListPage` (column "Cost"),
`BackfillDetailPage` ("Estimated cost" row), and `BackfillModal` (preview
line). The methodology — modeling bulk-backfill SFN transitions + child
pipeline transitions — was technically sound and positioned as a
serverless differentiator vs competing orchestrators.

Live UI revealed a different problem. The label "Cost" reads to users as
"what this run cost" (actuals), while the value is "what we expect this
to cost at submit time" (estimate). The two diverge in two directions:

- **Optimistic divergence**: estimate assumes ideal Lambda execution
  duration. Real runs hit cold starts, retries, and per-task variance.
  Actual usage can be 1.5–3× estimate on small backfills (where init
  overhead dominates).
- **Pessimistic divergence**: skip-completed partitions short-circuit at
  submit time, but our estimate counted them before skip. UI showed
  $0.05 when actual SFN bill was $0.01.

Either way, the displayed number was at best directionally correct, never
exact. Users acted on it (or worse — quoted it to stakeholders) as if
it were a bill.

#### Decision

Remove `estimated_sfn_cost_usd` from API responses, DDB writes, UI
displays, and the `PartitionRange.cost_estimate()` SDK helper. Replace
nothing in Community tier.

Cost reporting becomes a **Pro-tier feature** spec'd as one coherent
workflow: estimate-at-submit + actuals-reconciled-post-run +
budget-enforcement. None of these are useful without the others, so we
ship none of them until we ship all of them.

#### What changed

| Layer | Before | After |
|---|---|---|
| `POST /api/backfill` response | `estimated_sfn_cost_usd: 0.0047` | Field removed |
| `POST /api/backfill?preview=true` response | Same | Field removed |
| DDB `pipeline-tokens` (backfill record) | Wrote `estimated_sfn_cost_usd` | No write |
| `polyris.partitions.PartitionRange.cost_estimate()` | Public method | Removed |
| `dal/ddb_schema.py BackfillCols.ESTIMATED_SFN_COST_USD` | Constant | Removed |
| `BackfillsListPage` | "Cost" column | Column removed |
| `BackfillDetailPage` | "Estimated cost" meta row | Row removed |
| `BackfillModal` | "Estimated SFN cost: $0.0047" preview line | Line removed |
| UI `BackfillStartResponse` / `BackfillPreviewResponse` / `BackfillSummary` types | `estimated_sfn_cost_usd` field | Field removed |

ADR #53 is **partially superseded** — methodology stays in git history
for the future Pro implementation; current Community surface is removed.

Existing DDB records with `estimated_sfn_cost_usd` are untouched; the
read path simply no longer projects the field. Cleanup is not required
(field occupies negligible space).

#### Backwards compatibility

Mild break for any external consumer that read
`estimated_sfn_cost_usd` from the API. We don't know of any (UI is the
sole consumer in this codebase), and v0.78.0 was pre-1.0 with no
external-contract guarantees. CHANGELOG entry under "Removed" signals
the change explicitly.

CLI (`tests/sdk/test_cli.py`) had a stub response containing the field;
updated to remove it. Any downstream tooling that parsed the field
must catch `KeyError` / `null`.

#### Tests removed

`tests/sdk/test_partitions.py::TestCostEstimate` (5 tests) — deleted
entirely per CLAUDE.md #11 (no skipped tests). The class no longer
exists.

`tests/e2e/test_backfill.py` — two `'estimated_sfn_cost_usd' in body`
assertions removed.

`sam/lambdas/console_api/tests/routes/test_backfill.py` — no changes
needed (existing tests didn't assert the cost field).

#### Alternatives considered

1. **Rename "Cost" → "Est. Cost" + tooltip** — band-aid; still misleads
   users into treating estimate as actual.
2. **Show estimate only in `BackfillModal` (pre-submit decision support)**
   — drops the list/detail views but keeps the modal. Inconsistent across
   surfaces and still half-deliverable.
3. **Keep estimate, add actuals via post-run reconciliation** — that's
   the Pro spec; doing it now would mean shipping a partial implementation
   in Community.

Option (3) is the spec; doing it as Community defeats the tier
differentiation. Option chosen because (a) honest about the gap,
(b) preserves Pro feature integrity, (c) shrinks Community surface area.

### 63. Backfill detail → pipeline DAG navigation (v0.78.2)

#### Context

`BackfillDetailPage` rendered a heatmap of partitions and a table of
child executions, but neither was clickable. Users wanting to drill from
"backfill bf-xxx failed on 2026-05-22" into "what did the DAG look like
for that partition" had no path forward — they'd close the detail page
and manually navigate to Pipelines, find the pipeline, set the date.
Multi-step. Easy to forget which date you came from.

External UX review (Mike, 2026-05-27): *"коли клацію то перекидає на dag
сьогоднішнього дня, а було б добре на можливість переключитись"*.

#### Decision

Two new optional callbacks on `BackfillDetailPage`:

```ts
onPartitionClick?: (pipelineName: string, partitionKey: string) => void
onChildClick?: (pipelineName: string, executionName: string, partitionKey: string | null) => void
```

App.tsx wires both to deep-links into the Pipelines view:

| Click target | Resulting URL |
|---|---|
| Partition heatmap cell `2026-05-22` | `/pipelines/?pipeline=<name>&date=2026-05-22` |
| Child execution row | `/pipelines/?pipeline=<name>&execution=<id>&date=<key>` |

When the callback prop is absent, the cell/row renders non-clickable
(static heatmap behavior preserved for any future use cases like a
read-only embed).

#### Why URL-based, not dropdown

Considered: dropdown picker on PipelinesPage to switch between
backfill→partition combinations. Rejected:

- Duplicates URL state. PipelinesPage already drives DAG view via
  `?date=` / `?execution=` query params. Adding a dropdown that *also*
  drives the view creates two sources of truth that must stay synced.
- Breaks shareable URLs. Send a colleague "/pipelines/?pipeline=daily&date=2026-05-22"
  and they see your view. Send "/pipelines/?pipeline=daily" + "click the
  third item in the dropdown" — not shareable.
- More state, more bugs. The chosen direct-link path is one URL hop,
  no extra UI surface, no extra state machine.

#### Implementation

- `BackfillDetailPage.tsx` — props extended; partition cells get
  `role="button"`, `tabIndex={0}`, Enter/Space keyboard handling, and
  `aria-label`. Children rows get `onClick` when callback provided.
- `App.tsx` — wires both callbacks to `router.push(...)` with the URL
  patterns above. Encodes pipeline name and execution name for safety.
- `_modals.css` — `.bd-cell--clickable` and
  `.bd-children-row--clickable` modifier classes add cursor + hover ring.

#### Tests

5 new tests in `BackfillDetailPage.test.tsx::Navigation`:

- `partition cell click invokes onPartitionClick with pipeline + key`
- `partition cell is not clickable when callback omitted`
- `partition cell responds to Enter key` (keyboard a11y)
- `child row click invokes onChildClick with execution_name + key`
- `partition cell has aria-label for screen readers when clickable`

#### Backwards compatibility

Pure additive. Existing call sites without the callbacks see the same
static heatmap as before. App.tsx provides callbacks → users get
clickability. No type changes break consumers.

### 64. Standard keyboard shortcut convention (v0.78.3)

#### Context

The `useKeyboardShortcuts` hook and `SHORTCUTS` catalog have existed
since early v0.x, but only two surfaces used them: global App routes
(`?`, `ctrl+k`, `Esc`, `ctrl+shift+t`) and the PipelineDetail refresh
(`ctrl+r`). Everything added since — Backfills list/detail, All Tasks,
All Runs, Asset matrix/detail, BackfillModal, TaskDetailModal — had
zero keyboard wiring, even where the visible buttons clearly invited it
(Refresh, tab switching, modal Submit).

The problem isn't the hook (which works well). The problem is that
adding a shortcut wasn't on anyone's checklist when shipping a new
view. Each new surface diverged from convention silently — and the
divergence compounded.

External UX feedback (Mike, 2026-05-27): *"шорткати у нас ніби є
підтримка лише для функціоналу який ми давно додавали, а для всього
нового в декількох табах і вкладках ми не додавали підтримки. Також це
треба в claude.md правило занести аби про це не забувалось."*

#### Decision

Codify a **standard shortcut mapping by surface type**, and add a
CLAUDE.md rule (#19) requiring it on every new view/page/modal/tab
container before merge.

| Surface type | Standard shortcuts |
|---|---|
| **Global** | `?` help, `⌘K` search, `Esc` close overlay, `⌘⇧T` theme |
| **List views** (BackfillsListPage, AllTasksView, AllRunsView, AssetMatrixView, …) | `⌘R` refresh, `/` focus filter, `J` next row, `K` prev row, `Enter` open selected |
| **Detail pages** (BackfillDetailPage, AssetDetailPage, PipelineDetail, …) | `⌘R` refresh, `Esc` back to list |
| **Multi-tab containers** (TaskDetailModal, AssetDetailPage tabs, PipelineDetail viewModes, HelpModal) | `1`, `2`, `3` … switch tab in declaration order |
| **Modals with primary action** (BackfillModal, …) | `Esc` close (via BaseModal), `⌘↵` (ctrl+enter) submit |

Reserved keys: `j`, `k`, `1`–`9`, `/`, `?`, single letters bind only
per the table above. Non-standard actions use modifier-key combos
(`ctrl+*`, `ctrl+shift+*`).

#### Catalog additions

`SHORTCUTS` extended with:

- `FOCUS_FILTER: '/'` — list-view filter focus
- `OPEN_SELECTED: 'enter'` — list-view open highlighted row
- `SUBMIT: 'ctrl+enter'` — modal primary action
- `TAB_1` … `TAB_9` — numeric tab switching

`NEXT_TASK` / `PREV_TASK` / `EXPAND` / `COLLAPSE` were already in the
catalog but had no consumers. They are now wired (`j`/`k`) in list
views.

#### Surfaces wired in this release

| Surface | Shortcuts added |
|---|---|
| BackfillsListPage | refresh, `/`, j/k, Enter |
| BackfillDetailPage | refresh, Esc back |
| AllTasksView | refresh, `/` |
| AllRunsView | refresh |
| AssetMatrixView | refresh |
| AssetDetailPage | refresh + tabs 1-6 |
| BackfillModal | ctrl+enter submit |
| TaskDetailModal | tabs 1/2/3 |
| PipelineDetail | tabs 1/2/3 for viewMode (already had refresh) |
| HelpModal | tabs 1/2/3/4 |

#### HelpModal sync

`KeyboardShortcutsTab` rewritten to group bindings by surface type
matching the convention above. Discoverability matters as much as
wiring — users finding the help modal need to see exactly what's
bound, not a stale subset.

#### Non-surfaces (not wired)

- Inline `onKeyDown` for `Enter`/`Space` on individual focusable
  elements (buttons, links, table cells, sidebar items) — these are
  accessibility, not shortcuts, and stay as-is per a11y standards.
- `useUrlSync` / browser back-forward — handled by routing, not the
  shortcut system.

#### Tests

New shortcut tests on the major surfaces:

- `BackfillsListPage.test.tsx`: ⌘R, /, j/k highlight, Enter opens (+4 tests)
- `BackfillDetailPage.test.tsx`: ⌘R, Esc (+2 tests)
- `TaskDetailModal.test.tsx`: 1/2/3 tab switching + closed-modal no-op (+3 tests)

Smaller surfaces tested implicitly via existing render tests (no
regressions on 752 → 761 vitest count).

#### Forward-looking

The CLAUDE.md rule means future additions get shortcuts at the same
time as the feature. If a future surface doesn't fit the table — say,
a graph visualization with pan/zoom that wants arrow keys — the rule
prompts the conversation: either extend the standard table here, or
explain in the PR why this surface is an exception.

### 64.1 Keyboard shortcut convention — revised key allocation (v0.78.5)

#### Context

ADR #64 (v0.78.3) introduced the standard mapping. Live UX revealed a
fatal conflict: numeric keys `1-9` were used by both **top-level
navigation in App.tsx** (`1`=Pipelines, `2`=Assets, `3`=Tasks, `4`=Runs,
`5`=Backfills) and **inner-surface tab switching** (PipelineDetail
viewMode, AssetDetailPage tabs, TaskDetailModal, HelpModal).

`document.addEventListener` registers a separate listener per component
that calls `useKeyboardShortcuts`. When the user presses `2` while on
the Pipelines page:

1. App.tsx listener fires → `router.push('/assets/')`
2. PipelineDetail listener fires → `setViewMode('gantt')`

Both happen. The user sees Gantt view briefly, then the route change
swaps the view, then they land on /assets/. Or vice-versa depending on
React reconciliation timing. Either way: not what the user expected.

External UX report (Mike, 2026-05-27): *"коли клацнув на 1 — на пайплайн
переключило (це ок), коли звідти клацаю 1,2,3 — переключає між DAG /
Gantt / Calendar (не ок, бо коли клікну 2 — я очікую переключитись на
табу assets)."*

#### Decision

Reserve **numeric keys `1-9` exclusively for top-level navigation** in
App.tsx. Inner-surface tab switching uses **letter keys matching the
first letter of the tab name** (or another short, memorable letter when
the first letter is taken).

Revised mapping:

| Surface | Old (v0.78.3, removed) | New (v0.78.5) |
|---|---|---|
| App.tsx top-level | `1`/`2`/`3`/`4`/`5` | `1`/`2`/`3`/`4`/`5` (unchanged) |
| PipelineDetail viewMode | `1`/`2`/`3` | `d` / `g` / `c` (DAG / Gantt / Calendar) |
| AssetDetailPage tabs | `1`–`6` | `o` / `s` / `p` / `e` / `c` / `l` (Overview / Schema / Partitions / Events / Checks / Lineage) |
| TaskDetailModal tabs | `1`/`2`/`3` | `d` / `t` / `a` (Details / Timeline / Actions) |
| HelpModal tabs | `1`/`2`/`3`/`4` | `s` / `i` / `b` / `a` (Shortcuts / Icons / Backfill / API) |

`SHORTCUTS.TAB_1` … `SHORTCUTS.TAB_9` are **removed from the catalog**
(no consumers; constants would only invite future re-introduction of
the same bug).

#### Cross-surface letter overlap

Two surfaces may both define a letter that maps to different actions
(`c` = Calendar in PipelineDetail and `c` = Checks in AssetDetailPage;
`a` = Actions in TaskDetailModal and `a` = API in HelpModal). This is
acceptable because:

- PipelineDetail and AssetDetailPage are **different routes**; user
  cannot be on both simultaneously.
- TaskDetailModal and HelpModal are both modals but **mutually exclusive**
  (only one modal open at a time in practice).

If a future modal is designed to overlay a page that uses the same
letter, the modal's handler will fire alongside the underlying page's.
Cleanest mitigation when this happens: gate the underlying page's
shortcut on a "no modal open" flag from the app store. Defer until
needed.

#### Forbidden bindings

- **Numeric `1`-`9` outside App.tsx**: forbidden. Reserved for top-level
  nav. Future PRs that bind numeric keys at any non-App surface must be
  rejected at review.
- **Single letters that are already SHORTCUTS catalog entries** (`j`,
  `k`, `e`, `c`): allowed at surfaces only when those catalog entries
  are not wired in the same context. `e` (catalog: EXPAND) is wired
  nowhere globally, so AssetDetailPage's `e` = Events is fine.

#### CLAUDE.md #19 update

Rule #19's "Multi-tab containers: `1`, `2`, `3` … switch tab in
declaration order" is **replaced** with: "Multi-tab containers within a
page: letter keys matching the first letter of each tab name. Numeric
keys reserved for global navigation." See CLAUDE.md for the canonical
current text.

#### Tests updated

All shortcut tests on affected surfaces switched from `fireEvent.keyDown
{ key: '2' }` patterns to letter equivalents. No behavior tests removed;
existing tests rewritten to match new bindings. HelpModal got one new
assertion that "Top-level navigation" group is present in the rendered
help text.

#### Lesson

The original ADR #64 was written before live UX with real users. The
numeric convention "felt right" in isolation but immediately broke once
combined with the pre-existing global numeric nav. The lesson: shortcut
conventions need to be checked against **the full set of already-wired
handlers**, not the empty set. CLAUDE.md #19 update adds an explicit
"check for conflicts with App.tsx global nav" step.

### 65 Top-level view registration — three-place sync (v0.78.6)

#### Context

Top-level views in the console (Pipelines, Assets, Tasks, Runs, Backfills)
are file-system routes under Next.js static export (`output: 'export'`,
`trailingSlash: true`). Each route generates `/{view}/index.html` and is
served from S3 behind CloudFront.

The view list lives in **three** places that must stay in sync:

1. **`ui/src/types/index.ts` — `MAIN_VIEWS`** — runtime guard used by
   `isMainView()` and `viewFromPathname()`. Drives Header tab rendering
   and App.tsx render switch.
2. **`sam/template.yaml` — `ConsoleUiUrlRewriteFunction.FunctionCode`** —
   regex `^\/(pipelines|...)(\/...)?$` that rewrites SPA deep paths
   (`/pipelines/anything`) to the corresponding `/{view}/index.html` so
   S3 finds the file. Without this rewrite, `/backfills/` (directory
   path) returns 404 → CloudFront `CustomErrorResponses` converts to
   200 + `/index.html` → RootPage component → redirect to `/pipelines/`.
3. **`ui/src/app/page.tsx` — `validViews`** — legacy-bookmark fallback
   for `/?view=backfills` URLs. If a view is missing here, the legacy
   URL ignores it and redirects to default `/pipelines/`.

#### Decision

When adding a top-level view to App.tsx mainView, all three locations
**must** be updated atomically in the same PR. Order doesn't matter
mechanically but **deploy must include both backend (template.yaml) and
frontend (page.tsx + types) in the same release**.

#### Why this matters

Backfills view was added in v0.75.x to App.tsx mainView, Header tabs,
and BackfillsPage Next route — but the CloudFront function and root
page were never updated. The result: clicking the Backfills tab took
the user to `/backfills/`, where S3 returned 404, CloudFront
masqueraded as 200, and the RootPage component instantly redirected
back to `/pipelines/`. User-visible behavior: "click Backfills tab,
end up on Pipelines tab". Subtle enough that it went unnoticed for
months because the URL flicker is brief.

Fixed in v0.78.6 by adding `backfills` to both:
- `sam/template.yaml`: regex alternation in `ConsoleUiUrlRewriteFunction`
- `ui/src/app/page.tsx`: `validViews` array

#### Prevention

`CLAUDE.md` adds a rule about the three-place sync. The CloudFront
function file includes an inline `⚠️` comment naming the other two
files. A future test could enforce by parsing all three sources and
comparing the lists, but YAGNI — explicit comments + ADR + CLAUDE.md
rule should be enough.

#### Hard-learned lesson

Path-based SPA routing with static export has a sharp edge: every new
route requires updates in places that don't share types or imports.
Type-safety helps within the TS bundle but stops at the
infrastructure-as-code boundary. The CloudFront function is plain JS
in a YAML string — neither TypeScript nor any linter catches the drift.
The only mitigation is explicit cross-reference comments + a CLAUDE.md
rule that's checked at PR review time.

### 66 Export CSV removed from BackfillDetailPage (v0.78.7)

#### Context

BackfillDetailPage shipped with an "Export CSV" button (v0.78.0) that
downloaded a four-column CSV: `partition_key`, `status`, `child_count`,
`child_execution_ids`. The implementation was ~30 lines of code in
`handleExportCsv`: build the rows, escape commas/quotes, create a Blob,
click an anchor tag, revoke the URL.

User feedback (Mike, v0.78.5 deploy review): *"цей експорт не містить
корисної інформації, можемо прибрати"*.

#### Decision

Removed the button, the `handleExportCsv` function, and the `Download`
icon import. The replaced test asserts the button is no longer
rendered, which pins the removal so a future PR can't silently put it
back.

#### Why remove rather than improve

The four columns described what a backfill already shows visually in
the partition heatmap (`partition_key`, `status`) and the children
table (`child_count`, `child_execution_ids`). Anything the CSV
provided was already on-screen. The audit workflows the user actually
runs (debugging a failed partition, reviewing what got backfilled)
don't benefit from a separate text file.

A useful CSV export would need to surface NEW information not visible
in the UI — execution durations, task-level breakdowns, error
classifications, cost data. That's a bigger feature, not a tweak.
When that need arrives (likely as part of Pro-tier cost tracking or
SLA reporting), the export is best designed against the actual
workflow rather than patched onto the current button.

#### Recovery path

If a user requests CSV export later, the implementation pattern is
documented in the v0.78.0 commit history (search for `handleExportCsv`).
Restoring it is mechanical. The harder work — designing a CSV that's
worth downloading — is the part this ADR defers, not the code.

#### Lesson

Public-facing feature removals deserve a brief ADR even when they're
"obvious wins". The ADR captures (a) what the feature was, (b) why
removing was better than improving, (c) where to look if it needs to
come back. Without this, the next person seeing "v0.78.0 had CSV
export, v0.78.7 doesn't" has to dig through git log to understand
intent.

### 67 Client-side derived status for backfills (v0.78.8)

#### Context

`bulk_backfill` SFN writes `status='running'` at start and writes a
terminal status (`completed`/`partial`/`failed`) in its `Finalize`
step at the very end (template: `sam/sfn_templates/bulk_backfill/sfn.tpl.json`).
If the Finalize step never runs — SFN aborted between counter updates
and finalize, manual intervention, eventual-consistency lag, IAM
revocation, etc — the DDB record stays at `status='running'` forever.

User-visible symptom (Mike, v0.78.7 deploy review): TOTAL=2,
COMPLETED=2, FAILED=0, and yet the UI showed the "Running. 2 of 2
partition(s) processed so far" banner with a Cancel button. Mike's
note: *"вони ж ніби компліт вже а не ранінг"*.

#### Decision

Introduce `derivedStatus` in `BackfillDetailPage` that overrides
backend-reported `running`/`pending` when partition counters indicate
the work is done. Mirror the SFN Finalize logic on the client:

```ts
if (raw !== 'running' && raw !== 'pending') return raw;  // terminal — respect backend
const processed = completed + failed + skipped;
if (total > 0 && processed >= total) {
    if (failed === 0) return 'completed';
    if (completed === 0) return 'failed';
    return 'partial';
}
return raw;
```

`derivedStatus` drives:
- Status pill modifier class (`bd-status-pill--${derivedStatus}`)
- Banner choice (partial / failed / canceled / running)
- `isActive` flag (gates the Cancel button)
- `canRetry` flag (gates the Retry failed button)

A `console.warn` is emitted whenever raw and derived disagree, so
future debugging can quickly spot the discrepancy in the browser
DevTools console.

#### Why client-side and not backend

The right fix is finding out why `Finalize` doesn't run in some cases
and ensuring it does. That requires SFN execution log inspection
across multiple stuck backfills to find the pattern. Until that
investigation produces a backend fix, the UI workaround is the
minimum-cost safety net — users see correct state, action buttons
match reality, no false alarms.

The workaround is conservative: it only re-interprets the
**non-terminal** raw values. If backend writes `completed` or
`failed`, the UI respects them. If backend writes `running` AND counts
agree (still in flight), the UI shows running. The override only fires
in the narrow `running` + `counts say done` case where the backend is
demonstrably wrong.

#### Recovery if backend is fixed later

When the SFN Finalize bug is identified and fixed, `derivedStatus`
becomes a no-op for new backfills (raw will be terminal). Existing
stuck backfills in DDB will still benefit until they're re-run or
manually fixed. The override can be removed in a future cleanup pass,
or kept indefinitely as defense-in-depth (recommended).

#### Pinned by tests

`BackfillDetailPage.test.tsx › derivedStatus (v0.78.8, ADR #67)`:
6 tests cover (a) terminal raw respected, (b) stuck-running →
completed, (c) stuck-running → partial, (d) stuck-running → failed,
(e) genuine in-flight remains running, (f) Cancel button hidden when
override fires.

#### Lesson

State derived from multiple fields (raw status + counters) should be
computed in one place at read time, not stored. Storing `status` in
DDB and updating it via a separate SFN step means two writes have to
both succeed for the data to be consistent. A computed view in the
read path eliminates that whole class of failure mode at the cost of
slightly more work per read — usually the right trade for UI-facing
state.

### 68 Backfill UX/DX bundle — partial ratio, retry chain, notification source (v0.78.11)

#### Context

User review of v0.78.10 surfaced three small but distinct gaps:

1. The `partial` status pill said only "partial" — no indication of how
   partial. A 4-of-5 success looked the same as 1-of-5. Users had to
   click into the detail page to see the ratio.

2. `retry_failed` stores a `parent_backfill_id` link on the new backfill,
   but the UI didn't surface it. Three rounds of retry produced four
   disconnected detail pages; the chain was visible only via grep on
   the DDB record.

3. Backfill terminal events emitted no notification. The `Notifications.tsx`
   bell + dropdown infrastructure already polled the
   `/api/notifications` endpoint every 30s for pipeline failures, but
   the endpoint didn't surface backfills at all. Users had to refresh
   the Backfills list manually to find out a backfill finished.

#### Decision

Bundle all three as one release (v0.78.11) because they share the
same domain (Backfill) and overlap in implementation cost:

**1. Partial ratio in pill** — purely cosmetic UI change:
`partial` → `partial (4/5)`. Done in two files (BackfillsListPage,
BackfillDetailPage). Other statuses unchanged.

**2. Retry chain on detail page** — two-direction linkage:
- *Upward*: `parent_backfill_id` already in the DDB record; just
  expose it in `_format_backfill_summary` and render as a clickable
  link.
- *Downward*: new repo method `list_retries_of(parent_backfill_id)`
  via DDB scan with filter (Backfills are infrequent — ~hundreds per
  pipeline per year — so scan is fine; if retry rate spikes, add a
  GSI). Exposed in detail endpoint as `retried_by[]`.

Rendered as a compact section between metadata and partition heatmap,
hidden entirely when neither parent nor children exist (avoids empty
section on first-time backfills).

**3. Backfill terminal events in notification feed** — extend
`get_notifications` to also fetch recent terminal backfills via
`backfills_repo.list_recent(100)`, filter to terminal statuses
finished within the query window, sort by `finished_at` desc, cap to
limit. New notification subtype `type='backfill'` with status-specific
icon coloring (green/red/amber/grey). Click navigates to detail page
via new `onNavigateBackfill` prop.

#### Naming: "Partitions" → "Backfilling dates"

Renamed the partition heatmap heading too — user feedback that
"Partitions" was technical jargon not aligned with how users actually
think about the operation. Internal data model still uses
`partition_keys` / `partition_*` field names; only the UI heading
changed. This is a small surface-only rename with no schema impact.

#### Trade-offs considered

- **Scan vs GSI for `list_retries_of`**: scan is O(N) on the
  backfills table; chosen because retry chains are exceptional events,
  not hot paths. If we add bulk-retry automation later (e.g.
  scheduled re-runs), revisit and add a `parent-backfill-id-index`
  GSI.

- **Notification source: poll vs push**: poll path is consistent with
  how pipeline failure notifications already work. A push model (e.g.
  EventBridge → notification table) would scale better and update
  faster, but adds infrastructure for a low-volume event class.
  Defer until backfill volume justifies it.

- **Bundling vs three releases**: three separate releases would have
  better changelog granularity but triple deploy cost. Bundling is
  safer when each piece is small and they share a code surface
  (BackfillDetailPage gets edits for all three).

#### What this does NOT do

- Per-partition retry/cancel/skip — that's still in BACKLOG for the
  v0.79.x extension of existing endpoints. Out of scope here.
- Cost-preview before launch — separate concern, separate release.
- Slack notification integration — Mike explicitly said UI-only
  (bell + dropdown), no Slack.

#### Lesson

When a UX issue can be solved with backend extension + UI rendering,
prefer extending existing endpoints over creating new ones. Both
`_format_backfill_summary` and `get_notifications` got one new field
each + light render code — no new routes, no new tables, no new GSIs.
This stays consistent with the "extend, don't duplicate" principle
that the per-partition-cancel discussion landed on (CLAUDE.md hard
rule #12 — Maximize reuse).

### 69 derivedStatus lifted to shared util + inline Cancel on list (v0.78.12)

#### Context

ADR #67 introduced `derivedStatus` inside `BackfillDetailPage` to defend
against a stuck `bulk_backfill` SFN — backend reports `status='running'`
forever when the SFN never reaches its Finalize step, even though all
partitions completed. The detail page worked correctly: pill showed
"completed", Cancel button hidden.

The list page (`BackfillsListPage`) did NOT use derivedStatus. Result:
the SAME backfill showed `RUNNING` in the list and `COMPLETED` in the
detail. Mike caught this in v0.78.11 review. Same SFN, two different
truths in the UI.

Additionally: Cancel was reachable only from the detail page. To cancel
a running backfill, users had to navigate from list → row click → wait
for detail load → click Cancel. For a list page where multiple
backfills may need triaging at once, this is annoying.

#### Decision

**1. Extract `computeDerivedBackfillStatus` to `utils/backfillStatus.ts`**.
The same JSONata-mirroring logic now lives in one place. Both
`BackfillDetailPage` and `BackfillsListPage` import it. `isBackfillActive`
and `BACKFILL_TERMINAL_STATUSES` exported alongside.

**2. Status filter switches to client-side**. Was: list query took an
optional API filter (`status=running`, `status=failed`, etc) and the
backend filtered DDB. Problem: filtering by raw `status='completed'`
on the backend would EXCLUDE stuck-running backfills whose derived
status IS completed. So "Completed" tab in the UI would hide them.

New approach: list query always fetches with `null` filter (all
backfills), and the component filters client-side using
`computeDerivedBackfillStatus`. Backfills are infrequent (~hundreds
per pipeline per year), so the over-fetch is cheap.

**3. Inline Cancel button on list rows**. New rightmost column with an
`X` icon button, rendered only when `isBackfillActive(b)` is true.
Behavior identical to detail-page Cancel: `window.confirm`, then
`useCancelBackfillMutation`, toast feedback on success/error. Stops
event propagation so the row click (which navigates to detail) doesn't
also fire. Stuck-running backfills (raw=running, derived=completed)
get NO Cancel button, because `isBackfillActive` returns false for
them — same logic as the pill display.

#### Why not just fix the backend SFN

That's the right ultimate fix, but it requires log-trawling across
stuck SFN executions to find the pattern (timeouts? IAM revocations?
runaway retries?). Until we have that root cause, the client-side
derivation is the minimum-cost safety net. ADR #67 makes the same
argument; this ADR just extends the scope from one page to two.

#### Trade-off: API vs client-side filtering

Pros of client-side:
- Single source of truth for status semantics (`computeDerivedBackfillStatus`)
- Filter results match the displayed pills (no "this looks Completed
  but disappears from Completed tab")
- Stuck-status defense works for filtering, not just rendering

Cons:
- Over-fetch when user uses a filter (downloads all, then filters
  locally). With current volume this is negligible (~hundreds of
  records per pipeline per year). Revisit if list size grows past
  ~5000 records — at that point either paginate or add a derived
  status field to DDB updated by the SFN.

#### Tests

- New `utils/backfillStatus.test.ts` — 10 pure-function tests covering
  terminal-respected, stuck overrides (completed/partial/failed),
  skipped-counts-toward-processed, isBackfillActive truth table.
- `BackfillsListPage.test.tsx` — 5 new tests for stuck-running pill,
  Cancel button visibility (active/terminal/stuck), filter-includes-
  stuck-derived-completed scenario.
- `BackfillDetailPage.test.tsx` — unchanged (now uses shared util but
  behavior unchanged; existing 31 tests catch any regression).

#### Lesson

When applying a defensive heuristic to address a known backend bug,
DON'T apply it in just one place. The bug doesn't care which UI
surface is consuming the data. Either bake it into the data layer
(repo / hook) so every consumer sees the fix automatically, OR
extract it to a shared util that every consumer is expected to call.
This ADR picked the latter because the data layer (React Query
cache + types) didn't have a natural place to inject derived state
without leaking ADR-#67-specific logic into the hooks.

### 70 "Active" filter renamed to "Running"; narrowed to running-only (v0.78.13)

#### Context

The Backfills list page had filter chips: All / Active / Completed / Failed
/ Partial / Canceled. "Active" was a synthetic bucket meaning
`{running, pending}`.

User feedback: "active = running?" — Mike observed the two are effectively
synonymous from a user perspective, and asked to rename. Investigation
confirmed: `BackfillStatus.PENDING` is defined in backend constants but
the `bulk_backfill` SFN writes `status='running'` immediately on start,
so `pending` is theoretically possible but operationally never seen by
end users.

#### Decision

Rename the filter chip from "Active" to "Running", and narrow its
semantics to derived-running only (no longer matches `pending`).

Implementation:
- `STATUS_OPTIONS` now has `{ value: 'running', label: 'Running' }`
  instead of `{ value: 'active', label: 'Active' }`.
- `statusFilter` state type narrows from `BackfillStatus | 'active' | 'all'`
  to `BackfillStatus | 'all'` — no synthetic value.
- Filter logic simplifies to `computeDerivedBackfillStatus(b) === statusFilter`
  with no special case for active.

#### What about `pending`?

The backend constant `BackfillStatus.PENDING` stays — removing it would
be a larger backend change and the value MIGHT still be written in some
edge case (manual DDB update, future SFN restructure). The constant
remains for forward compatibility; the UI filter just doesn't show a
chip for it. If a `pending` backfill ever appears, it shows under "All"
but not under "Running" — the user sees an explicit pending status pill.

This trade-off mirrors a known frontend cleanup pattern: don't break
data contracts when removing UI affordances. Definition stays; surface
changes.

#### Why not include `pending` under "Running"

Mike's question implied "active = running" — that they're the same
thing. Users think binary: "is it running or not?". Hiding `pending`
under "Running" would re-introduce the same ambiguity in code (filter
matches two values, label says one). Cleaner: one chip = one status.
If `pending` becomes operationally meaningful later (e.g. SFN
restructure that genuinely lingers in pending), add a chip for it
then.

#### Tests

- `BackfillsListPage.test.tsx` — 2 new tests: "Running" chip present
  + "Active" chip gone; "Running" filter narrows to derived-running
  only (excluding stuck-running with derived=completed).

#### Lesson

When a synthetic filter bucket like "Active" gets questioned by users,
it's usually because the bucket abstracts over a distinction the user
doesn't care about. The fix isn't always "explain the abstraction
better" — it's "remove the abstraction." Direct status-to-chip mapping
is simpler to reason about, both for users and for the code.

### 71 ExecutionStatus case normalization (v0.78.14)

#### Context

`ExecutionStatus` (pipeline-execution status) was a mess in the frontend
type: `'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'TIMED_OUT' | 'ABORTED' |
'running' | 'succeeded' | 'success' | 'failed' | 'timed_out' | 'aborted' |
'stopped'` — twelve variants for what should be five+stopped statuses.

Root cause: AWS Step Functions `DescribeExecution` API returns status
in UPPERCASE (`'RUNNING'`, `'SUCCEEDED'`, ...). Internal Python code
and DDB storage use lowercase. Without a single normalization point
at the boundary, raw SFN values leak through certain code paths into
API responses, forcing the frontend type to accept both cases.
Additionally, some legacy code wrote `'success'` (the `TaskStatus`
form) instead of `'succeeded'` (the `ExecutionStatus` form), adding
yet another variant.

Two duplicate normalization maps existed:
- `SFN_STATUS_MAP` in `console_api/constants.py` (SFN → TaskStatus)
- A local `sfn_status_map` in `pipelines_list.py` (SFN → ExecutionStatus,
  collapsing `TIMED_OUT` → `'failed'`)

This ADR is Stage 2 of the multi-stage alignment plan (after ADR #70).

#### Decision

**1. One canonical helper** — `normalize_execution_status(status, log_warn=None)`
in `sam/lambdas/_shared/constants.py` (mirrored into
`console_api/constants.py` via `make sync-constants`).

```python
EXECUTION_STATUS_CANONICAL = {
    'running', 'succeeded', 'failed', 'timed_out', 'aborted', 'stopped',
}
```

Maps UPPERCASE SFN values, legacy `'success'`/`'SUCCESS'`, and returns
already-canonical values unchanged. Idempotent. Optional `log_warn`
callable for diagnostic warnings on unexpected values (CLAUDE.md hard
rule #4).

**2. Apply at boundary** (per Q2 = read + write):
- Replaced local `sfn_status_map` in `pipelines_list.py:_reconcile_running`
  with the helper. Removes the duplicate.
- Behavior change: `TIMED_OUT` previously collapsed to `'failed'`; now
  preserved as `'timed_out'`. Downstream "SFN-failed-but-tasks-resolved"
  recovery check updated from `if sfn_status == 'failed'` to
  `if sfn_status in {'failed', 'timed_out'}` to keep semantics.

**3. Narrow frontend type** —
```ts
export type ExecutionStatus =
  | 'running' | 'succeeded' | 'failed' | 'timed_out' | 'aborted' | 'stopped';
```

Removed UPPERCASE variants and the legacy `'success'`. TypeScript now
catches at compile time any code that still compares against UPPERCASE.

**4. Frontend defensive code updated**:
- `CalendarView.tsx` — removed UPPERCASE branches in the status→bucket
  mapping. Added `'timed_out'` → `'failed'` visual bucket.
- `asset-tabs/TabEvents.tsx` — kept accepting both forms because
  `evt.metadata.status` is free-form metadata (not ExecutionStatus
  itself, set by various SFN templates that haven't been migrated to
  canonical writes yet).

**5. Tests** — `tests/test_normalize_execution_status.py`:
18 tests covering UPPERCASE → canonical mapping, idempotence on each
canonical value, `'success'`/`'SUCCESS'` legacy mapping, `None` input,
unknown value handling (log + return original), and a lock test on
the canonical set so accidental drift is caught.

#### What this does NOT do

- **Does not migrate `SFN_STATUS_MAP`** (the SFN → TaskStatus mapping).
  Different domain — TaskStatus is for tasks within an execution,
  with values like `'waiting'`, `'deps_ready'`, etc. that don't exist
  in ExecutionStatus. Stage 4 (SSoT enums codegen, ADR TBD for v0.79.0)
  will deduplicate that map across files.
- **Does not normalize `evt.metadata.status` in asset events**.
  Free-form metadata, set by various SFN templates. Migrating those
  templates to write canonical values is a separate cleanup.
- **Does not collapse `'success'` vs `'succeeded'`** anywhere except
  ExecutionStatus normalization. Two statuses exist by design at
  different layers (TaskStatus uses `'success'`; ExecutionStatus uses
  `'succeeded'`). The normalizer only canonicalizes ExecutionStatus
  inputs that historically conflated them.

#### Trade-offs

- **Defense-in-depth normalization (read + write)** has a small CPU
  cost (one dict lookup per status). Negligible compared to DDB I/O.
- **Behavior change on `TIMED_OUT`**: was hidden inside `'failed'`,
  now surfaces as distinct. UI's "Failed" filter still includes both
  via `CalendarView` mapping; the new precision matters if anything
  downstream specifically wants to distinguish (alerts, retry logic).

#### Lesson

When a type union grows to 12 variants for what should be 5, the type
isn't doing its job — it's documenting historical confusion. The fix
isn't more variants; it's enforcing a contract at the boundary and
narrowing the type to match. TypeScript then becomes the enforcement
mechanism — any code still expecting UPPERCASE breaks at compile time,
which is exactly when you want to know.

### 72 SSoT enums codegen — single canonical source for status/rule enums (v0.79.0)

#### Context

Prior to v0.79.0, the same enum families lived as multiple hand-written
copies across the codebase (audit per v0.78.12 conversation):

| Family | Copies | Locations |
|---|---|---|
| TaskStatus | 4 | SDK + 2 Lambda + frontend |
| TriggerRule | 3 | SDK + 2 Lambda |
| BackfillStatus | 2 | console_api + frontend |
| BackfillCascade | 2 | console_api + frontend |
| BackfillGranularity | 2 | console_api + frontend |
| ExecutionStatus | 2 | (added v0.78.14) backend + frontend |
| PipelineStatus | 2 | console_api + frontend (loose) |
| StalenessStatus | 2 | both sides compute |

8 families, total 18+ copies. The existing `make sync-constants` only
checked backend↔backend; frontend drift was not caught. Real bugs from
this shape:
- ExecutionStatus mixed-case mess (closed by ADR #71 v0.78.14)
- "Active" → "Running" rename had to be coordinated in three places
  (closed by ADR #70 v0.78.13)
- A new enum value required editing 4 files and praying nothing was
  missed

This ADR establishes a single canonical source with a code generator.

#### Decision

**1. Canonical source: `polyris/constants.py`** (extended). Existing
`TaskStatus` (Enum), `TriggerRule` (class), and the various status sets
remain. Added (v0.79.0):
- `PipelineStatus`, `ExecutionStatus`, `BackfillStatus`, `BackfillCascade`,
  `BackfillGranularity`, `StalenessStatus`, `AssetOperator`
- `EXECUTION_STATUS_CANONICAL`, `BACKFILL_TERMINAL_STATUSES`,
  `BACKFILL_ACTIVE_STATUSES`
- `normalize_execution_status` (lifted from v0.78.14 Lambda code)

**2. Generator module: `polyris/codegen/sync_enums.py`**. Imports the
canonical Python module (not regex — handles Enum subclasses, derived
sets, inheritance), writes:
- `sam/lambdas/_shared/constants_generated.py` (Lambda shared, Python)
- `sam/lambdas/console_api/constants_generated.py` (Lambda, Python)
- `ui/src/generated/enums.ts` (TypeScript)

Each generated file has a banner `DO NOT EDIT — generated` with the
canonical SHA (first 16 chars). Idempotent: re-running with no source
changes produces identical bytes.

**3. CI drift gate: `make check-generate-enums`** (calls
`python -m polyris.codegen.sync_enums --check`). Exits 1 if any generated
file would change. CI runs this; PR can't merge if the developer edited
canonical without regenerating.

**4. Frontend fully migrated.** `ui/src/types/index.ts` no longer
defines `TaskStatus`/`TriggerRule`/`PipelineStatus`/`ExecutionStatus`/
`BackfillStatus`/`BackfillCascade`/`BackfillGranularity`/`StalenessStatus`
inline — re-exports them from `@/generated/enums`. All frontend code
uses the canonical types via this re-export.

**5. Backend pragmatic.** `sam/lambdas/console_api/constants.py` keeps
its hand-written enum classes (because they use class-level sets like
`TaskStatus.TERMINAL` at 8 sites). The generated module
`constants_generated.py` exists alongside. A unit test suite
`tests/test_enum_drift.py` (5 tests) asserts hand-written values agree
with canonical. Future v0.79.x will migrate the 8 sites and let the
hand-written disappear.

#### Why Python-import the canonical (not regex/AST-parse)

The canonical mixes `Enum` subclasses (TaskStatus), plain classes
(TriggerRule), and derived sets (`TERMINAL_STATUSES = {TaskStatus.SUCCESS.value, ...}`).
Re-implementing all that resolution in a regex/AST is a 2nd canonical
to drift from. Python's import machinery already does it correctly —
the generator just `from polyris import constants as src` and reads
the resolved attributes.

#### Why class-level (not enum.Enum) for generated Python

Lambda code expects bare strings: `if status == TaskStatus.RUNNING:`.
Using `enum.Enum` would force `.value` access. Class-level string
attributes match existing patterns and require no caller changes.

#### What this does NOT do (yet)

- **Does not migrate backend hand-written `TaskStatus` class to
  generated.** Class-level sets (`TaskStatus.TERMINAL`) used at 8
  sites. Migration is incremental — drift test catches divergence
  meanwhile.
- **Does not generate SFN templates.** Those embed status values
  ('running', 'SUCCEEDED' for SFN-internal Choice rules) but they're
  Jsonata-templated and would need a different generator. BACKLOG.
- **Does not generate from non-Python canonicals.** If JSON Schema or
  similar becomes the source for some types, that's another generator.

#### Tests

- `tests/test_normalize_execution_status.py` — already added v0.78.14
- `tests/test_enum_drift.py` (NEW, 5 tests):
  - TaskStatus backend values ⊆ canonical values
  - TriggerRule backend values == canonical values (strict)
  - BackfillStatus backend values == canonical values (strict)
  - Generator output in sync with canonical (re-runs `check_all()`)
  - `normalize_execution_status` behavior matches between backend
    copy and canonical (8 input cases tested)

#### CI integration

`.github/workflows/ci.yml` should call `make check-generate-enums`
(not changed in this PR — add when CI is touched next).
For local: `make generate-enums` to regenerate, or
`python -m polyris.codegen.sync_enums`.

#### Lesson

When multiple copies of a value drift, the fix isn't a stricter review
discipline ("remember to update all 4 files!") — it's removing the
multiplicity. Codegen does this: edit one, regenerate, files match by
construction. The drift test then becomes the contract enforcement
mechanism, not human attention.

### 73 Computation moved to backend — derived status + per-partition aggregation (v0.79.1)

#### Context

Stage 5 of the multi-release alignment plan. The audit completed in
the v0.78.12 conversation found three places where frontend derived
values from raw fields:

| Field | Where computed (before) |
|---|---|
| Backfill derived status (stuck-SFN override) | Frontend only (`utils/backfillStatus.ts`) |
| Per-partition aggregate status | Frontend only (`summarizePartition` in `BackfillDetailPage`) |
| Asset staleness (picker-scoped) | Frontend (`utils/staleness.ts`); backend separately computes its own |

Frontend-only derivation breaks when a second client appears (CLI,
mobile, webhook). Same logic re-implemented in each = drift risk.
This ADR moves backfill derivation to the backend per Mike's
alignment Q7/Q9.

(Q8 — `utils/staleness.ts` — handled differently. See "What this does
NOT do" below.)

#### Decision

**1. Server-side derived backfill status.**
`_compute_derived_backfill_status(item)` in `routes/backfill.py`.
Mirrors the (now-deleted) frontend `computeDerivedBackfillStatus`:

  - Terminal raw status → respected
  - `running`/`pending` + all partitions accounted for → derive
    `completed`/`failed`/`partial` from completed/failed ratio
  - Otherwise → raw

Applied in `_format_backfill_summary` (so it covers list + detail +
retry-chain links). The returned `status` field IS the derived value;
**raw is no longer exposed in API responses** (per Q7). If raw and
derived diverge, the helper logs at `warn` level with full context
(`backfill_id`, `raw_status`, `derived_status`) for CloudWatch
diagnosis of stuck SFNs.

**2. Server-side per-partition aggregate.**
`_summarize_partition_status(children, partition_key)` in
`routes/backfill.py`. Mirrors the frontend `summarizePartition`:

  - No children for this key → `pending`
  - All success → `success`
  - Any failure/upstream_failed/aborted → `failed`
  - Otherwise → `running`

Detail response gains a new field (per Q9):
```json
"partitions": [
    {"key": "2024-01-15", "status": "success"},
    {"key": "2024-01-16", "status": "failed"},
    ...
]
```

The frontend `BackfillDetailPage.partitionBadges` now reads
`detail.partitions` directly (with a legacy fallback to
`partition_keys` mapped to `pending` for cases where backend hasn't
redeployed yet).

**3. Frontend cleanup.**
- Deleted `ui/src/utils/backfillStatus.ts` and its test file.
- `BackfillsListPage` reads `b.status` directly (no client-side
  override).
- `BackfillDetailPage` reads `detail.status` and `detail.partitions`
  directly. The previous diagnostic `console.warn` for raw≠derived
  divergence is gone — that's now a `log.warn` in CloudWatch where
  ops people actually look.
- `isBackfillActive` becomes a tiny inline check against
  `BACKFILL_TERMINAL_STATUSES` (sourced from `@/generated/enums`,
  per ADR #72).

#### What this does NOT do

- **Does not delete `utils/staleness.ts`** as originally planned in
  the Q8 answer. On closer review the file has picker-scoped
  resolution logic (recent-events for the visible date range,
  fallback to backend-computed `last_updated` otherwise) that the
  backend doesn't replicate — backend computes staleness from a
  single timestamp without picker context. Deleting the util would
  lose the picker-scoped behavior. The original "delete entirely"
  recommendation was based on a shallower read of the file.
- **Does not migrate SFN templates.** The Finalize step in
  `bulk_backfill/sfn.tpl.json` writes the terminal status from
  JSONata, and that's a fine source of truth — it's the
  *stuck-without-Finalize* case the override catches. Templates stay.
- **Does not add per-partition retry/cancel endpoints.** That's
  Stage 6 (v0.79.2).

#### Tests

Backend (`tests/routes/test_backfill.py`, +15):

- `TestDerivedBackfillStatus` (8 tests):
  - Terminal status respected (per each terminal value)
  - Running + all completed → 'completed'
  - Running + all failed → 'failed'
  - Running + mixed → 'partial'
  - Running + some pending → 'running' (untouched)
  - `skipped` counts toward processed
  - Pending + no partitions → 'pending'
  - String-coerced int inputs (DDB returns Decimal sometimes)
- `TestSummarizePartitionStatus` (7 tests):
  - No children → 'pending'
  - All success → 'success'
  - Any failed/upstream_failed/aborted → 'failed'
  - In-flight (running) when not all done
  - Partition_key filter isolates correctly

Frontend tests rewritten for the backend-trust contract:

- `BackfillDetailPage.test.tsx` — `derivedStatus` describe block
  replaced with `status rendering trusts backend` (5 tests). Mocks
  provide the derived value (matches what backend returns).
- `BackfillsListPage.test.tsx` — stuck-running tests replaced with
  the backend-trust equivalents (same 5 tests, semantics aligned).

#### Lesson

When the same computation lives in N places, the fix isn't "find a
clever shared util" — that just hides the multiplicity at the
language level. The fix is to put the computation behind the API
boundary, so every client (including ones that don't exist yet)
gets the same answer for free. Frontend becomes a renderer, not a
deriver.

Equally important: when planning, "delete the duplicate util" sounds
clean, but you have to actually open the file. Q8 looked obvious in
the abstract; the real `staleness.ts` had picker-scoped logic the
backend doesn't replicate. Walking through the code before deleting
is part of the work, not optional rigor.

### 74 Per-partition retry-failed (v0.79.2)

#### Context

Stage 6 of the multi-release alignment plan. The user-facing problem:
when a backfill ends in 'partial' (some partitions failed), the only
action is "retry all failed". For a 30-day backfill where 2 dates
failed, the user has to either retry all 28 untouched failures
together or manually start a new ad-hoc backfill with just those 2.

This ADR adds **per-partition retry** by extending the existing
`/api/backfills/{id}/retry-failed` endpoint with an optional
`partition_keys` filter.

#### Decision

**1. Extended endpoint, NOT a new one.** Per the plan: "Extension of
existing cancel + retry_failed endpoints, not new routes."

```http
POST /api/backfills/{id}/retry-failed
{
  "partition_keys": ["2024-01-15", "2024-01-16"]   // optional
}
```

- **Body omitted or empty array** → retry ALL currently-failed
  partitions (v0.78.11 behavior, backward-compatible).
- **`partition_keys` provided** → retry only the intersection of
  caller's keys and actually-failed partitions.
- **Caller-provided key NOT in failed set** → `422
  partition_keys_not_failed` with the actual failed list in the
  response (so the UI can surface stale state).

**2. Strict validation.** Silent intersection (just compute
`intersect(requested, failed)` and proceed) would hide client bugs
— stale UI showing failures that backend has since cleaned up,
or a typo in the partition key. The endpoint rejects with 422
when any requested key isn't actually failed.

**3. Frontend affordance: tiny ↻ button on failed cells.**
Appears at top-right of each `failed`-status partition cell in the
heatmap. Clicking:
- Stops event propagation (doesn't open DAG)
- Confirms via `window.confirm`
- POSTs with `partition_keys: [key]`
- On success: toast + `onRetryStarted(newBackfillId)` callback
- On 422 or other error: toast with error message

#### What this does NOT do (deferred)

- **Per-partition CANCEL.** Cancel mid-flight needs SFN template
  changes — the bulk_backfill SFN already checks a backfill-level
  cancel flag at each Map iteration, but adding per-partition skip
  flags requires additional DDB state and JSONata logic in the SFN
  template. Significant risk in a single release. BACKLOG.
- **Per-partition SKIP.** Same issue as cancel — needs SFN template
  changes. BACKLOG.
- **Multi-partition select in UI.** No checkbox column on the heatmap
  for batch select of "these 3 partitions". The single-partition
  button covers the most common ask (one specific failure that the
  user has a fix for). Batch select is a UI-design item separate
  from this ADR.

#### Why "strict" instead of silent intersect

Three reasons:

1. **Stale UI catch.** If the user's tab has been idle and the
   underlying backfill changed (someone else retried, or retry
   succeeded), the UI might offer "retry partition X" when X is no
   longer failed. Silent intersect would do nothing; strict 422
   tells the user what happened so they can refresh.

2. **Typo catch.** Per-partition retry might end up scriptable
   (e.g. a CLI command). Silent intersect would silently no-op on
   typo. 422 surfaces the mistake.

3. **Diagnostic in response body.** Returning the actual failed list
   alongside the error lets the client offer "retry these instead?"
   without a second roundtrip.

#### Tests

Backend (`tests/routes/test_backfill.py`, +7 in `TestRetryFailedWithPartitionKeys`):

- Empty body retries all failed (backward-compat)
- Subset partition_keys retries only those
- Unknown partition_key → 422 with `failed_partitions` in body
- Non-array `partition_keys` → 422 `invalid_partition_keys`
- Malformed JSON body → 422 `malformed_body`
- Empty array == no filter (treated as "retry all")
- Duplicate keys deduplicated (set semantics)

Frontend (`hooks/queries/useBackfillQueries.test.ts`, +3 in
`useRetryFailedBackfillMutation`):

- Backward-compat: bare `mutateAsync('bf-id')` works, sends empty body
- New shape `{backfillId, partitionKeys: [...]}` sends `partition_keys`
- Empty array → omitted from body (parallels backend treatment)

UI behavior tests for the ↻ button per-partition retry are covered
implicitly via existing BackfillDetailPage tests + the mutation
contract test. A dedicated click-through test would be valuable but
involves modal/confirm interception not yet abstracted in the test
harness — left as a follow-up.

#### Lesson

Adding an optional filter to an existing endpoint is almost always
cleaner than minting a new endpoint. New URLs need API versioning
thinking, client compat shims, and route discovery — none of which
helps the user. The trade-off is being strict about validation, so
the optional parameter doesn't quietly do the wrong thing.

Per CLAUDE.md #4 (visibility of errors): rejecting unknown partition
keys with a diagnostic 422 instead of silently intersecting is the
right default. Silent-by-default is what created the v0.78.12 stuck-
running bug — defensive code at one layer hiding what's actually
wrong upstream.

### 75 DAL repository pattern for all 4 helper Lambdas (v0.79.3)

#### Context

Stage 7 of the multi-release alignment plan. The v0.78.12 architectural
audit found that the CLAUDE.md rule "DAL repository pattern for all
DynamoDB access" was honored by only 1 of 6 Lambdas (`console_api`).
The other helper Lambdas accessed DDB via raw boto3:

| Lambda | Raw boto3 calls before |
|---|---|
| `evaluate_deps` | 3 (batch_get + 2 get_item paths) |
| `notify_asset_subscribers` | 4 (query × 2, delete_item, put_item) |
| `check_assets` | 5 (query × 2, get_latest, put_item, delete_item) |
| `query_subscriptions` | 2 (paginated query) |
| `ui_bootstrap` | 0 (no DB access) |

Mike's Q3 = A ("all under DAL, є мінуси хіба?") confirmed the
direction. This ADR is the migration.

#### Decision

**1. Per-Lambda DAL directory, not a shared module.**
Each helper Lambda gets its own `dal/__init__.py` (or `qs_dal/__init__.py`
for query_subscriptions — see naming caveat below). The repos are:

| Lambda | DAL repos | Methods |
|---|---|---|
| `evaluate_deps` | `TokensRepo` | `batch_get_statuses`, `get_status_one`, `is_paused` |
| `notify_asset_subscribers` | `SubscriptionsRepo`, `AssetEventsRepo` | `list_for_asset`, `delete`, `query_recent` |
| `check_assets` | `AssetEventsRepo`, `SubscriptionsRepo` | `query_recent`, `get_latest`, `put_asset_subscription`, `delete` |
| `query_subscriptions` | `SubscriptionsRepo` | `list_for_dependency` |

**Why per-Lambda not shared.** Lambda deploy packages are independent —
sharing code requires either a Lambda Layer or copy-on-build. The
`_shared/constants.py` pattern uses copy via `make sync-constants`,
but DAL repos are larger and Lambda-specific (each Lambda accesses
different tables with different access patterns). Duplication here
is genuine: tiny `_resource()` boilerplate appears 3× across the four
Lambdas, but the table-specific repos are not shared.

**2. Naming caveat: `query_subscriptions/qs_dal/`** (not `dal/`).
The backend test conftest puts `sam/lambdas/console_api` on
`sys.path`, which imports console_api's `dal` package (a directory
with submodule files like `subscriptions_repo.py`). When pytest then
imports `query_subscriptions/index.py`, its `from dal import
subscriptions_repo` resolves to console_api's `dal` — wrong package
shape, no module-level `subscriptions_repo` singleton.

Three options were considered:
- (a) Evict `dal` from `sys.modules` in the test file — broke 50
  unrelated console_api backend tests that re-import dal lazily.
- (b) Use absolute imports `from sam.lambdas.query_subscriptions.dal
  import ...` — works in tests but fails at Lambda runtime where
  `sam.lambdas` isn't on the path.
- (c) Rename the package: `dal` → `qs_dal`. Removes the collision
  cleanly at both test time and runtime.

**Chose (c).** Other Lambdas keep `dal/` because their test files run
from their own directory with their own path, no console_api
shadowing. Only the SDK-side `tests/backend/test_query_subscriptions.py`
suffers the collision; renaming the package surface is cheaper than
fighting sys.path.

**3. Test pattern.** Existing tests patched `_get_dynamodb` and built
boto3 mock chains (`mock_dynamodb.Table().query.return_value = ...`).
The DAL pattern simplifies this to `mocker.patch.object(repo, 'method',
return_value=[...])`. Migration was mostly mechanical:
- evaluate_deps: rewrote 6 handler tests + dropped `_get_dynamodb`
  import. 56 tests pass.
- notify_asset_subscribers: 9 patches transformed via script;
  3 consecutive-check tests needed per-table mocks
  (`mock_sub_table` + `mock_events_table`) wired into the right
  repo. 12 tests pass.
- check_assets: 14 patches transformed; same pattern. 19 tests pass.
- query_subscriptions: 7 new Lambda-side tests + 7 SDK-side tests
  rewritten. 11 + 7 pass.

**4. `_get_dynamodb()` removed** from index.py in evaluate_deps,
notify_asset_subscribers, and check_assets. The lazy-init logic
moved to the DAL's `_resource()` (module-private). Anything that
still imported `_get_dynamodb` would break at import time — the
test migration caught this.

**5. SFN client init stays inline.** In notify_asset_subscribers,
`_get_sfn()` stays as-is — single Step Functions client, no
repository-shaped abstraction needed. DAL pattern is for
DynamoDB; SFN is a different service. Symmetry isn't a rule
worth honoring at the cost of overengineering.

#### What this does NOT do

- **Does not migrate `console_api/dal/`** — that's already done.
- **Does not introduce a shared DAL layer.** Each Lambda's DAL is
  independent. If three Lambdas grow to query the same table with
  the same access pattern, that's the time to extract — not before.
- **Does not migrate SFN/EventBridge clients to repos.** Those
  remain inline `boto3.client('stepfunctions')` and similar.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| SDK Python | 900 | **901** | +1 (test_query_subscriptions has 7 tests but 6 were renames, 1 net new test for AccessDenied) |
| console_api | 348 | **348** | 0 |
| evaluate_deps | 56 | **56** | 0 (rewrote 6 in place) |
| notify_asset_subscribers | 12 | **12** | 0 (rewrote 9 in place) |
| check_assets | 19 | **19** | 0 (rewrote 14 in place) |
| query_subscriptions Lambda | (new) | **11** | +11 (new suite) |
| **Python total** | **1335** | **1347** | **+12** |
| vitest | 830 | 830 | 0 |
| **All gates** | green | **green** | — |

#### Trade-offs

**Slight cold-start cost.** Each migrated Lambda now imports its
`dal/` package on cold start — a few extra ms. Negligible compared
to boto3's own init time. The benefit is testability and explicit
contracts.

**Per-Lambda DAL duplicates `_resource()` 3×.** Eight lines per
Lambda. Could be eliminated by introducing `sam/lambdas/_shared/dal_base/`
copied via make target like `_shared/constants.py`. Decision: not
yet. Three copies of eight lines is below the threshold where SSoT
infrastructure pays for itself.

#### Lesson

When a project rule applies but exceptions exist, the right question
isn't "is the exception OK?" — it's "can we close it cleanly?". The
2-5 raw boto3 calls per Lambda looked too small to justify
restructuring; the migration showed each one wrapped a real concern
(pagination, fallback, batching). Lifting them behind a repo
revealed the structure that was already there, just hidden in
inline code. Tests got 10× shorter because they could now express
intent (`mock_repo.method.return_value = [...]`) instead of
mechanism (`mock_dynamodb.Table().query.return_value = {'Items': ...}`).

The naming collision (qs_dal) is also a lesson: test infrastructure
that aggressively manipulates sys.path can shadow your packages in
non-obvious ways. Picking a unique name is cheaper than fighting it.

### 76 print() → structured logger in all helper Lambdas (v0.79.4)

#### Context

Per the v0.78.10 Philosophy compliance audit (CLAUDE.md "12-Factor App
logs paragraph"): 4 helper Lambdas — `evaluate_deps`,
`notify_asset_subscribers`, `check_assets`, `query_subscriptions` —
used `print()` for diagnostics, scattered across ~21 sites. CloudWatch
captures them, but unstructured: no level field, no consistent
function name, hard to filter via CloudWatch Insights.

Console API has had `from logger import log` (structured JSON) since
v0.68. The helper Lambdas were carrying this gap because each was
small enough that print() felt sufficient.

#### Decision

**1. Lift `logger.py` to `sam/lambdas/_shared/logger.py`** as canonical.
Identical content to the previous `console_api/logger.py`; one
module class (`_Logger`) with `info` / `warn` / `error` methods.

**2. Copy to each Lambda** at deploy time via the same pattern as
`_shared/constants.py`. Added `make sync-loggers` target with drift
detection — runs in CI and locally; fails if any Lambda's `logger.py`
differs from the canonical.

**3. Migrate ~21 `print(...)` sites** to structured calls:

```python
# Before
print(f"[evaluate_deps] Unhandled error: {type(e).__name__}: {e}")

# After
log.error("handler", "Unhandled error",
          error_type=type(e).__name__, error=str(e))
```

CloudWatch Insights queries now work:
```
filter level = "WARN" and fn = "_check_freshness"
filter level = "ERROR" and fn = "_check_asset"
stats count(*) by level, fn
```

#### Per-Lambda site counts

| Lambda | print() before | After |
|---|---|---|
| evaluate_deps | 3 | 0 |
| notify_asset_subscribers | 9 | 0 |
| check_assets | 5 | 0 |
| query_subscriptions | 4 | 0 |
| **Total** | **21** | **0** |

#### Why copy-per-Lambda instead of shared at runtime

Same reason as `_shared/constants.py`: Lambda deploy packages are
independent. Each Lambda needs `logger.py` in its own deploy
artifact. `make sync-loggers` enforces canonical-source discipline;
divergence fails CI before merge.

#### What this does NOT do

- **Doesn't touch console_api's `logger.py`** functionally — it was
  already canonical. Now copied from `_shared/logger.py` (same
  content), enforced by `make sync-loggers`.
- **Doesn't change log shape.** Still JSON one-line-per-event,
  consumed by CloudWatch Logs as-is.
- **Doesn't add log levels filter env variable.** All levels emitted
  by default; CloudWatch retention policy handles volume.

#### Tests

No new tests for this release — `print() → log.X()` is a pure
substitution at every site, all existing tests continue to pass.
Tests do not assert on log output; that would couple test bodies
to log message strings, which is an anti-pattern.

| Suite | Was | Now | Δ |
|---|---|---|---|
| Python total | 1347 | **1347** | 0 |
| vitest | 830 | 830 | 0 |
| **All gates** | green | **green** | — |

#### Lesson

Closing a Philosophy compliance gap doesn't require a feature
release. A small dedicated cleanup release with one principle's
worth of changes is the right unit. The sync-loggers target follows
the same SSoT pattern as sync-constants — drift catches creep that
manual review would miss.

The 21 sites looked small enough that print() "felt sufficient"
when each Lambda was first written. Adding structured logging
later costs the same as adding it at the start, plus one
incremental release. The lesson is to start with structured
logging on day one; the marginal cost is roughly zero.

### 77 TaskStatus class-level sets → generated module-level (v0.79.5)

#### Context

When ADR #72 (SSoT enums codegen, v0.79.0) consolidated 8 enum
families, the backend's hand-written `TaskStatus` class kept its
class-level sets (`TERMINAL`, `SUCCESS_STATES`, `FAILURE_STATES`,
`ACTIVE`, `WAITING_STATES`, `STOPPABLE`) intact because 8 call sites
relied on `TaskStatus.TERMINAL` etc. access patterns. The generator
emitted module-level equivalents (`TASK_TERMINAL_STATUSES` etc.) but
backend code didn't use them.

ADR #72 documented this explicitly: "Future v0.79.x will migrate the
8 sites and let the hand-written disappear." This is that release.

#### Decision

**1. Remove class-level sets** from the hand-written `TaskStatus`
class in both `_shared/constants.py` and `console_api/constants.py`.
The class keeps only the string attributes (WAITING, RUNNING, etc.).

**2. Re-export generated sets at module level** so existing import
paths still work:

```python
# At end of console_api/constants.py:
from constants_generated import (
    TASK_TERMINAL_STATUSES,
    TASK_SUCCESS_STATUSES,
    TASK_FAILURE_STATUSES,
    TASK_ACTIVE_STATUSES,
    TASK_WAITING_STATUSES,
    TASK_STOPPABLE_STATUSES,
)
```

`from constants import TASK_TERMINAL_STATUSES` now works without
touching `constants_generated` directly.

**3. Migrate 8 call sites**:

| File | Before | After |
|---|---|---|
| `evaluate_deps/index.py` | `TaskStatus.SUCCESS_STATES` | `TASK_SUCCESS_STATUSES` |
| | `TaskStatus.FAILURE_STATES` | `TASK_FAILURE_STATUSES` |
| | `TaskStatus.TERMINAL` | `TASK_TERMINAL_STATUSES` |
| `console_api/task_actions.py` | `TaskStatus.TERMINAL` (× 2) | `TASK_TERMINAL_STATUSES` |
| `console_api/routes/executions.py` | `TaskStatus.TERMINAL` | `TASK_TERMINAL_STATUSES` |
| `console_api/routes/tasks.py` | `TaskStatus.WAITING_STATES` | `TASK_WAITING_STATUSES` |

**4. Extend codegen targets to evaluate_deps.** Per ADR #72 the
generator wrote to `_shared/` + `console_api/`. evaluate_deps imports
from its synced `constants.py` (copy of `_shared/`), and that file
now does `from constants_generated import ...`. So evaluate_deps's
deploy package needs `constants_generated.py` too. Added a third
PY_TARGETS entry.

**5. `_shared/constants.py` keeps fallback constants** inside a
`try/except ImportError`. If a future Lambda copies `_shared/constants.py`
without also pulling `constants_generated.py`, it still gets working
sets (values inline-duplicated from canonical). The drift test
(`test_enum_drift.py`) catches divergence.

#### What changed about the canonical sets

The canonical `polyris.constants.TERMINAL_STATUSES` includes both
`'success'` (legacy) and `'succeeded'` (current). The
old class-level `TaskStatus.TERMINAL` in backend only had `'success'`.
After migration, `TASK_TERMINAL_STATUSES` now includes both forms.

**Behavior impact:** any code checking `status in TASK_TERMINAL_STATUSES`
will now treat `'succeeded'` as terminal where previously it might
not have. This is the correct direction — the SDK already documented
`succeeded` as an alias for `success` (migration compat), so callers
that didn't recognize it were silently buggy. Tests updated to
reflect the new set membership.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| SDK Python | 901 | 901 | 0 (2 smoke tests updated in place) |
| console_api | 348 | 348 | 0 |
| evaluate_deps | 56 | **56** | 0 (3 TestStatusCategories tests updated for canonical sets) |
| Other Lambda totals | unchanged | unchanged | 0 |
| **Python total** | **1347** | **1347** | 0 |
| vitest | 830 | 830 | 0 |
| **All gates** | green | **green** | — |

#### Two updated tests

- `tests/sdk/test_smoke.py::test_trigger_rule_sync` — checked for
  literal `'FAILURE_STATES'` string in constants.py source. Updated
  to check `'TASK_FAILURE_STATUSES'`.
- `tests/sdk/test_smoke.py::test_notify_dependents_deps_blocked` —
  same pattern, same fix.
- `evaluate_deps/test_evaluate_deps.py::TestStatusCategories` —
  3 tests updated; sets now include `'succeeded'` per canonical.

#### What this does NOT do

- **`AssetOperator`, `BackfillStatus`, etc. classes still have
  module-level sets in canonical only.** They never had class-level
  sets in backend. No migration needed.
- **Frontend already used generated.** Per ADR #72, frontend is
  fully SSoT. This release is purely backend cleanup.

#### Lesson

When ADR #72 deliberately left this migration for later, the
reasoning was "8 sites is real touch surface." Doing it 5 releases
later, the actual migration was ~15 minutes of mechanical
substitution + 3 trivial test updates. The cost was lower than
predicted because by v0.79.5 the codegen infrastructure was solid
and the call sites were all in files we'd touched recently.

Deferring mechanical cleanup is cheap when the deferral is honest —
documented in an ADR, tracked in a release plan, with a defined
trigger ("when v0.79.x lands"). Deferring it because "we don't have
time" is how technical debt accumulates without a paydown date.

### 78 SFN template drift checker (v0.79.6)

#### Context

ADR #72 (v0.79.0) brought SSoT enum codegen for Python and TypeScript.
SFN template `.tpl.json` files were the remaining gap — they contain
status string literals like `"failed"`, `"upstream_failed"`,
`"deps_ready"` embedded in JSONata expressions and DDB
ExpressionAttributeValues. A rename of `TaskStatus.UPSTREAM_FAILED` in
canonical would break templates silently.

Mike's BACKLOG item: "SFN template literals → canonical via окремий
generator". On investigation, mechanical substitution into JSONata
expressions like `$status = "failed"` would either:
- break JSONata readability (`$status = "{{TASK_STATUS_FAILED}}"`),
- require a custom template processor in the SAM deploy pipeline,
- or both.

The cheaper intervention that achieves the same SSoT goal is
**validation, not generation**.

#### Decision

**Build a drift checker, not a substitution generator.**

`polyris/codegen/check_sfn_templates.py` scans all
`sam/sfn_templates/**/*.json` for status string literals in two
patterns and validates each against canonical:

1. **Pass-state status writes:**
   `"status": "<value>"`
2. **DDB status updates:**
   `":status": {"S": "<value>"}` (and common name variants like
   `:newstatus`, `:s`)

If any value isn't in `polyris.constants.TaskStatus`, the checker
fails and prints the file/line. Run via `make check-sfn-templates`
locally and in CI.

#### Why drift-check beats substitution

| Aspect | Substitution generator | Drift checker |
|---|---|---|
| **Template readability** | Loses (placeholders) | Keeps (real values) |
| **SAM/JSONata compatibility** | Complex (custom pre-processor) | None needed |
| **Catches typos** | ✅ | ✅ (typos in any literal) |
| **Catches removed canonical values** | ✅ | ✅ |
| **Catches values added without canonical** | ✅ | ✅ |
| **Implementation cost** | ~1 day | ~2 hours |
| **Runtime cost** | Build step | CI gate only |

The drift checker delivers identical guarantees with lower complexity
because templates are read by humans far more often than they're
generated.

#### Allowlists

Two narrow exemption sets:

1. **`ALLOWLIST`** — common JSON field names that pass status-shape
   but aren't statuses: `pass`, `task`, `choice`, `name`, `value`, etc.
   These shouldn't be flagged even if they appear in status-like
   contexts.

2. **`HELPER_OPERATION_STATUSES`** — operation-result values returned
   by helper Lambdas as Pass-state Output. Currently:
   - `restarted` — `restart_task` helper signals successful restart.

Adding to either allowlist requires explicit justification in code
comments — these are exceptions to canonical enforcement and must
have a documented reason.

#### Pattern scope (deliberate narrowness)

- The DDB AV check is scoped to `:status` / `:newstatus` / `:s`
  attribute placeholders only. Other attribute names (`:nf` for
  `notification_failed`, `:lu` for `last_updated`, etc.) carry
  diagnostic data with no canonical contract.
- JSONata comparisons inside `{% ... %}` blocks have JSON-escaped
  quotes that defeat regex matching. The Pass-state and DDB-AV
  checks cover the surfaces where status writes actually appear.
- Tested coverage: 12/14 canonical TaskStatus values appear in some
  template; `pending` and `succeeded` don't, which is fine — those
  values come from backend Lambda writes, not SFN-direct DDB writes.

#### Tests

11 unit tests in `tests/sdk/test_sfn_template_drift.py`:
- 2 canonical loader tests (sanity).
- 2 allowlist shape tests.
- 1 real-templates-pass test (the committed templates must always pass).
- 6 synthetic-template detection tests (typos, valid values,
  allowlist behavior, DDB pattern detection, non-status DDB
  attribute ignoring).

| Suite | Was | Now | Δ |
|---|---|---|---|
| SDK Python | 901 | **912** | +11 |
| **All gates** | green | **green** | — |

#### What this does NOT do

- **Doesn't generate templates.** Authors still hand-edit `.tpl.json`
  files. The checker catches divergence, doesn't enforce structure.
- **Doesn't check non-status string literals.** Trigger rule names,
  trigger condition keywords, etc. are not validated. The Python
  evaluator handles those; SFN templates rarely embed them.
- **Doesn't fix detected drift automatically.** Failing the CI gate
  forces a human decision: was this an intentional canonical change,
  or a typo?

#### Lesson

When the BACKLOG says "generator", the right first question is "what
does generation give us that validation doesn't?" In this case:
nothing. The substitution would have been pure ceremony — the
templates would still be hand-edited at the placeholder layer, just
with worse readability. The validator catches the same class of
mistakes at lower cost.

"Generator" in the Polyris vocabulary now has two flavors:
- **Codegen** (sync_enums): produces files from canonical, drift
  check ensures they stay in sync.
- **Drift checker** (check_sfn_templates): scans hand-authored
  files for compatibility with canonical.

Both serve SSoT. The codegen flavor wins when the output is
mechanical (TypeScript enums); the drift-check flavor wins when
the output is hand-authored (JSONata expressions).

### 79 Backfill icon single source of truth (v0.79.7)

#### Context

The backfill affordance appeared across the console with three different
icons, chosen ad-hoc per component:

| Surface | Icon before |
|---|---|
| Nav "Backfills" tab (Header) | Rocket |
| Backfills list page header | Rocket |
| Backfill modal header (cell/asset) | Rocket |
| Backfill detail page header | Rocket |
| Backfill badge in All Runs | Rocket |
| Backfill notification (item + toast) | Rocket |
| "Backfill" button on pipeline DAG | Rewind |
| Help legend backfill entries (×2) | History |

Even the centralized `utils/icons.tsx` carried the inconsistency:
`ActionIcons.backfill = Rewind` but `ContextIcons.backfill = History`.
Ten call sites across eight files, three icons, no single owner.

Mike flagged it from the UI: the nav tab (rocket) and the DAG button
(rewind `«`) didn't match, and the modals used a third style. He chose
**Rewind** as the canonical backfill icon.

#### Decision

**1. One definition.** `ActionIcons.backfill` (in `utils/icons.tsx`) is
THE backfill icon. Set to `Rewind` per Mike's choice. A comment marks
it as canonical: every backfill affordance renders this; change it here
and it changes everywhere.

**2. ContextIcons aliases ActionIcons.** `ContextIcons.backfill` no
longer hardcodes a lucide component — it references
`ActionIcons.backfill`:

```ts
export const ContextIcons = {
    ...
    backfill: ActionIcons.backfill,  // single source, not History
    ...
};
```

This removes the second, divergent definition. ActionIcons is declared
before ContextIcons in the module, so the reference resolves cleanly.

**3. All 10 sites consume the indirection.** No component imports a raw
`Rocket` / `Rewind` / `History` for backfill anymore. They use
`ActionIcons.backfill` (or `ContextIcons.backfill` in the help legend,
which is semantically a "context section" icon and aliases the same
component). Components that imported the icon directly from `lucide-react`
(e.g. `Header.tsx`) now import `ActionIcons` from `@/utils/icons`.

**4. Dead code removed.** After the migration, the `History` lucide icon
was no longer referenced anywhere (the `Clock`-based "History" text label
in TaskDetailModal is unrelated). Its import and re-export were removed
from `utils/icons.tsx` per the no-dead-code principle.

#### What did NOT change

- **`Rocket` stays** for its real meaning: `ActionIcons.run`,
  `ElementIcons.dagTrigger`, the Run button, run notifications, the
  run-action help legend entry, and DAG-trigger nodes in the lineage
  flow. Rocket = "launch / run", which is the opposite of backfill —
  conflating them was the original bug.
- **`Rewind` is now backfill-only**, owned by `ActionIcons.backfill`.

#### Guard test

`utils/icons.test.tsx` (3 tests) locks the invariant:
- `ActionIcons.backfill` is defined.
- `ContextIcons.backfill === ActionIcons.backfill` (same reference —
  proves the alias, catches re-divergence).
- `ActionIcons.backfill !== ActionIcons.run` (catches the most common
  pre-v0.79.7 mistake of collapsing backfill back into Rocket).

If a future edit hardcodes a different backfill icon in either map, the
`toBe` reference check fails immediately.

#### Test mock update

Component test mocks for `@/utils/icons` previously stubbed
`ActionIcons: () => null` (a function). Code now does
`ActionIcons.backfill`, so the mock had to be an object. Replaced with
`new Proxy({}, { get: () => () => null })` — any icon key resolves to a
null-rendering component, future-proof against new keys. Applied across
15 (`ActionIcons`) + 14 (`ContextIcons`) test files. HelpModal's mock
gained a `ContextIcons` entry it previously lacked.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| vitest | 830 | **833** | +3 (icon SSoT guard) |
| Python total | 1358 | 1358 | 0 |
| tsc strict | 0 errors | **0 errors** | — |
| **All gates** | green | **green** | — |

#### Lesson

"Make the icon consistent" looked like a find-and-replace, but the real
fix was structural: the inconsistency existed because there was no single
owner. Replacing ten hardcoded icons with ten hardcoded copies of the
same icon would have re-drifted the moment someone touched one file. The
durable fix is the indirection — `ActionIcons.backfill` as the one
definition, `ContextIcons.backfill` aliasing it, a reference-equality
guard test, and zero raw icon imports for backfill. Now "change the
backfill icon" is a one-line edit, which is what consistency actually
requires.

The same pattern applies to any icon used in 3+ places: route it through
the icon map, never import the lucide component directly in feature
components. (Existing direct imports elsewhere are pre-existing debt; this
ADR fixes backfill specifically and sets the precedent.)

### 80 Backfill modal viewport overflow + dead CSS cleanup (v0.79.8)

#### Context

The backfill modal (one `BackfillModal` component, reused for pipeline /
asset / cell / task contexts) overflowed the viewport when the user
expanded Options and the "Tasks to run" list. The footer action buttons
(Cancel / Preview / Start Backfill) were pushed off-screen with no way to
reach them — the modal grew unbounded and was vertically centered, so
both top and bottom clipped.

Root cause: a **class mismatch**. The live modal used
`className="modal bf-modal"`, but `.bf-modal` had only
`max-width: 600px` — no height cap. The correct height-cap CSS
(`max-height: 90vh` + flex column + scrollable body) existed under
`.bf-backfill-modal`, a class **no component used** — dead CSS left over
from the pre-v0.78 modal that was renamed to `bf-modal` without moving
the rules.

#### Decision

**Fix (A): height-cap the live `.bf-modal`.**

```css
.bf-modal {
    max-width: 600px;
    width: 100%;
    max-height: 90vh;                  /* fallback */
    max-height: calc(100dvh - 2rem);   /* precise; dvh handles mobile chrome */
    display: flex;
    flex-direction: column;
}
.bf-modal .modal-body { overflow-y: auto; flex: 1 1 auto; min-height: 0; }
.bf-modal .bm-modal-header,
.bf-modal .modal-footer { flex-shrink: 0; }
```

Header and footer pin; only the body scrolls. The component already had
the right JSX structure (`ModalHeader` / `ModalBody` / `ModalFooter` as
direct children) — the CSS just wasn't reaching it.

**Fix (B): responsive task list.**

```css
.bf-task-list { max-height: min(180px, 24vh); overflow-y: auto; }
```

`min()` shrinks the inner list on short screens (24vh ≈ 144px on a
600px-tall laptop) so it never dominates; on tall monitors it caps at
180px. The modal body's own scroll handles overflow beyond that.

**Responsive (all screen sizes):** global overlay gains
`padding: 1rem; box-sizing: border-box;` so no modal touches the viewport
edge on small screens. The `.bf-modal` max-height `calc(100dvh - 2rem)`
exactly matches that padding (1rem top + 1rem bottom), so the modal fills
the available space precisely without overflow on any height. `dvh`
(dynamic viewport height) means a mobile browser's collapsing URL bar
doesn't push the footer out of reach.

**Dead CSS removed (CLAUDE.md #1):** the audit that found the mismatch
also found the entire old-modal stylesheet was dead — every
`.bf-backfill-*` class (info, pipeline-name, dates, selection, group,
item, summary, auto-vars, advanced, variables, disclaimer) and
`.bf-nav-pills--plain`. None referenced by any `.tsx`. The CSS comment
literally said "Replaces .bf-* after old BackfillModal removed (Phase
5c)" — the old modal was gone, the CSS wasn't. Removed 220 lines across
two interleaved blocks (the live `.bf-option-*` / `.bf-task-*` rules
between them were preserved).

#### Why this overlay change is safe for all modals

The overlay (`.bm-modal-overlay`) is shared by every modal. Adding 1rem
padding shrinks available space by 2rem total. Any modal with
`max-height: 90vh` still fits within `100vh - 2rem` whenever the viewport
exceeds 320px tall (2rem < 10vh) — always true in practice. Short modals
are unaffected (they're nowhere near the cap). The change is a strict
improvement: every modal gets edge breathing room on small screens.

#### Guard tests

Two structural guards in `BackfillModal.test.tsx` (the test renders the
real `BaseModal`, not a stub):
- modal container carries the `bf-modal` class (catches the exact rename
  that caused this bug),
- `.modal-body` and `.modal-footer` both present under the dialog (the
  two elements the height-cap CSS depends on).

These can't verify scroll/layout in jsdom (no real layout engine), but
they lock the class/structure contract the CSS relies on. A future rename
of `bf-modal` or removal of the body/footer wrappers fails the test
immediately.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| vitest | 833 | **835** | +2 (layout contract guards) |
| Python total | 1358 | 1358 | 0 |
| tsc strict | 0 errors | **0 errors** | — |
| CSS lines | — | **−220 dead** | cleanup |
| **All gates** | green | **green** | — |

#### Lesson

The bug looked like "modal too tall" but was really "CSS pointed at a
class that no longer existed." The fix existed in the codebase the whole
time — orphaned under a dead class name. Two lessons:

1. **Renames must move the rules, not just the markup.** When
   `bf-backfill-modal` became `bf-modal`, the CSS should have moved with
   it. It didn't, and the height cap silently stopped applying.
2. **Dead CSS hides working CSS.** The dead `.bf-backfill-modal` block
   looked like it was handling the modal — anyone scanning the file would
   assume the height cap was active. Removing dead code isn't just
   tidiness; it removes false signals about what the live code does. A
   CI check for unreferenced BEM-prefixed classes would have caught both
   the dead block and the mismatch.

### 81 Zombie-backfill reconciliation unblocks the concurrency guard (v0.79.9)

#### Context

Users could not start a backfill from the asset tab (or pipeline tab):
every attempt returned `409 concurrent_backfill_active`, even though the
matrix showed `0 running` executions. The nav badge read "Backfills 1" —
one backfill the system considered active, permanently blocking all new
backfills for that pipeline.

Root cause: a **zombie backfill**. The `bulk_backfill` SFN writes
`status='running'` at start and stamps the terminal status in its
Finalize step. If Finalize never runs (SFN timeout, abort, mid-way
crash, or an orphaned record whose SFN never started), the DDB record
stays `running`/`pending` forever. The concurrency guard
(`list_active_for_pipeline`) checks the **raw stored status**, so a dead
backfill blocks every new one — including asset-tab backfills, which
resolve to the asset's producing pipeline and hit the same guard.

The codebase already had `_compute_derived_backfill_status` (v0.79.1,
ADR #73) that detects the "all partitions done but not finalized" case —
but it was used only for **display**, never for the guard. So the UI
could show a backfill as completed while the guard still treated it as
active. A pure mismatch between what's shown and what's enforced.

#### Decision

Add a single reconciliation helper, `_reconcile_backfill_status(item)`,
used wherever a backfill's *effective* status matters. It resolves a
pending/running record cheapest-first and heals zombies in place:

- **A — counters (no AWS call).** Reuse `_compute_derived_backfill_status`.
  Covers the common zombie: all partitions processed, Finalize just
  didn't run → terminal.
- **C — SFN execution state.** If counters are inconclusive, describe the
  bulk_backfill execution (its name equals the `backfill_id`). A terminal
  execution (`SUCCEEDED`/`FAILED`/`TIMED_OUT`/`ABORTED`) — or a missing
  one (`ExecutionDoesNotExist`: never started or aged out of SFN's 90-day
  history) — means the backfill cannot still be running.
- **B — self-heal.** When A or C resolves a pending/running record to a
  terminal status, stamp it in DDB via a conditional update (status still
  pending/running). Idempotent; concurrent heals are safe (the loser hits
  ConditionalCheckFailed and no-ops). Best-effort: a heal failure never
  breaks the caller.

Two call sites, one helper (no duplication):
- **Concurrency guard** (`start_backfill`): filter the raw-active list
  through reconciliation; only genuinely pending/running backfills block.
  Zombies heal and drop out → new backfills proceed.
- **Display** (`_format_backfill_summary`, used by list + detail): report
  the reconciled status. Side effect: the "active backfills" badge and
  the guard self-heal on the next list/detail load, without a manual
  cancel.

SFN→Backfill terminal mapping: `SUCCEEDED→completed`, `FAILED`/
`TIMED_OUT→failed`, `ABORTED→canceled`, missing execution→`failed`.

#### Edge case: the just-started race (age guard)

`ExecutionDoesNotExist` is ambiguous. It means *either* "the SFN
`start_execution` for a just-created backfill hasn't propagated yet"
(the record is seconds old and legitimately starting) *or* "the
execution never started / aged out of SFN's 90-day history" (a genuine
zombie). Healing the first case to `failed` would let a concurrent
reconcile (a list load, or a second start hitting the guard) kill a
backfill that is starting normally — `start_backfill` writes the
`pending` record (`put_if_new`) *before* `sfn.start_execution`, so there
is a real window where the record exists but the execution is not yet
describable.

Resolution: the missing-execution path heals only when the record is
older than `_EXECUTION_GONE_MIN_AGE_SECONDS` (120s). SFN executions are
describable within milliseconds of `start_execution` returning, so any
record older than that with no execution is certainly orphaned; anything
younger is treated as still active (returned as raw, not healed). The
counters path (A) and the SFN-terminal path (FAILED/TIMED_OUT/ABORTED)
do not need the guard — counters only complete if work actually ran, and
a terminal execution definitively ended. `started_at` is stamped at
record creation, so age is always available for real records; if it is
somehow absent, the path errs toward *not* healing (treats as active),
preserving the race-safety bias.

#### Fail-closed

On any *unexpected* SFN error during reconcile (throttling, IAM), the
helper returns the raw status — the backfill stays "active" and the guard
still rejects. Wrongly blocking a new backfill is safer than allowing a
true duplicate (the guard exists to prevent duplicate per-partition
writes). Only `ExecutionDoesNotExist` is treated as a definitive
not-running signal.

#### Why reconcile on read (display) too, not just the guard

The guard fix alone unblocks new backfills, but the zombie record would
linger as `running` in DDB and the "Backfills N" badge would stay wrong
until someone started a new backfill against the same pipeline. Healing
on read (the lazy-reconciliation pattern, same as pipelines'
`_reconcile_running`) means any list/detail view clears the badge. The
cost is bounded: reconciliation short-circuits at A (no AWS call) for the
common case, and only running/pending records ever reach the SFN describe
— terminal records return immediately.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| console_api | 348 | **355** | +7 |
| — `TestZombieReconciliation` | — | 6 | counters-heal, SFN-timeout-heal, execution-gone-heal (old record), just-started-not-healed (race guard), genuinely-running-blocks, SFN-error-fails-closed |
| — `TestListBackfills.test_list_self_heals_zombie_status` | — | 1 | display self-heal + badge |
| SDK Python | 912 | 912 | 0 |
| **All gates** | green | **green** | — |

The pre-existing concurrency reject test was tightened to set an explicit
`describe_execution → RUNNING`, so it no longer passes by accident on a
truthy MagicMock.

#### What this does NOT do

- **Does not change the `bulk_backfill` SFN.** The Finalize step is still
  the primary path that stamps terminal status; reconciliation is the
  safety net for when it doesn't run.
- **Does not interrupt in-flight executions.** Healing only re-labels the
  parent record; it never stops a real running SFN. A genuinely-running
  backfill (SFN status RUNNING) is never healed.
- **Does not add a background sweeper.** Reconciliation is lazy
  (on-guard, on-read). A stuck backfill on a pipeline nobody ever lists or
  backfills again stays labeled running — harmless, since it blocks
  nothing until someone tries that pipeline (at which point it heals).

#### Lesson

The bug was a split between *computed* truth and *enforced* truth. v0.79.1
correctly derived the real status for display but left the guard reading
raw — so the system showed one thing and enforced another. When a value
has a "real" derivation, every consumer that acts on it (display AND
guards AND counts) must go through the same derivation, or they drift.
The fix routes all three through one `_reconcile_backfill_status`.

### 82 Derived-status premature-terminal fix + backfill audit (v0.79.10)

#### Context

A full review of the backfill subsystem (routes/backfill.py, backfills_repo,
the bulk_backfill SFN template) after the v0.79.9 zombie-reconciliation
work surfaced one real correctness bug and a consistency gap.

#### Bug — `skipped_partitions` inflated the "processed" count

`_compute_derived_backfill_status` computed
`processed = completed + failed + skipped` and reported terminal once
`processed >= total_partitions`. But the counters mean different things:

- `total_partitions` is set to `len(partition_keys)` **after** the
  skip_completed pre-flight filter — it is the to-RUN count, excluding
  pre-skipped partitions.
- The bulk_backfill Map increments only `completed_partitions` and
  `failed_partitions`, one per iteration.
- `skipped_partitions` is the pre-flight skip_completed count, set once at
  record creation and **never** touched by the SFN.

So adding `skipped` to `processed` mixed a pre-flight count (not in `total`)
into the Map-progress sum. A partially-run backfill could be reported
terminal while the Map was still running. Example: backfill 8 partitions,
3 already complete → `total=5, skipped=3`; after 2 of 5 ran,
`processed = 2 + 0 + 3 = 5 >= 5` → "completed" while 3 were still in flight.

Before v0.79.9 this was a cosmetic display bug (list showed a running
backfill as done). v0.79.9 made it dangerous: `_reconcile_backfill_status`
drives the concurrency guard and self-heal, so a premature-terminal
derivation would heal a *running* backfill to terminal and drop it from
the active set — letting a concurrent backfill start against the same
pipeline and defeating the duplicate-write protection the guard exists for.

The SFN's own Finalize JSONata never had this bug — it computes status
from completed/failed only. The Python derivation diverged from it.

**Fix:** `processed = completed + failed`. "All Map work done" is exactly
`completed + failed >= total_partitions`. `skipped_partitions` stays in the
record for display ("N already complete") but no longer feeds the
done-check. The buggy test `test_skipped_counts_toward_processed` — which
had codified the wrong behavior — was rewritten to assert that a pre-flight
skip does not make a running backfill look terminal.

#### Consistency fix — retry eligibility uses reconciled status

`retry_failed` checked the raw stored status for eligibility
(failed/partial). A zombie parent stored `running` but effectively
failed/partial would be rejected as `not_eligible`, even though the UI
(showing the reconciled status) offers a Retry button. Routed the
eligibility check through `_reconcile_backfill_status(parent)` so the
backend agrees with what the user sees; reconcile also self-heals the
parent in passing.

#### Audit notes (reviewed, no change needed)

- **SFN sets `is_backfill: true`** on every child execution (both pipeline
  and asset targets go through bulk_backfill) — the old "asset-backfill
  missing is_backfill" concern is not present on this path.
- **No double-count of failures** — the StartChildSFN `Catch` (exception
  path) and the `ChildSucceededChoice` default (non-exception failed path)
  are mutually exclusive, so `IncrementFailed` fires at most once.
- **Canceled finalization** is correct — Finalize honors a `canceled`
  status regardless of counters.
- **DAL discriminators** (`record_type='backfill'`, sentinel pipeline_name)
  are set consistently in put/put_if_new and filtered on read.
- **No client-side derivation duplicate** — `backfillStatus.ts` was removed
  in v0.79.1; the UI consumes the backend status (CLAUDE.md #2 clean).

#### Audit notes (logged for backlog, not fixed here)

- **Hourly partition keys in child execution Name** — the SFN builds the
  child execution name from `partition_key`; if hourly granularity ever
  produces keys containing characters illegal in SFN execution names
  (e.g. `:`), StartExecution would fail. Daily/weekly/monthly keys are
  safe. Verify the hourly partition-key format before enabling hourly.
- **`.sync:2` Retry name reuse** — StartChildSFN retries reuse the same
  execution Name+Input; SFN StartExecution is idempotent on name+input, so
  the Retry block may not produce a genuinely fresh attempt. Pre-existing;
  warrants a live test before relying on child-level retries.
- **`force` / `incremental` options** remain accepted-but-unused (API
  compat, documented). Borderline dead config; kept intentionally.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| console_api | 355 | **356** | +1 net (rewrote derived-skip test; added retry-zombie-eligible) |
| SDK Python | 912 | 912 | 0 |
| **All gates** | green | **green** | — |

#### Lesson

Two helpers (`_compute_derived_backfill_status` and the SFN Finalize
JSONata) implemented the same concept — "is this backfill done?" — and
drifted: the JSONata used completed/failed, the Python added skipped. When
the same rule lives in two places it will diverge; the safest design has
one authority. Here the SFN must own the Finalize math (it runs in the
execution), so the Python derivation's job is only to *mirror* it — and a
shared comment now pins them together. The same drift caused ADR #81 (raw
vs. derived status in the guard); the recurring fix is "one rule, one
place, every consumer routes through it."

### 83 Backfill status consolidation + correctness hardening (v0.80.0)

#### Context

ADR #81 (raw-vs-derived status in the concurrency guard) and ADR #82 (a
stray `+ skipped` term reporting running backfills as terminal) were the
same failure twice: the "is this backfill done / what's its terminal
status" rule was implemented in multiple places and drifted. A focused
quality pass consolidates that rule and closes the residual correctness
gaps the v0.80.0 audit surfaced.

This bundles the three workstreams the maintainer approved (A/B/C):

#### A — Parity drift check (one rule, verified across runtimes)

The terminal-status rule lives in two runtimes that can't share code:
Python (console API derivation) and JSONata (the bulk_backfill SFN
Finalize state). Created `polyris/codegen/check_backfill_status_parity.py`
(`make check-backfill-parity`), which asserts the SFN Finalize JSONata
encodes the canonical rule — the canceled/completed/failed/partial
decision structure, and that it does NOT reference `skipped` in the
aggregate (the ADR #82 guard). It's the same pattern as the SFN
status-literal drift check (ADR #78), enforced via the SDK test suite
(`tests/sdk/test_backfill_status_parity.py`). The JSONata is the only side
that *can* drift now (see B); this catches it at CI time.

#### B — Single Python authority + `BackfillRecord` value-object

- **`polyris/backfill_status.py`** is the one Python authority for the
  rule: `finalize_status(completed, failed, *, canceled)` and
  `all_map_done(total, completed, failed)`. Both the console API derivation
  and (logically) the SFN Finalize implement this; the parity check pins
  the JSONata to it. The Python side can no longer drift because every
  consumer calls these functions.
- **`BackfillRecord`** (in `dal/backfills_repo.py`) is a typed, read-only
  view over a Backfill DDB item. It centralizes field access and the
  status/counter semantics: `id` (backfill_id or execution_name),
  `total`/`completed`/`failed`/`skipped` (typed, with the documented "total
  excludes pre-flight skipped" semantics), `raw_status`, `is_active`,
  `is_terminal`, `map_done`, `derived_status()`, `age_seconds()`. No
  consumer reads raw dict keys or re-derives done/active ad-hoc anymore —
  the exact pattern that drifted in ADR #81/#82.
- `_compute_derived_backfill_status`, `_reconcile_backfill_status`,
  `_heal_backfill_status` were refactored to go through `BackfillRecord`;
  the standalone `_backfill_age_seconds` and the duplicated
  `item.get('backfill_id') or item.get('execution_name')` (6 sites)
  collapsed into the record. `_compute_derived_backfill_status` is kept as
  a thin shim (delegates to `BackfillRecord.derived_status`) so existing
  callers/tests are undisturbed.

#### C — Correctness gaps from the audit

- **Child execution name length (fixed).** The bulk_backfill SFN names
  each child `{pipeline}-{partition}-bf-{hex8}`; AWS caps execution names
  at 80 chars. A long pipeline name plus a long partition key could exceed
  it, which would make StartExecution fail *inside the Map* — every such
  partition silently marked failed with no actionable error. `start_backfill`
  now validates the projected name length up front and rejects with
  `child_name_too_long` (422) and a clear message.
- **Hourly partition keys (verified safe, not a bug).** The hourly format
  `YYYY-MM-DDTHH` contains only SFN-name-legal characters; the earlier
  audit note was over-cautious. The only real edge was length, covered
  above.
- **`.sync:2` child Retry name reuse (documented, deliberately not
  edited).** The `StartChildSFN` Retry likely re-attaches to the same
  already-failed execution (idempotent StartExecution on constant
  name+input), making child-level retry ineffective. Impact is low
  (partitions still end correctly failed; backfill-level retry-failed is
  the real re-run path; cost is wasted backoff). Fixing it means editing
  orchestrator JSONata, which has no CI-side syntax validation and can't be
  end-to-end tested here. Per the "highest quality" bar, an unverified
  change to the most critical component is not shipped blind — the issue,
  its open AWS-behavior question, and two candidate fixes are specified in
  STATE.md for a deploy-time validation.

#### Backlog hygiene

Two "future enhancement" items were already implemented and were marked
done (real `skip_completed` pre-flight; `partition_start` from asset
metadata) — the backlog had drifted from the code.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| console_api | 356 | **366** | +10 (8 BackfillRecord, 2 child-name) |
| SDK Python | 912 | **923** | +11 (parity) |
| **All gates** | green | **green** | parity + sfn-templates + enums + cfn-lint |

#### Why this is a minor version bump

New public SDK module (`polyris.backfill_status`), a new CI check, and an
internal architecture change (value-object + single authority). No API
contract change and no behavior change for valid inputs — the consolidation
preserves semantics while removing the drift surface.

#### Lesson

The recurring fix across #81, #82, #83 is the same sentence: *one rule, one
place, every consumer routes through it.* #81 and #82 patched the symptoms
(guard reads derived; remove the bad term). #83 removes the cause — there
is now exactly one Python authority, one typed accessor per concept, and a
CI check that the second runtime (JSONata) stays in parity. Drift is no
longer possible silently; it fails a test.

#### Post-review SSoT completion (same release)

A quality review of this work found that the consolidation was incomplete:
`console_api/constants.py` still defined manual class-level
`BackfillStatus.TERMINAL` / `.ACTIVE` / `.ALL` sets, duplicating the
codegen-generated `BACKFILL_TERMINAL_STATUSES` / `BACKFILL_ACTIVE_STATUSES`
(sourced from `polyris/constants.py`). This is the "BackfillStatus 2 copies"
gap — TaskStatus had migrated to generated sets (ADR #77) but BackfillStatus
hadn't. The new `BackfillRecord` had (incorrectly) consumed the manual
duplicate.

Completed the migration:
- `constants.py` now re-exports `BACKFILL_TERMINAL_STATUSES` /
  `BACKFILL_ACTIVE_STATUSES` from the generated module.
- Removed the manual `BackfillStatus.TERMINAL` / `.ACTIVE` / `.ALL`
  (the last was also dead — zero references).
- Migrated all 6 consumers (BackfillRecord ×2, routes guard/derive/cancel
  ×3, plus an inline `(PENDING, RUNNING)` tuple in the guard) to the
  generated sets. Removed the now-dead `BackfillStatus` import from the DAL.

Result: the terminal/active status sets have one canonical source
(`polyris/constants.py`) flowing through codegen to every consumer; no
hand-maintained copy remains. The string-value `BackfillStatus` class
(PENDING/RUNNING/… with per-value docstrings) is intentionally kept in
console_api as the readable value namespace — it mirrors the generated
class and is covered by the existing enum drift check; folding it fully
into the generated module is the remaining piece of the broader
enum-SSoT consolidation tracked in STATE.md.

#### DAL placement note

`BackfillRecord` lives in `dal/backfills_repo.py` — the typed shape of what
the repo returns, co-located with it for cohesion. Its `derived_status()`
is a thin adapter that delegates to the `polyris.backfill_status` authority
(no logic ownership in the DAL). This is a deliberate judgment call given
there is no separate `models/` layer in console_api; if stricter layering
is wanted later, the value-object extracts cleanly to a models module
without touching its consumers.

#### Follow-up 2: models layer + class-level enum consolidation (same release)

Closing the two quality nuances flagged in review:

**DAL → models.** `BackfillRecord` was extracted from `dal/backfills_repo.py`
to a new `models/` layer (`models/backfill_record.py`). The DAL is now
persistence-only; the domain value-object (status/counter semantics) lives
in models. Consumers import `from models import BackfillRecord`; the DAL no
longer carries the domain helpers. Its tests moved to
`tests/models/test_backfill_record.py`.

**Class-level enum SSoT — BackfillStatus + AssetOperator.** Beyond the
terminal/active *sets* (Follow-up 1), the `BackfillStatus` *class* itself
was a manual duplicate in `console_api/constants.py` of the
codegen-generated class — and the drift check only validates
`constants_generated`, so the manual class was unguarded. Verified the
manual and generated values were identical for `BackfillStatus` and
`AssetOperator`, then replaced both manual classes with re-exports of the
generated ones. Per-value documentation was moved to the canonical
`polyris/constants.py` source. Now there is one definition per class,
guarded by the drift check.

**Found but deferred — three families have diverged.** `TaskStatus`,
`TriggerRule`, `PipelineStatus` could NOT be consolidated the same way:
their membership has already drifted (generated `TaskStatus.SUCCEEDED` not
in manual; manual `TriggerRule.DEFAULT` and `PipelineStatus.PAUSED/ABORTED`
not in canonical). A naive re-export would break those consumers. Reconciling
canonical membership first — and investigating whether canonical
PipelineStatus is genuinely missing paused/aborted (a possible latent bug,
not mere duplication) — is logged in STATE.md as the remaining piece of
the 8-family enum SSoT effort. Consolidating only the safe families now,
and documenting the unsafe ones precisely, is the correct bounded scope:
shipping a re-export that breaks `PipelineStatus.PAUSED` to chase tidiness
would trade a cosmetic SSoT win for a real regression.

#### Final test counts (v0.80.0, all follow-ups)

| Suite | v0.79.10 | v0.80.0 |
|---|---|---|
| console_api | 356 | **366** |
| SDK Python | 912 | **923** |
| **Drift checks** | enums, sfn-templates | **+ backfill-parity** |
| **All gates** | green | **green** |

#### Follow-up 3: TaskStatus / TriggerRule / PipelineStatus consolidation

Investigated the three families flagged as "diverged" and found the
divergent members were all **dead code**, not real gaps:
- `TriggerRule.DEFAULT`, `TriggerRule.EARLY_TRIGGER`, `TriggerRule.WAIT_ALL`
  — zero references repo-wide.
- `PipelineStatus.PAUSED`, `PipelineStatus.ABORTED` — zero references.
  (ABORTED is correctly an `ExecutionStatus` member already; the canonical
  `PipelineStatus` is intentionally the aggregate sidebar status —
  idle/running/succeeded/failed/waiting — not execution/operational state.
  So this was not a missing-status bug.)
- `TaskStatus` — canonical is a strict superset of the manual copy (only
  adds `SUCCEEDED`), so re-export is non-breaking.

Result: **console_api/constants.py now defines no duplicated status
classes.** TaskStatus, TriggerRule, PipelineStatus, BackfillStatus,
AssetOperator and the TASK_*/BACKFILL_* sets are all re-exported from the
codegen-generated module (the single source). Only console-specific
constants remain hand-written (EventType, Limits, BackfillLimits,
SFN_STATUS_MAP, validators). The dead members were dropped (CLAUDE.md #1).

**_shared / evaluate_deps** (the other copies of the "4-copy" problem):
`_shared/constants.py` keeps its status classes manual on purpose — it has
an ImportError fallback so the file works standalone in unit tests, and
re-exporting the generated classes would couple it hard to
`constants_generated` at import time. Rather than trade that resilience for
tidiness, the dead members (`DEFAULT`, `EARLY_TRIGGER`) were removed and a
new guard — `polyris.codegen.check_shared_constants` (run by
`make sync-constants`) — verifies the _shared manual classes never define a
value absent from canonical. So the duplication that remains is now
*guarded*: it cannot silently drift. evaluate_deps stays a verified copy of
_shared.

**sync-constants check fixed.** Its old text-line comparison (console_api
literals ⊇ _shared literals) broke once console_api stopped defining the
constants as literals (it re-exports now). Replaced with the value-based
`check_shared_constants` guard, which reflects the new structure and is the
more meaningful invariant.

This closes the enum-SSoT work for the status families touched by the
backfill review. The remaining families' string-value classes that are
still hand-written in _shared are now guarded rather than free-floating;
fully folding _shared into generated (resolving the standalone-resilience
tension) is the last piece of the broader effort, left as a deliberate
design decision in STATE.md.

### 84 SDK / shared-constants delivery to Lambdas — guarded copy now, PyPI later (v0.80.0)

#### Status

Accepted. Records a deliberate decision so the question isn't re-litigated
each time the constants duplication is noticed.

#### Context

AWS Lambda functions cannot import each other's code. Canonical definitions
in `polyris/constants.py` must therefore be *delivered* into each Lambda
artifact. Today this happens two different ways:

- **console_api** bundles the whole `polyris` package via a committed
  symlink (`sam/lambdas/console_api/polyris` → repo-root `polyris/`); SAM's
  Python builder follows it into the artifact.
- **helper Lambdas** (evaluate_deps via the `_shared` copy) carry a
  codegen-generated `constants_generated.py` plus, in `_shared/constants.py`,
  a few hand-written status classes with an ImportError fallback so the file
  imports standalone in unit tests.

After the v0.80.0 enum-SSoT consolidation (ADR #83), console_api re-exports
all status classes from the generated module, and `_shared`'s remaining
manual classes are guarded against drift by `check_shared_constants`. What
remains is the question of whether to eliminate the duplication *structurally*
rather than guard it — i.e. how shared code reaches each Lambda.

#### Options considered

**A — Fold `_shared` into the generated module (re-export classes).**
Mirror what console_api does. *Rejected for `_shared`:* it removes the
ImportError fallback that lets `_shared/constants.py` import standalone in
direct unit tests, trading real test resilience for cosmetic tidiness, and
hard-couples `_shared` to `constants_generated` at import time.

**B — Keep the guarded copy (status quo after v0.80.0).** `_shared` keeps
its manual classes + fallback; `check_shared_constants` (run by
`make sync-constants`) fails CI if those classes ever define a value absent
from canonical. Duplication remains but **cannot silently drift**.

**C — Lambda Layer.** Publish `polyris` (or shared constants) as a layer,
attach to each Lambda; code lands on `/opt/python` at runtime, so every
Lambda imports canonical `polyris` directly — collapsing the symlink,
`constants_generated` copies, and `_shared` manual classes together.
AWS-native and the cleanest runtime model, but: separate layer deploy cycle
+ extra CloudFormation resource + layer-version ARN pinning; and it does
**not** fully remove the standalone-test fallback, because a layer is a
runtime concept absent under local/CI `pytest` (tests would still need the
code pathed). Per the packaging tech-debt note, "not recommended unless
multiple Lambdas need polyris and PyPI publish is far off."

**D — PyPI (or private CodeArtifact) + `requirements.txt`.** Publish
`polyris`, pin it in each Lambda's `requirements.txt`; `pip install` vendors
it at build. This is the documented **target end state** for the symlink
migration. Removes the symlink, and would let helper Lambdas import
`polyris` directly (retiring `constants_generated` + `_shared` classes).
Gated on the OSS/PyPI launch.

#### Decision

- **Now: Option B.** The only real risk in the duplication — silent drift —
  is already closed by the `check_shared_constants` guard. B preserves the
  standalone-test resilience of `_shared` at zero ongoing cost.
- **End state: Option D (PyPI).** When `polyris` is published, the symlink
  *and* the per-Lambda constants copies *and* the `_shared` manual classes
  collapse together: every Lambda declares `polyris==X.Y.Z` and imports the
  canonical module directly. Codegen mirrors (`constants_generated`) become
  unnecessary.
- **Option C (Layer) is the fallback** if a second Lambda needs `polyris.*`
  before PyPI is ready — that event is itself the migration trigger.

Crucially, this is **one packaging migration done as a single move**, not a
piecemeal change for constants alone. The constants duplication and the
polyris symlink are the same underlying problem ("Lambdas can't share
code"); they should be resolved together, under the existing
"Lambda packaging of polyris SDK" tech-debt item, when its trigger fires
(PyPI release, or a second `polyris` consumer).

#### Consequences

- No code change from this ADR; it ratifies the v0.80.0 state (guarded copy)
  as intentional, not unfinished.
- The `_shared` manual classes are permitted to remain *only while* the
  `check_shared_constants` guard exists; removing that guard without doing
  the PyPI/Layer migration is forbidden.
- Revisit when any migration trigger from the packaging tech-debt item
  fires; at that point prefer D, fall back to C, never A.

#### What must NOT come back (cross-ref packaging tech-debt)

The Lambda-local `Makefile` + `BuildMethod: makefile` + `cp ../../../polyris`
pattern and the top-level `make sam-build` vendor-copy both failed live and
are blocked by regression tests in
`tests/sdk/test_reviewer_regressions_v078.py`. A future packaging migration
must use PyPI/CodeArtifact or a Layer, not a build-time copy hack.

### 85 Fix skip-task JSONata (`$contains` on array) + evaluate template JSONata in CI (v0.80.1)

#### Symptom

A pipeline-target backfill (`cascade: auto`) over 2 dates appeared stuck:
the Backfills detail showed RUNNING, 0/2 processed, 0 children; yet the
child pipeline executions had run and showed one task FAILED with the rest
SKIPPED. The failing task's run-task-helper execution ended with
`States.QueryEvaluationError` in state `Check_Should_Skip_Task`:

> T0410: Argument 1 of function "and" does not match function signature

#### Root cause

The task-subset skip feature (ADR #51) passes a `skip_tasks` **array** into
each task. `Check_Should_Skip_Task` in `sam/sfn_templates/helpers/run_task`
decided whether to skip with:

```
$exists($states.input.skip_tasks) and $count($states.input.skip_tasks) > 0
  and $contains($states.input.skip_tasks, $states.input.task_name)
```

`$contains(str, pattern)` is a JSONata **string** function. Passing the
`skip_tasks` array as argument 1 throws a signature error — but only when the
array is non-empty (an empty array short-circuits via `$count > 0`). So tasks
that received an empty `skip_tasks` ran fine, while any task given a non-empty
skip list threw, failed, and cascaded `upstream_failed`/skipped to its
descendants. This is why a previous simple backfill succeeded but this one
(with a populated skip list) did not. It was a latent template bug, unrelated
to the v0.80.0 constants work, which never touched the orchestrator.

Confirmed against a JSONata evaluator: the old expression throws on every
non-empty `skip_tasks`; the sibling `dependency_wrapper` template already did
the same check correctly with the array-membership operator.

#### Fix

Use the `in` operator (matching `dependency_wrapper`):

```
$exists($states.input.skip_tasks) and ($states.input.task_name in $states.input.skip_tasks)
```

Verified with `jsonata-python`: empty → False, match (single or multi) →
True, no-match → False, absent → False, never throws.

#### Prevention — evaluate template JSONata in CI

The deeper problem: SFN-template JSONata had **no CI validation**, so a
malformed expression shipped and only failed at runtime. Added
`tests/sdk/test_sfn_skip_task_jsonata.py`, which extracts every skip-task
Choice condition across the templates and evaluates it (via `jsonata-python`,
a new dev dependency) against representative inputs — asserting it never
throws and returns the correct membership boolean — plus a static check that
`$contains()` is never applied to the `skip_tasks` array. Verified the guard
goes red on the old expression and green on the fix.

This is a first, targeted step (skip-task conditions use only standard
JSONata, so a standard evaluator matches ASL's dialect). Broadening JSONata
evaluation to all Choice conditions — many of which use ASL-only context like
`$states.context` — is logged in BACKLOG as a larger follow-up.

#### Operational note

In-flight executions started under the old template are not retroactively
fixed. After deploying v0.80.1, cancel the stuck backfill and start a new one.

#### Follow-up — defensive `$string()` wraps + audit harness as maintenance tool

A post-fix exhaustive audit (618 expressions × 35 input variants =
21,630 evaluations) found two places where `$length()` was called on a
field whose stringness was guaranteed only by an upstream invariant
(`task_output` set via `$string(...)` by the 7 `Run_Task_*` handlers;
`error` set via `$string(...)` by Catch outputs). Safe in production, but
fragile: if a future change drops the upstream `$string()`, both sites
would throw at runtime — exactly the same failure class as the original
bug.

Hardened defensively by wrapping the `$length()` calls (and the
value-passthrough branches) with `$string()`:

- `Save_Success` and `Save_Canonical_Output`: introduced
  ``$to := $string($states.input.task_output)`` and reused everywhere in
  the expression — the truncation check, the truncated-size encoding, and
  the value stored in DynamoDB all go through the same string.
- `Interactive_Slack` Cause: introduced ``$causeStr := $string($cause)``
  and reused.

`$string()` is idempotent on strings, so production behavior is unchanged
when the invariant holds; if a future change ever breaks the invariant,
the expressions degrade gracefully (storing the JSON-encoded form) instead
of throwing `States.QueryEvaluationError`. The full audit harness re-run
post-hardening reports 0 type/signature throws.

Also committed the audit harness as `scripts/audit_jsonata.py` and a
`make audit-jsonata` target — a maintenance tool to run before releases or
after non-trivial template edits. Not added to CI (input synthesis isn't a
perfect mirror of ASL runtime, so output is human-read; the focused
skip-task guard in `tests/sdk/` remains the CI gate).

### 86 Product concept: asset-centric UX over an unchanged task-based DSL

#### Status

Accepted — foundational product decision. Locks the conceptual model so we
develop forward instead of re-architecting. Supersedes the ad-hoc drift
between "pipeline backfill" vs "asset backfill" and the overloaded
`cascade` parameter.

#### Context

The codebase had accumulated two parallel mental models — task/pipeline
and asset (Dagster-style) — both surfaced as first-class
to the user. This produced concept sprawl: two backfill entry points,
`cascade` (auto/all/none) bolted onto only asset targets, `skip_tasks` /
task-subset, skip-completed pre-flight, and a UX that forced the user to
pick an abstraction level (pipeline vs asset) before understanding the
difference. The product's core promise — simpler than managed orchestrators — was at
risk. The recurring "we keep reworking this" pain traces to never having
fixed which model is primary.

Investigation of the existing code showed the unification is already ~80%
real in the implementation, not aspirational:
- A task that declares `outlets=[Asset(...)]` already *is* an asset
  producer; task↔asset is one entity in the data model, joined by outlets.
- Backfill is a single code path: `_resolve_target` resolves both
  `target.type='pipeline'` and `target.type='asset'` (the latter via
  `_find_producers_for_asset`) down to the same pipeline + `bulk_backfill`
  SFN. Asset backfill already collapses into pipeline backfill internally.
- The operational layer (dependency_wrapper SFN, run_task helper,
  step-actions, rerun/from-to, deps-signaling, PagerDuty) operates on a
  unit of execution and is model-agnostic.

So the divergence the team felt is in **UX/terminology**, not in the
runtime or DSL.

#### Decision

Fix the *primary lens*, not the engine.

1. **DSL is unchanged.** Authoring stays task-based: `@task` with
   `outlets` / `inlets` exactly as today. No `@asset` decorator, no
   sugar-alongside (that would be duplication — CLAUDE.md #2), no
   migration of existing pipelines. A task remains the technical unit of
   execution; a task with an outlet *is* its asset.

2. **The product accent is the asset.** UX/DX for operating the system —
   lineage, freshness, partitions, and backfill — is framed in assets
   (the data), not tasks (the steps). The asset is the lens through which
   the user views state and triggers work; tasks surface only when
   debugging a specific execution. Tasks without an outlet (sensors,
   cleanup, alerts) simply don't appear in the asset lens — they still run.

3. **Runtime/operational layer is unchanged.** Wrapper, run_task helper,
   step-actions, rerun, `_resolve_target`, `bulk_backfill` — all reused
   as-is. This is the stable foundation.

#### What changes vs what stays

| Area | Decision |
|---|---|
| Python DSL (`@task`, outlets, inlets) | **Unchanged** |
| task ≡ asset (via outlets) | **Unchanged** — already true |
| Pipeline as a grouping | **Unchanged** |
| dependency_wrapper / run_task SFN | **Unchanged** |
| step-actions / rerun / deps-signaling | **Unchanged** |
| `_resolve_target` (asset→pipeline) | **Reused** |
| UI tabs (Pipelines / Assets) | **Stay** |
| Backfill engine (`bulk_backfill`) | **Extended**, not replaced |
| Partition tracking tables in DDB | **Not needed** — current structure suffices |
| Backfill UX (asset-centric, partitions) | **Refined** — the product focus |
| `cascade auto/all/none` | **Renamed** → `downstream: off/dependents/all` |
| upstream awareness in backfill | **New** (the only genuinely new behavior) |

#### Scope of the only new behavior

Upstream smart-fill: backfilling an asset for date D resolves which
upstream assets are missing for D and builds them first, reusing existing
partitions where present. It reuses the existing DAG dependency graph and
the existing skip-completed partition check — wiring, not a new subsystem.
Paired with a symmetric `downstream` axis (renamed `cascade`). This is the
forward-development surface; everything else is fixed.

#### Consequences

- The "should I rework the model" question is closed. Future work develops
  the asset-centric UX and the upstream/downstream backfill axes; it does
  not revisit whether task or asset is primary.
- No DSL churn, no breaking change for existing pipelines, no parallel
  authoring path (CLAUDE.md #2, #9, #12 — reuse and no duplication).
- Detailed API/UI/SFN design for upstream smart-fill is deferred to its
  own ADR when that work starts; this ADR fixes only the concept.

#### CLAUDE.md alignment

#2 no duplication (one authoring path, one backfill engine), #6 alignment
before implementation (this ADR is that alignment), #9 no fix-on-fix
(consolidation, not patching), #12 maximize reuse (wire existing DAG /
partition-check / `_resolve_target` / wrapper rather than build new).

### 87 Partition mapping + upstream smart-fill — adopt Dagster's model as prior art

#### Status

Accepted (concept). Fixes the architecture for partition-level upstream
dependencies and the upstream smart-fill backfill behavior. Detailed
API/SFN implementation deferred to its own follow-up when the work starts;
this ADR locks the *shape* so we don't paint ourselves into a corner.

#### Context

ADR #86 made the asset the product's primary lens and named upstream
smart-fill (build missing dependencies for a date, reuse existing ones) as
the one genuinely new backfill behavior. Designing it surfaced two cases
that the existing single-granularity range expansion (`_floor_to_bucket` /
`_advance` in `partitions.py`) does not cover:

1. **Cross-granularity** — a daily asset depends on an hourly upstream:
   one daily partition for date D corresponds to 24 hourly partitions.
2. **Rolling window** — asset_1 for date D needs asset_2 for a window
   (e.g. the prior 7 days, D-6..D), not just D.

The team's real-world distribution: same-period 1↔1 is the dominant
pattern; cross-granularity and rolling windows are valid but less common.
Crucially, rolling windows are *already* handled at runtime today — a
task's code reads whatever range it needs from storage
(`WHERE date BETWEEN ...`); the orchestrator is not involved. The window
only matters to the orchestrator in one narrow situation: a backfill of a
gap where several consecutive upstream partitions are missing, where a
naive "check only date D" smart-fill would see the dependency as satisfied
and silently produce wrong data.

#### Decision

Adopt Dagster's `PartitionMapping` model as prior art. It is the proven,
field-tested abstraction for exactly these cases, and inventing a worse
one would violate the project's "check comparable projects before building
custom" discipline (CLAUDE.md #12). We mirror its shape, not its code.

The core abstraction is a single function — conceptually
`partitions_needed(asset, D)` — that returns the set of upstream partitions
a target partition depends on. Both problem cases are special cases of this
one function, not separate mechanisms:

- **Default = intersecting time window.** A target partition depends on all
  upstream partitions whose time window intersects its own. This single
  default covers:
  - same-granularity 1↔1 (windows coincide on one partition) — the
    dominant case, zero config;
  - cross-granularity (daily←hourly maps to the 24 covering hourly
    partitions) — also zero config.
- **Override = offset.** A rolling window is expressed as an explicit
  offset on the dependency (Dagster's `start_offset` / `end_offset`;
  Polyris surface TBD, e.g. `inlets=[asset2.offset(days=-6)]`). Opt-in,
  rare, does not complicate the default path.
- **Boundary case = allow-nonexistent.** When mapped upstream partitions
  fall outside the upstream's `partition_start`, do not hard-fail; return
  the in-range subset and warn (mirrors Dagster's
  `allow_nonexistent_upstream_partitions`).

The upstream smart-fill resolver consumes only `partitions_needed(asset, D)`
— it has no separate notion of "window" vs "granularity". It walks the
dependency graph upward, asks the mapping for each edge, checks existence
of each required partition (reusing `_scan_completed_partitions` /
skip-completed logic), and builds the missing ones. Two fill modes (from
ADR #86): `smart` (reuse existing upstream partitions, build only missing)
and `force` (rebuild all upstream regardless).

#### Why this is the right architecture

- One abstraction instead of three special cases (CLAUDE.md #2 — define
  once). The resolver is mapping-agnostic.
- Proven design — Dagster has run this in production for years; we are not
  pioneering a partition-mapping semantics.
- The default covers the dominant pattern (1↔1) *and* cross-granularity at
  zero user cost; only rolling windows need explicit opt-in.
- Forward-compatible: designing `partitions_needed` with the intersecting-
  window default *now* means adding offset overrides later is additive, not
  a breaking re-design. We avoid the corner where 1↔1-only would have to be
  torn up to admit windows.
- Reuses existing machinery: `_floor_to_bucket` / `_advance` already do
  bucket math; existence checks reuse skip-completed; `_find_producers_for_
  asset` already resolves producers.

#### Scope / phasing

- **Phase 1 (when smart-fill work starts):** implement
  `partitions_needed(asset, D)` with the intersecting-window default
  (covers 1↔1 + cross-granularity), the upward graph walk with cycle
  detection + diamond dedup, existence check, and missing-partition build.
  Same-granularity and cross-granularity both work; rolling windows fall
  back to the 1↔1 default with an explicit "window not yet honored at
  orchestration layer" warning if an offset is declared but not yet
  implemented.
- **Phase 2 (when a real rolling-window case needs it):** add the offset
  override DSL surface + its expansion. Additive; the resolver signature
  does not change.

#### Consequences

- The "will granularity / windows cope" question is closed: yes, via one
  mapping abstraction, with the dominant cases free and rolling windows as
  an additive override.
- No silent-wrong-data risk: mixed-granularity and (eventually) windowed
  dependencies resolve to the full covering set; boundary shortfalls warn
  rather than lie.
- Remaining edge-case decisions for the smart-fill implementation ADR:
  stale-vs-reuse rule (ADR #86 leaning: reuse if exists = `smart` mode;
  `force` to rebuild), backfill-vs-scheduled conflict policy, future-date
  validation, max_parallel throttle limit, mark-partition-done (lift
  task-level `mark_success` to partition scope).

#### CLAUDE.md alignment

#2 no duplication (one mapping function, not per-case branches), #6
alignment before implementation (this ADR), #7 ADR for an architectural
decision, #12 maximize reuse (adopt proven Dagster shape; reuse bucket
math, skip-completed, producer resolution).

### 88 Upstream smart-fill — implementation architecture (Python-planned, SFN-trusted)

#### Status

Accepted (implementation architecture). Builds on ADR #86 (asset-centric
UX, unchanged DSL) and ADR #87 (partition-mapping shape, Dagster prior
art). This ADR fixes *how* upstream smart-fill is built so the phased work
can proceed without re-architecting. Code not yet written.

#### Context — what the deep code analysis found

A read-through of the existing backfill flow established the constraints
the implementation must respect:

1. **The bulk-backfill SFN is intentionally "dumb"; Python plans.** Per the
   `_scan_completed_partitions` contract: the SFN trusts the partition list
   it is given and has no "check if done" state — all
   completeness/skip intelligence runs in Python at API request time. Any
   new planning intelligence (upstream resolution) must follow the same
   pattern: compute in Python at `start_backfill` time, hand the SFN an
   already-resolved plan.

2. **`_scan_completed_partitions` is the existence-check primitive.** It
   already answers "is partition D of pipeline P complete" (every expected
   task succeeded). Smart-fill reuses it verbatim to decide reuse-vs-build
   for an upstream partition.

3. **Same-pipeline upstream is free.** Within one pipeline the DAG already
   runs tasks in dependency order via `dependency_wrapper`. Upstream
   smart-fill is only needed when an upstream asset is produced by a
   *different* pipeline. Both cases occur in practice.

4. **Reusable building blocks exist:** `_expand_partitions` /
   `PartitionRange` (range → key list), `_resolve_target` +
   `_find_producers_for_asset` (asset → producing pipeline),
   `_floor_to_bucket` / `_advance` (bucket math), the `Initialize → Map →
   Finalize` bulk-backfill structure.

#### Decision

**Architecture (A): resolve in Python, keep the SFN trusting its plan.**

At `start_backfill` time, when `upstream != off`, a resolver computes the
full execution plan — target partitions plus any missing cross-pipeline
upstream partitions, arranged in dependency tiers — and the bulk-backfill
SFN executes that plan. The SFN gains no new planning logic; it remains the
trusted executor. This mirrors the established `skip_completed` pattern and
keeps SFN-change risk low (the class of failure that produced the v0.80.1
`bf-336b8481` hang).

**Two upstream scopes, handled distinctly:**

- *Same-pipeline upstream* → no resolver work. Backfilling the pipeline for
  date D already runs its internal DAG in dependency order. Free.
- *Cross-pipeline upstream* → the resolver walks the asset dependency graph
  upward across pipeline boundaries, maps each edge via
  `partitions_needed(asset, D)` (ADR #87 default = intersecting window),
  checks existence via `_scan_completed_partitions`, and schedules the
  missing upstream pipeline+partition before the target. In mode `smart`,
  existing upstream partitions are reused; in `force`, they are rebuilt.

**Resolver contract:** consumes only `partitions_needed(asset, D)`; has no
separate notion of window vs granularity (ADR #87). Includes cycle
detection and diamond dedup. Window offsets are declared-but-not-honored in
Phase 1 (warn), implemented in Phase 2 (ADR #87 phasing).

#### Open edge-case decisions (resolved here)

- **Stale-vs-reuse:** `smart` reuses an upstream partition iff it *exists*
  (passes `_scan_completed_partitions`); `force` rebuilds regardless. No
  freshness-comparison in Phase 1 (StalenessStatus stays a display concept;
  wiring it to reuse decisions is deferred — start simple, add later if a
  real case demands it).
- **Backfill-vs-scheduled conflict on the same date:** backfill wins. It is
  explicit user intent; a scheduled run for a partition under active
  backfill is suppressed/deferred. (Lock semantics detailed at build time.)
- **Future-date:** reject partitions beyond "now" floored to granularity,
  with a clear validation error.
- **max_parallel:** the cross-pipeline plan respects a parallelism cap to
  avoid SFN `StartExecution` throttling; concrete limit set against the
  existing concurrency guard at build time.
- **mark-partition-done:** lift the existing task-level `mark_success`
  (routes/tasks.py) to partition scope so a manually-fixed partition is
  treated as complete by future smart-fill (reuse, not re-run). Requires a
  confirmation in UI because it makes the partition reusable downstream.
- **Boundary (upstream shorter history):** return the in-range covering set
  and warn; never hard-fail (ADR #87 allow-nonexistent semantics).

#### Phasing

- **Phase 0:** this ADR (done).
- **Phase 0.5 (spike, done):** the cross-pipeline tiered-resolution mechanic
  was prototyped and proven on the real partition primitives — partition
  mapping (1↔1, cross-granularity), tiering, diamond dedup (incl.
  asymmetric), and cycle detection all hold. Outcome: the "one backfill_id,
  sequential tiers" approach is sound; the `force`-fallback (chained
  sub-backfills) is not needed. The spike was then removed (superseded by
  the production module) and its algorithm carried into Phase 1.
- **Phase 1 (done):** `polyris/upstream_resolver.py` (`resolve_plan`,
  `AssetGraph`, `AssetNode`, `PlanItem`, `ResolvedPlan`, `CycleError`) +
  `partitions.partitions_covering`. O(V+E) tiering via Kahn propagation
  (not the spike's re-walk), per-item `dag_hash` (R5), window-offset surface
  reserved with warn (R2). Pure SDK, fully tested.
- **Phase 2 (done):** `console_api/upstream_integration.py` builds the
  cross-pipeline `AssetGraph` from the registry and an `exists` adapter over
  `_scan_completed_partitions`; `start_backfill` accepts the top-level
  `upstream` option (off/smart/force, default off = unchanged behavior),
  resolves the plan, surfaces it in preview, and draws the honest Phase 2/3
  boundary: a real start needing cross-pipeline upstream *built* returns 422
  `upstream_execution_pending` with the plan rather than silently running on
  missing upstream.
- **Phase 1:** `partitions_needed()` + cross-pipeline upward graph walk
  (cycle detection, diamond dedup), existence via `_scan_completed_
  partitions`, intersecting-window default (1↔1 + cross-granularity).
  Same-pipeline upstream: no-op (DAG handles). Window offset: warn-only.
- **Phase 2:** integrate into `start_backfill` behind `upstream` option;
  default `off` preserves current behavior (backward-compatible).
- **Phase 3:** SFN — minimal-to-none under architecture (A); the Map
  executes the Python-built tiered plan. `make audit-jsonata` + a focused
  JSONata test gate any template touch.
- **Phase 4:** rename `cascade` → `downstream` with alias + deprecation
  (drift caught by `check-generate-enums`); backfill UI dialog
  (upstream/downstream); mark-partition-done.
- **Phase 5:** quality gates — new resolver/integration/compat tests added
  to the existing suites; no skipped tests (#11); version bump via
  `check-versions`.

#### Consequences

- SFN-change risk stays low: planning is Python-side, the SFN keeps
  trusting its input (consistent with `skip_completed`).
- Same-pipeline upstream costs nothing; only cross-pipeline upstream
  exercises the resolver — scope is proportional to actual cross-pipeline
  dependency use.
- `upstream=off` default means zero behavior change for existing backfills
  until a user opts in.
- The remaining build-time choices (lock semantics, max_parallel value,
  mark-done UX copy) are parameters, not architecture — they will not
  reopen this ADR.

#### CLAUDE.md alignment

#2 (one resolver, reuse scan/resolve/expand/bucket-math; no parallel
backfill system), #6 (this alignment precedes code), #7 (architectural ADR),
#9 (extend `start_backfill` behind a flag; not a patch-on-patch), #11 (tests
each phase), #12 (reuse `_scan_completed_partitions`, `_find_producers_for_
asset`, `_expand_partitions`, DAG ordering, Map structure; Dagster shape as
prior art).

### 89 Upstream smart-fill — deferred-risk register (no-surprises contract)

#### Status

Accepted (risk register). Companion to ADR #88. Its purpose is explicit:
enumerate every known risk in the upstream smart-fill design and give each
a concrete resolution — decided-now, or deferred-with-a-named-trigger-and-
plan — so that no risk is discovered late as a surprise. "Detailed at build
time" is not used here; each item states the decision and, critically, what
data we capture from day one so that a later policy change needs no
retroactive migration.

#### Principle

The cheapest insurance against a future rework is to **capture the data a
future decision would need, even if we don't act on it yet.** A policy can
change cheaply; a missing column across historical records cannot. So
Phase 1 records more than it uses.

#### Risk register

**R1 — Reusing a stale upstream partition yields wrong data.**
- Decision (Phase 1): `smart` reuses an upstream partition iff it exists
  (`_scan_completed_partitions`); `force` rebuilds regardless. The `force`
  mode is the always-available escape hatch.
- Data captured now: each reused partition's producing `dag_hash` and
  `last_updated` are already on the token rows; the resolver records, on the
  backfill, which upstream partitions were reused vs built.
- Deferred: a third mode `upstream=fresh` that compares freshness
  (`StalenessStatus` / `freshness_hours`, both already exist) before reuse.
- Trigger to implement: first real incident of "reused stale → wrong data",
  or a user asking for freshness-gated reuse. Additive (new enum member),
  no migration.

**R2 — Window-offset DSL surface, added in Phase 2, breaks early adopters.**
- Decision (now, to remove the risk): reserve the surface immediately even
  though Phase 1 does not honor it. The shape is
  `inlets=[asset.offset(start=-6, end=0)]` (mirrors Dagster
  `start_offset`/`end_offset`; units follow the asset granularity). Phase 1
  parses and validates it but warns "window offset not yet honored at the
  orchestration layer; resolves as 1↔1". Phase 2 implements expansion only.
- Result: Phase 2 is genuinely additive — the authored API does not change,
  only its runtime effect turns on. No early-adopter breakage.

**R3 — Tiered cross-pipeline plan mechanics are unproven.**
- Decision (Phase 1 target, to be confirmed by spike): ONE `backfill_id`
  carrying a resolved tiered partition plan; the existing `Map` runs tier
  N+1 only after tier N completes (sequential tiers, parallel within a
  tier, bounded by MAX_PARALLEL).
- Fallback if the spike shows this doesn't hold: chained sub-backfills
  (upstream pipeline backfilled first, target after), each a normal
  bulk-backfill, linked by parent `backfill_id`.
- Mandatory de-risk: a 2–3 day **spike on a branch** against one real
  cross-pipeline case BEFORE locking the mechanic. This is the single
  genuinely unknown piece; the spike exists to find the wall before code.

**R4 — mark-partition-done has no un-mark / downstream invalidation.**
- Decision (Phase 1): mark-partition-done is one-way and requires UI
  confirmation (it makes the partition reusable downstream). We are not
  regressing — no invalidation exists anywhere today.
- Deferred: invalidation (un-mark a partition + cascade-invalidate
  downstream consumers that reused it) as its own feature/ADR.
- Trigger: first need to undo a wrongly-marked partition. Until then,
  recovery path is `force` rebuild of the affected partition + its
  downstream — which already works.

**R5 — A partition is "valid" but was built with since-changed code.**
- Good news from code: `generate_dag_hash` exists and backfill already
  stores `pipeline_dag_hash`. Detection is therefore possible today.
- Decision (Phase 1): record the producing `dag_hash` per partition (cheap;
  the hash is already computed at deploy and loaded by `LoadPipelineDagHash`).
  In `smart` mode, if a reusable partition's `dag_hash` differs from the
  current pipeline's, **warn** ("partition exists but built under an older
  pipeline version") — do not block.
- Deferred: code-version-aware auto-rebuild policy.
- Trigger: a user wanting "rebuild anything built under old code". Because
  the hash is captured from day one, this becomes a pure policy addition —
  no retroactive backfill of missing data.

**R6 — Two backfills contend for the same upstream partition.**
- Decision: reuse the existing per-pipeline active guard
  (`list_active_for_pipeline`, `allow_concurrent=False` default). When
  smart-fill would schedule an upstream pipeline+partition that is already
  in flight, the resolver treats it as in-progress: it waits on / defers to
  the active run rather than starting a duplicate. Mechanism (active guard)
  is decided; the wait-vs-skip detail is a resolver parameter, not new
  architecture.

**R7 — Cancel during a partial backfill that already built some upstream.**
- Decision: reuse existing `cancel_backfill` semantics. Upstream partitions
  already built are real, valid data and remain reusable by future
  smart-fill. Target partitions partially done → `partial`. In-flight
  children honor the existing cancel path. No new state is introduced;
  built-upstream simply persists as completed partitions.

#### Phase 0.5 — the spike (added to ADR #88 phasing)

Before Phase 1 implementation, run a 2–3 day branch spike exercising R3
(tiered cross-pipeline plan) end-to-end on one real pipeline pair. The
spike's only job is to confirm or refute the "one backfill_id, sequential
tiers" mechanic and surface any unknown before it is encoded. Outcome
updates ADR #88's Phase-3 decision.

#### What remains genuinely open (and is acceptable)

After this register, the only un-pre-decided item is R3's exact mechanic,
which is precisely why it gets a spike. Everything else is either decided or
deferred with a named trigger and zero-migration data capture. This is the
no-surprises contract: future change lands as additive policy on data we
are already recording, confined to the resolver + backfill integration —
never the concept (ADR #86), the mapping shape (#87), or the DSL.

#### CLAUDE.md alignment

#6 (risks aligned before code), #7 (decisions recorded), #9 (deferrals are
additive, not patch-on-patch), #12 (reuse dag_hash, active guard, cancel
semantics, StalenessStatus rather than building new). #8 no-stubs is
honored by the spike: we prove the one unknown mechanic rather than stubbing
it.

### 90 Upstream smart-fill Phase 3 — tiered SFN execution (nested Map + tier gate)

#### Status

Accepted. Implements Phase 3 of ADR #88: make the bulk-backfill SFN execute
a Python-built tiered cross-pipeline plan. Builds on Phases 1–2 (resolver +
integration, v0.81.0). Touches live orchestration, so it follows the
JSONata discipline of ADR #85 (jsonata-python proof + `make audit-jsonata`).

#### Context

After Phase 2, `start_backfill` resolves a tiered plan but refuses to
execute cross-pipeline upstream (honest 422 `upstream_execution_pending`).
Phase 3 removes that refusal by teaching the SFN to run the plan.

The current SFN runs ONE pipeline across a flat `partition_keys` list via a
single Map. Two things must change: (1) items must span pipelines (each
carries its own `sfn_arn`), and (2) tiers must run in dependency order
(deepest upstream first), while items within a tier still run in parallel.

A subtlety surfaced during design: sequential tiers guarantee *order* but
not *dependency gating*. If an upstream partition fails, the dependent
target must not run on missing/incomplete data (the silent-wrong-data
failure mode this whole effort guards against).

#### Decision

**Execution mechanic — nested Map.**
- Outer Map over `input.tiers` with `MaxConcurrency: 1` → tiers run
  strictly sequentially, in order (AWS Map with concurrency 1 preserves
  order).
- Inner Map over the tier's items with `MaxConcurrency: max_parallel` →
  items within a tier run in parallel, exactly as the pre-Phase-3 single
  Map did.
- The inner item processor is the existing child-execution logic
  (cancel-check → StartChildSFN → increment), reused. `StartChildSFN`
  already reads `target_pipeline_sfn_arn` per item, so cross-pipeline items
  work by carrying their own arn.

**Unified input (no dual path) — CLAUDE.md #2.** The SFN input becomes
`tiers: [[item, ...], ...]` where each item is
`{sfn_arn, pipeline, partition_key, reused}`. The single-pipeline case
(`upstream=off`) is simply a one-tier plan, so there is no separate legacy
code path. `start_backfill` and `retry_failed` both build tiers via one
helper. The DDB record keeps `partition_keys` (the target partitions, for
display / retry) and `total_partitions` now counts only executable
(non-reused) items.

**Reused-skip.** An item with `reused: true` (smart-fill found the upstream
partition already complete) is not executed — a Choice routes it to a Pass.
Reused items do not increment completed/failed (they are not work).

**Tier gate (failure safety).** At the start of each tier, the processor
re-reads the backfill record. If `status = canceled` OR
`failed_partitions > 0`, the entire tier is skipped. Because nothing has
run when the first (deepest) tier starts, `failed_partitions = 0` and it
runs; if any item in a tier fails, every later tier is skipped. This
guarantees a downstream/target is never run on top of a failed upstream —
no silent wrong data. The gate is a Choice on an integer read from DDB (the
same shape as the existing cancel-check), not a type-coercing expression,
keeping JSONata risk low.

This gate is deliberately **coarse**: a single upstream failure blocks all
later tiers, including targets whose own upstream actually succeeded (or was
reused). That is the safe default. Per-partition gating (a target runs iff
*its* upstream partitions succeeded) is a refinement — recorded as Phase 3b
below — not shipped now, because doing it correctly requires per-item
`upstream_keys` plus a multi-partition completeness check in the SFN, which
is exactly the array-JSONata risk class (ADR #85) and warrants its own
careful pass.

**Finalize unchanged.** The aggregate-status computation (canceled /
completed / failed / partial from completed+failed counters) is untouched,
so `check-backfill-parity` stays green and the canonical
`backfill_status.finalize_status` rule still governs.

#### Phasing within Phase 3

- **3a (this change):** nested-Map tiered execution, reused-skip, coarse
  tier gate. Cross-pipeline upstream now executes; downstream never runs on
  a failed upstream (coarsely). Phase 2's `upstream_execution_pending` 422
  is removed.
- **3b (deferred, trigger = a real case where coarse gating blocks too
  much):** per-partition gating via item-carried `upstream_keys` + a
  completeness check. Additive; revisits only the gate, not the mechanic.

#### Consequences

- Cross-pipeline upstream smart-fill is end-to-end functional.
- The common path (`upstream=off`, single tier) is behaviorally identical:
  one tier, gate passes at failed=0, inner Map runs all partitions in
  parallel — same as before.
- SFN-change risk is contained: the risky child logic is reused; new
  surface is the outer tier Map, the reused Choice, and the integer tier
  gate, all proven with jsonata-python and swept by `make audit-jsonata`.
- Coarse gating is a known, documented limitation with a named trigger for
  3b — no surprise.

#### CLAUDE.md alignment

#2 (unified tiers input, reused inner processor — no dual path), #6 (this
ADR precedes code), #7 (architectural decision recorded), #8 (no stubs —
3a is complete and correct, 3b is a separate honest scope, not a stub), #9
(extends the SFN; the coarse gate is a deliberate safe design, not a
patch), #11 (template + integration tests, JSONata proof), #12 (reuses the
child-execution processor, Finalize, cancel-check pattern, the resolver).

### 91 Rename backfill `cascade` → `downstream` (with deprecated alias)

#### Status

Accepted. Phase 4 lead item of ADR #88. Pure API-contract rename; no SFN
change, no behavior change.

#### Context

Phase 2 (ADR #88) added the top-level `upstream` option
(`off`/`smart`/`none`→`smart`/`force`) to the backfill request. The existing
opposite-direction option was named `cascade` (`auto`/`all`/`none`). So the
API ended up asymmetric: `upstream=…` walks producers, `cascade=…` fans out
to consumers. "Cascade" also overloads a word that means different things in
different tools. The asset-centric concept work (ADR #86) calls these two
directions plainly **upstream** and **downstream**; the API should match.

#### Decision

`downstream` is the canonical request/response field name for the
consumer-fan-out option; `cascade` is accepted as a **deprecated alias** on
input and mirrored on output for one transition window.

- **Input:** the request may send `downstream` (preferred) or `cascade`
  (deprecated). If only `cascade` is present, a `deprecated_field` warning is
  returned and its value is used. If both are present, `downstream` wins.
  Values are unchanged (`auto`/`all`/`none`); the `BackfillCascade` enum
  family is untouched.
- **Output:** preview, start, and list/detail responses return `downstream`
  (canonical) and continue to include `cascade` (a mirror) so existing
  readers don't break during the transition.
- **Internal:** the in-process variable, the DDB record field, the SFN input
  key, and the SFN template all keep the name `cascade`. It is invisible to
  users, and renaming it would force a second consecutive SFN-changing deploy
  (right after ADR #90) for zero user benefit. The boundary translation lives
  only at the API edge. Documented here so the internal/external name gap is
  intentional, not drift.

The deprecation window closes (alias removed) only when no client sends
`cascade` — tracked via the `deprecated_field` warning. The bundled UI keeps
working unchanged because the alias is accepted; migrating it to `downstream`
is cosmetic and independent.

#### Consequences

- The backfill API reads symmetrically: `upstream` + `downstream`.
- Zero behavior change, zero SFN change, no deploy disruption.
- One transition release where both names are accepted/returned.

#### CLAUDE.md alignment

#5 (version-consistent rename), #6 (this ADR precedes code), #7 (API
contract change recorded), #9 (alias, not a breaking removal). The internal
name gap is a deliberate, documented trade-off against a needless SFN deploy,
not a #2/#5 violation.

### 92 Asset backfill lineage-awareness (same-pipeline frontier) + correction of ADR #88

#### Status

Accepted. Phase A of the lineage-aware backfill work. Corrects a stated
assumption in ADR #88. Backend (this ADR) + UI ship together (v-next). No
SFN change.

#### Context

ADR #88 claimed "same-pipeline upstream is free — the DAG handles it." That
is true for a *pipeline* backfill (the child runs the full pipeline DAG in
dependency order), but NOT for an *asset* backfill: `_resolve_target` sets
`task_subset = [producer_task]`, so `_compute_skip_task_ids` skips every
other task — including the producer's own same-pipeline ancestors. The
producer then runs reading its inputs from the canonical-output store
(`output#{pipeline}#{task}#{date}` in pipeline-tokens, written by
`Save_Canonical_Output`, read by `Read_Upstream_Outputs`). If those inputs
are missing for the partition, the asset is built on empty/stale input — the
"green cell over red upstream" failure visible in the Asset Matrix.

Empirical findings grounding the fix:
- Every task writes a canonical output on success; existence of an
  ancestor's output for a partition is observable via the SAME signal
  `_scan_completed_partitions` already uses (`executions_repo.query_by_date`
  → per-task status; one query returns all tasks for a (pipeline, date)).
- Canonical outputs carry a TTL of ~31 days (dependency_wrapper:
  `max(orchestration_timeout, 2592000) + 86400`). Beyond that window an
  ancestor's output is gone; a `skip_on_backfill` ancestor (e.g. an extract
  that pulls live data) cannot be rebuilt, so deep-historical partitions can
  be genuinely unbuildable. This is a data boundary, surfaced — not hidden.
- Prior art: Dagster supports backfilling a whole lineage tree and has a
  distinct "only missing partitions" mode; dbt's `+model` selects upstream
  and `state:modified` flags stale-by-code. Validates the smart/force split
  and the upstream-fail-blocks-downstream concern (already handled by the
  Phase-3 tier gate, ADR #90).

#### Decision

Asset backfill gains a top-level `upstream` mode (mirrors the existing
`downstream`), governing how much of the producer's **same-pipeline**
lineage is (re)built. A single shared frontier walk powers it:

```
frontier(producer, requested_partitions, status_of, force):
  run = {producer}
  for each direct dependency d of a task already in `run`:
    if d.skip_on_backfill:        # never run; downstream reads stored output
        continue
    if force:                     run += d; recurse into d's deps
    elif d's output is MISSING for ≥1 requested partition (status_of):
                                  run += d; recurse into d's deps
    else:                         # output present for all → stop (read it)
        continue
  return run            # -> task_subset, fed to existing _compute_skip_task_ids
```

- **off** (default, unchanged): `task_subset = [producer]`. Bug-fix re-run;
  inputs assumed present. Backward-compatible.
- **smart**: producer + ancestors whose output is missing for at least one
  requested partition (union across partitions — Phase A carries one skip
  set for all partitions, so the union never under-runs; re-running an
  already-present ancestor is idempotent). Frontier stops at present outputs.
- **force**: producer + all non-`skip_on_backfill` ancestors, regardless of
  existing outputs (full lineage rebuild).

Reuse and limits:
- Existence uses `executions_repo.query_by_date` (one query per partition,
  memoized) — the proven completeness signal; no new DAL access pattern.
- Bounded by `PREFLIGHT_MAX_PARTITIONS` (as skip-completed is); beyond it,
  `smart` degrades to `force` with a warning (never under-runs).
- `skip_on_backfill` ancestors are never added to the run set, preserving the
  read-from-storage pattern. Preview warns when such an ancestor's stored
  output is missing/expired for a requested partition (the TTL boundary).
- `_compute_skip_task_ids` is unchanged — the frontier only widens the
  positive `task_subset` fed into it (#9 widen, don't patch).

Granularity: not a concern in Phase A — a single pipeline runs all its tasks
for one partition key, so the whole same-pipeline lineage shares one
granularity by construction. (Cross-pipeline granularity is Phase B's, where
ADR #87 `partitions_covering` already maps.)

Deferred (recorded, not built):
- **Cross-pipeline per-item lineage (Phase B):** each tiered plan item
  carries its own `skip_task_ids` (its asset's frontier), composed onto the
  Phase-3 nested-Map. Requires a per-item SFN-input change. Trigger: a second
  pipeline exists. Until then there is no consumer, so no code (#1).
- **Stale-by-code:** `smart` keys off output *existence*, not freshness; an
  existing-but-stale output (changed task code) is reused. `force` rebuilds.
  The signals (`dag_hash` per partition, `updated_at`) exist for a later
  freshness mode.

#### Consequences

- Asset backfill can build an asset correctly from its real same-pipeline
  lineage, not just re-run the producer on assumed-present inputs.
- The common single-pipeline deployment is fully served with **no SFN
  change** (safe deploy).
- Preview must disclose scope (tasks-to-run × partitions, reused,
  skip_on_backfill+TTL warnings) so a one-cell click that expands to a large
  lineage is never a surprise.

#### CLAUDE.md alignment

#2 (one `frontier`, reused for Phase B later) · #6 (this ADR precedes code) ·
#7 (semantics change + correction of #88 recorded) · #9 (widen subset, skip
merge untouched) · #12 (reuse `query_by_date`, `_compute_skip_task_ids`,
preview) · DAL pattern (existence via repo) · #1 (Phase B unbuilt — no dead
code) · #8 (A+C complete; B is an honest deferred scope with a trigger).

### 93 UI status vocabulary consolidated onto generated enums (single source)

#### Status

Accepted. Chore (v0.84.1). Closes the last ungated enum-duplication surface
found in the v0.84.0 whole-repo consistency audit. No product behavior change.

#### Context

The whole-repo audit found `ui/src/utils/constants.ts` hand-maintained a
`TASK_STATUS` value object (and a `TERMINAL_STATUSES` set) parallel to the
generated, gated `ui/src/generated/enums.ts`. It was in sync with canonical at
the time, but nothing pinned it — a silent-drift risk and the UI tail of the
enum-SSoT consolidation (the backend had already been consolidated:
`console_api/constants.py` re-exports from `constants_generated.py`, pinned by
`test_enum_drift`). The duplication existed only because the codegen emitted a
type union (`type TaskStatus`) and terminal-status arrays, but not an
ergonomic named-value object (`TASK_STATUS.SUCCESS`) that UI components need.

#### Decision

Eliminate the copy rather than police it with a second test (a drift test
would institutionalize the duplication; CLAUDE.md #2/#12 say remove it).

- `sync_enums.py` additionally emits a named-key const object
  (`export const TASK_STATUS = { SUCCESS: 'success', ... } as const`) from the
  canonical `TaskStatus` Enum members, alongside the existing union type.
- `ui/src/utils/constants.ts` stops defining `TASK_STATUS`; it imports and
  re-exports it from `@/generated/enums` (mirroring the backend re-export
  pattern), and rebuilds `TERMINAL_STATUSES` from the generated
  `TASK_TERMINAL_STATUSES` array. Derived UI groupings (SUCCESS/FAILURE/
  WAITING/ACTIVE/COUNTDOWN_STATUSES) and presentation constants (STATUS_COLORS,
  normalizeStatus, VIEWS, MS) stay — they reference the single generated source
  and are typed `Set<string>` since the generated values are now `as const`.
- Consumers are unchanged (they still import `TASK_STATUS` from
  `utils/constants`, now a re-export), so the migration is zero-churn at the
  call sites.

`check-generate-enums` now covers the UI value object (it is generated), so the
gate falls out for free — no new test needed.

#### Consequences

- One generated, gated source for UI status values; the silent-drift surface
  is closed without adding a parallel test.
- Only `TaskStatus` gets a named-value object emitted — it is the only family
  with UI value-access consumers (#1: no dead generated exports). Other
  families can get the same treatment if/when a consumer appears.

### 94 BackfillUpstream + backfill error-code registry onto the generated SSoT

#### Status

Accepted. Chore (v0.85.0). Closes two cross-language drift surfaces found in
the v0.84.1 reviewer audit: the `BackfillUpstream` enum was added outside the
generated enum SSoT, and the UI backfill error-map had silently fallen behind
the route. No product behavior change.

#### Context

The upstream-lineage epic (v0.81–v0.84) added a `BackfillUpstream` mode
(`off`/`smart`/`force`) as the upstream mirror of `BackfillCascade`, but
hand-maintained it in two places — the backend validator's `_UPSTREAM_MODES`
tuple and a TS `type BackfillUpstream` union — outside the codegen enum SSoT
(ADR #72/#83/#93). `BackfillCascade`, its sibling, *is* canonical in
`polyris/constants.py` and generated; `BackfillUpstream` was a 9th, un-gated
family. Correct at the time, but exactly the silent-drift surface the enum SSoT
exists to remove.

Separately, `ui/src/utils/backfillErrors.ts` (the error-code → friendly
title+hint map) had drifted from the route's emitted codes: it was missing the
v83 `invalid_downstream*` rename codes and the v81 `invalid_upstream*` /
`upstream_cycle` codes (so both flagship features fell back to raw backend
messages), while still listing dead `invalid_cascade*` / `backfill_not_found` /
`range_outside_target` / `partition_start_violation`. Worse, its "parity" test
asserted a hardcoded `criticalCodes` array that was itself stale (listed the
dead `backfill_not_found`, none of the new codes) and therefore stayed green —
false confidence. This was the one cross-language backfill surface with no
generator gate.

#### Decision

Two different fixes because the two surfaces differ in kind.

1. **BackfillUpstream — eliminate the duplication (per ADR #93).** A canonical
   `class BackfillUpstream` lives in `polyris/constants.py`; `sync_enums.py`
   emits it into every `constants_generated.py` and into
   `ui/src/generated/enums.ts`. The backend `_UPSTREAM_MODES` is now built from
   `BackfillUpstream.{OFF,SMART,FORCE}` (re-exported via
   `console_api/constants.py`), and the TS `BackfillUpstream` type is
   re-exported from the generated module — no hand-maintained copy survives.
   `check-generate-enums` gates it for free; no new test.

2. **Error-code registry — gate the irreducible copy.** Unlike a status enum,
   the code → friendly-text map is hand-authored content (titles and hints);
   the map itself cannot be generated away. What *can* be single-sourced is the
   **set of codes** the map must cover. So a canonical `BACKFILL_ERROR_CODES`
   frozenset is added to `polyris/constants.py` and generated into `enums.ts`,
   and gated on both sides:
   - Backend `test_backfill_error_registry` pins the registry to the route's
     emitted `{'error': '<code>'}` literals in both directions (a new emitted
     code not in the registry fails; a registry code the route no longer emits
     fails).
   - UI `backfillErrors.test.ts` asserts the map covers every code in the
     generated registry — replacing the stale `criticalCodes` list.
   The map is filled for all 36 codes (operational ones like `internal_error` /
   `throttled` get explicit, deliberately-generic entries rather than falling
   through to raw backend text) and the 5 dead codes are removed.

   Scope: the registry covers **error** codes (the `{'error': ...}` shape)
   only. Warning codes (the `{'code': ...}` shape in the `warnings` array —
   `deprecated_field`, `cron_ambiguous`, `lineage`, `upstream`,
   `large_backfill`) are a separate surface and are not in this registry.

#### Consequences

- The upstream enum has one generated, gated source; the hand copies are gone.
- The backfill error-map can no longer silently drift: adding a backend error
  code fails the backend parity test until it is added to the registry, and the
  UI coverage test until it is given friendly text. The flagship downstream/
  upstream validation errors now render with titles+hints instead of raw JSON.
- Cost vs ADR #93: #93 could delete the duplicated value object because it was
  generatable; here the friendly copy is irreducibly human, so we keep a
  hand-maintained registry and gate it, rather than institutionalize a second
  hand-maintained *map*. Two test surfaces, but both check one generated list.

#### CLAUDE.md alignment

- **#1 (no dead code):** removed the 5 dead UI error codes, the unreachable
  `partitions_covering` branch, the unused `_DAY_NAMES`, and the unused
  `processed` counter; replaced the false-green `criticalCodes` test.
- **#2 / #12 (no duplication / maximize reuse):** eliminated the
  `BackfillUpstream` copy entirely; for the error map, single-sourced the code
  *list* both gates derive from rather than adding a parallel list.
- **#6 (align before implement):** audit-driven; mapped the existing codegen
  (sync_enums families, `_set_py`/`_ts_array`) before extending it.
- **#7 (ADR for API/architecture):** this entry; the registry and the upstream
  enum are now part of the documented generated contract.

---

### 95 Unified Run/Activity feed — Backfills as first-class rows in `/api/runs`

#### Status

Accepted (v0.86.0). Supersedes the Run-redesign discovery
(`docs/redesign/run/DISCOVERY.md`): the unified "Run" object that discovery
proposed is not built as a write-side parent record — Backfill already is that
parent object for the only case that needs one. "Run" is instead realized as a
read-only projection that merges the two write shapes (bare executions +
Backfill records) into one feed. Read-side change only: no DDB schema, no SFN,
no write path.

#### Context

The system has two write shapes that both "produce executions":

- A **single run** (manual trigger, asset-triggered) produces exactly one SFN
  execution. It carries no group-level state, so it needs no parent object —
  the execution *is* the run.
- A **Backfill** (ADR #51/#56/#88) produces N executions over a partition range
  and carries group-level state (progress counts, `max_parallel`, tier gating,
  cancel-all, retry-failed-subset) that cannot live on any single child
  execution. That state needs a first-class parent record — which Backfill has
  (`pipeline-tokens`, `record_type='backfill'`, sentinel
  `_polyris_bulk_backfill`, GSI `backfill-id-index`).

Discovery proposed promoting *every* run (manual/scheduled/backfill) to a
unified write-side `Run` record (a "Run of 1" for the single case). That work
was overtaken: ADR #51–#94 shipped the parent-object machinery scoped to
Backfill, scheduled runs were ruled out of scope (discovery's own note), and
`run_id` was freed by renaming the old task-grouping field to
`parent_execution_id`. What stayed un-built — a `Run`-of-1 wrapper around single
executions — adds a DDB record, an ID, and a GSI query for zero new capability.
The only real rough edge left was read-side: `/api/runs` returned executions
only and excluded Backfills (`should_skip_token_row`), while Backfills lived in
a separate `/api/backfills`, so there was no single "what happened" feed.

#### Decision

**Boundary rule (the durable principle): a parent object exists if and only if
there is group-level state that cannot be reconstructed from any single
execution.** Backfill clears it; a single run does not. Recorded so the
write-side split is intentional-on-record and not re-litigated. No `Run`-of-1.

**Read-side unification — approach A (promote to the backend, not the UI).**
`get_all_runs` (`GET /api/runs`) merges Backfills as first-class rows:

1. **`kind` discriminator in the contract.** Every row carries
   `kind: 'execution' | 'backfill'`. Execution rows are unchanged plus the tag
   (additive, backward-compatible). Backfill rows are built from
   `backfills_repo.list_recent()`.
   - *Why backend, not a UI-side merge of two endpoints:* "Run" now has a named
     definition (this ADR). A core read concept defined in one client's
     TypeScript is the duplication #1 forbids — any second consumer (CLI, API
     client) would re-derive it. ADR #22 (UI reads DDB, backend assembles the
     list) and the project's SSoT discipline put the canonical shape on the
     server. Correct time-ordered pagination also needs one sorted source, not
     a client merge of two capped lists.

2. **Status stays per-kind — no normalization.** Execution rows keep the
   aggregated execution vocabulary (`running`/`succeeded`/`failed`/`aborted`);
   Backfill rows keep the 6-state Backfill vocabulary (ADR #56:
   `pending`/`running`/`completed`/`failed`/`partial`/`canceled`). `?status=`
   matches literally against whichever vocabulary a row uses; `kind` is exactly
   what lets a consumer interpret a status correctly. This is the concrete
   reason `kind` must be in the contract rather than inferred client-side.

3. **`?date=` includes a Backfill when the date is within its partition range.**
   Membership is a string range over the record's `partition_keys`
   (`lo <= date <= hi`). Exact for daily granularity (`YYYY-MM-DD` sorts as
   dates); for weekly/monthly keys a daily `?date=` is best-effort and may not
   match — `?date=` is a daily-oriented filter. Acceptable: the unfiltered feed
   and `?pipeline=`/`?status=` are the primary paths.

4. **`?pipeline=` filters a Backfill on its `target_pipeline`.** A
   cross-pipeline tiered Backfill (ADR #90/#92) surfaces under its target only,
   not under every pipeline in its tier plan. Simple and predictable.

5. **14-day window alignment.** Execution rows default to the 14-day SLA window;
   Backfills come from `list_recent()` (not date-windowed), then the merged list
   is truncated by `limit`. A Backfill older than 14 days can therefore appear
   when its executions no longer do — accepted; the feed is "recent activity",
   and a long-running Backfill over old partitions is exactly what an operator
   still wants to see.

**Children are not embedded.** A feed row expands on demand via existing
endpoints — an execution via `GET /api/execution-children`, a Backfill via
`GET /api/backfills/by-id` — so the list stays cheap and reuses what exists
(#12). The dedicated `/api/backfills` endpoint and the backfills page remain as
a backfill-only filtered view; nothing is removed.

#### Consequences

- `/api/runs` response gains `kind` on every row and may contain `backfill`
  rows; the Runs page becomes the unified Activity feed. Internal-only today, so
  the additive contract change is safe; documented in `docs/operations/API.md`.
- `get_all_runs` now rides `backfills_repo.list_recent()`, a sentinel scan
  (noted since v0.78). At scale the `started_at` GSI optimization already
  backlogged for `/api/backfills` covers the unified feed too — no new cost path
  beyond that shared scan.
- The write-side split is now load-bearing and on record; a future
  "everything is a Run" change must revisit this ADR, not assume it.

#### CLAUDE.md alignment

- **#1 / #12 (no duplication / maximize reuse):** one canonical Run shape on the
  server, not re-derived per client; expand reuses existing children endpoints;
  the backfills page/endpoint are reused, not forked.
- **#2 (follow existing patterns):** DAL repos (`executions_repo`,
  `backfills_repo`), `cors_response`, `(ClientError, BotoCoreError)` + `log.error`,
  the existing row-shaping/sort/limit flow.
- **#6 / #7 (align before implement / ADR for API+architecture):** this entry
  precedes code; the four filter behaviors and the boundary rule are decided
  here, not silently in the handler.
- **#22 (UI reads DDB; backend assembles):** the merge is server-side; the UI
  consumes one assembled list.

---

### 65. API Tokens (PAT) & Auth Enforcement

> Full record: [`adr-65-api-tokens-and-auth-enforcement.md`](./adr-65-api-tokens-and-auth-enforcement.md).
> Summarized here so the ADR index stays the single canonical list.

**Decision:** Add Personal Access Tokens and the missing API auth enforcement in
one move. Enforcement lives in a single gate inside the `console-api` Lambda
(`auth.authenticate`, run before route dispatch under the `/{proxy+}`
integration), **not** an API Gateway authorizer — there was no authorizer and no
token check before this. The gate accepts either a Cognito access token
(browser, verified **offline** via RS256/JWKS and bound to this deployment's app
client) or a PAT (`plrs_…`, SHA-256 hash
lookup). Gated by `AUTH_ENABLED`, **on by default** (the template sets it
`true`; set `false` to disable).

**Key choices:**
- JWT verified **offline** via the pure-Python `rsa` library against the pool
  JWKS (RS256 + issuer + expiry + app-client binding), added through a
  `requirements.txt` in the console_api CodeUri (`sam build` packages it — no
  `--use-container` needed, since `rsa`/`pyasn1` are pure-Python `py3-none-any`
  wheels). No per-request Cognito call, no `cognito-idp` IAM permission. (v0.89.2
  swapped `PyJWT[crypto]`→`rsa` to drop the native `cryptography` wheel, which
  failed to load on Lambda when host-built. An earlier lean used `GetUser`; the offline
  path was implemented instead — see ADR #65 doc.)
- PATs in a dedicated `api-tokens` table (PAY_PER_REQUEST), not the shared
  `pipeline-tokens` table — keep a credential separate from operational data.
  Only the hash is stored; plaintext shown once. `hash-index` GSI for the auth
  lookup, `owner-index` for listing, TTL on expiry.
- No per-token scopes in v1; `/api/health*` and `/api/metrics` stay public.

**Non-goals (v1):** per-token scopes, RBAC beyond admin/non-admin, offline JWT
verification, a Settings page (tokens live in the `UserMenu` modal).

**Consequences:** turning `AUTH_ENABLED` on breaks any unauthenticated caller
(read-only e2e, UI if it does not send a token everywhere) — mitigated by the
flag + a UI audit + the e2e→PAT migration (rollout phase). `authentication.md`
was corrected (it falsely claimed an API Gateway JWT authorizer existed).
Cost ≈ $0 (on-demand table, no new always-on resource).

---

### 66. Per-Token Scopes (Granular Authorization)

> Full record: [`adr-66-per-token-scopes.md`](./adr-66-per-token-scopes.md).
> Summarized here so the ADR index stays the single canonical list.

**Decision:** Add an ordered scope level on each PAT — **`read` ⊂ `write` ⊂
`admin`** — enforced by `auth.authorize()` in the gate after `authenticate()`
(raises `AuthzError → 403`). Follows ADR #65. Takes effect only when
`AUTH_ENABLED=true`.

**Key choices:**
- Required level is **derived from the HTTP method** (`GET`→read, mutations→
  write) plus a 5-entry `ADMIN_ROUTES` override (token CRUD + the two delete
  routes). Classifies all 57 routes (31/21/5) with no hand-maintained table;
  new routes inherit automatically (#1/#12).
- "CI may backfill but not delete" = a `write` token (backfill=write,
  deletes=admin) — no separate `backfill` scope.
- New tokens default to **`read`** (least privilege); the UI picker makes the
  choice explicit. Cognito users = `admin`; **legacy PATs without a scope =
  `admin`** so enabling scopes never breaks an existing token (#4).
- Slack callbacks (`/api/action/*`) added to the public allowlist — a
  documented, conscious cut (link-buttons, no token; only task-state mutations;
  upgrade path = signed expiring URLs). Also a rollout prerequisite: otherwise
  `AUTH_ENABLED=true` would 401 Slack buttons.

**Non-goals (v1):** per-resource scopes, a separate `backfill` scope, RBAC,
signed Slack URLs (deferred).

**Consequences:** tokens can be scoped read-only / write, shrinking leak blast
radius; additive + backward-compatible; `403` is a new response distinct from
`401`. Cost $0 (a field on the token record).

---

### 94. Runtime config precedence (window-first)

> Full record: [`adr-94-runtime-config-precedence.md`](./adr-94-runtime-config-precedence.md).
> Summarized here so the ADR index stays the single canonical list.

**Decision:** `window.CONFIG` (written at load by `/config.js` from CloudFormation
outputs) is authoritative for all UI runtime config; baked `NEXT_PUBLIC_*` is only
a build-time fallback. `getConfig()` in `ui/src/lib/config.ts` is **window-first**,
matching `getApiUrl()` / `isAuthEnabled()`. `AUTH.enabled` uses
`window.CONFIG?.AUTH?.enabled ?? envBool(NEXT_PUBLIC_AUTH_ENABLED)` — `??`, never
`||` or `? :`, so a build-baked `'false'` cannot shadow a runtime `true`.

**Why:** the old env-first form took the truthy string `'false'` baked by
`next.config.mjs` and forced `config.AUTH.enabled=false`; `getAuthHeaders` then
dropped the bearer token while `isAuthEnabled` (window-first) still showed the
app — a blanket `401` once API auth was enabled (CHANGELOG v0.89.5).

**Rule:** never make a config field env-first again; use `??` for booleans so
`false` is not read as "unset". Regression test in `config.test.ts`.

### 96. CloudFront security response headers (static export)

**Decision:** Security response headers (`X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`,
and `Strict-Transport-Security` — max-age 1y, includeSubdomains) are applied by
an `AWS::CloudFront::ResponseHeadersPolicy` (`ConsoleUiResponseHeadersPolicy`)
attached to the console distribution's default cache behavior. The `headers()`
block in `next.config.mjs` was removed.

**Why:** the UI is `output: 'export'` (ADR #41) — a pure static export served
from S3 + CloudFront with no Next.js server at runtime. A Next `headers()` block
is silently ignored under static export (Next emits a build warning to that
effect), so the security headers it declared never reached the browser: the
console shipped with no clickjacking (`X-Frame-Options`) or MIME-sniffing
(`nosniff`) protection, while the config gave a false impression they were set.
CloudFront is the only layer that sees every response, so the policy lives there.

**Notes:**
- `X-XSS-Protection` is intentionally dropped — deprecated, ignored by modern
  browsers, and can itself introduce issues (OWASP advises against setting it).
- `Cache-Control` stays on the S3 objects (`sam/deploy-ui.sh`, `ui/deploy.sh`):
  `immutable` for content-hashed `/_next/static`, `must-revalidate` for HTML and
  `config.js`. A single response-headers policy can't express per-path cache
  rules, and the S3 object metadata already does it correctly — so the cache
  half of the old `headers()` block was redundant, not lost.

**Rule:** for a static-export UI, response headers belong at the edge
(CloudFront ResponseHeadersPolicy) or on the S3 object — never in
`next.config.mjs`, where `output: 'export'` will drop them.

### 104. Open-core CLI split, backfill nav-tab slot, and contract-drift cleanup

**Context.** The open-source build advertised and partially wired Team-tier
features the open-core backend no longer registers (ADR #99/#100) — "contract
drift." Concretely: the bare `polyris` CLI was a backfill client hitting
`/api/backfill*` (404 in OSS); `Header.tsx` polled `/api/backfills` every 5s
via an inline `useBackfillsListQuery('active')` (a live 404 in OSS); a dead
`usePipelineMetricsQuery` hit a non-existent `/api/pipeline-metrics`; and the
README, `docs/operations/API.md`, and `HelpModal` promised backfill, Gantt,
calendar, Slack/PagerDuty, and API tokens as generally available. The boundary
is intentional (backfill/ops/governance are paid; engine + assets + lineage are
free) — the drift was that the public surface still claimed the paid half.

**Decision.**
1. **CLI split** (supersedes the backfill-dispatch half of ADR #51). The bare
   `polyris` command is now a pure command index — it prints the available
   `polyris-*` scripts and exits 0, performing no work. Backfill moves to a
   dedicated `polyris-backfill` console script shipped from `polyris-ee`
   (`polyris._ee.backfill_cli`), with a flattened parser
   (`polyris-backfill pipeline|asset|list|show|cancel|retry-failed`). The OSS
   wheel carries neither `_ee` nor any backfill code.
2. **Backfill nav tab → `BackfillNavTab` paid-surface slot** (ADR #99). The
   Header's Backfills tab and its active-count badge now live in the Team build
   behind the `BackfillNavTab` slot; the free `ViewTab` helper is exported for
   reuse. In OSS the slot is absent (empty `paidSurface` stub), so no tab is
   rendered and the `/api/backfills` poll never runs.
3. **Removed the dead `usePipelineMetricsQuery`** (no consumer; targeted a
   non-existent endpoint) and its query key.
4. **Team backfill e2e tests** (`tests/e2e/test_backfill.py`) moved to
   `polyris-ee`; the two repos are independent (РОЗЧЕПЛЕННЯ), so the e2e harness
   travels with the tests.
5. **Docs** (README, API.md, HelpModal) now mark backfill, Gantt/calendar view
   modes, and Slack/PagerDuty alerts as Team-tier.

**Consequences.** The OSS build is self-contained and honest: no live 404s, no
free-tier promises the backend won't serve. Team features remain fully available
through the overlay, validated via the merged tree (CE base + `src/ee`). The
`BackfillNavTab` slot follows the same eager-component pattern as the other
in-Header surfaces (e.g. `GanttChart`, `PipelineActionsProvider`).

### 105. Asset console gated to Team with an open-core "coming soon" page; engine stays free

**Context.** The open-core boundary (ADR #99/#104) is "backfill/ops/governance =
paid; engine = free." Assets sat inconsistently across the seam: the asset
*engine* (SDK asset declarations, partition lambdas, asset tables) is free and
pipelines depend on it, but the asset *console* — the `/assets` view (matrix,
lineage, detail, asset-tabs) plus the single free read route `GET /api/assets` —
was split awkwardly. The OSS UI gated the whole `AssetsView` as a Team slot
(showing the Team-tier fallback) while the OSS backend still served
`GET /api/assets` and the README advertised the asset matrix as free. Net: a
nav tab that led to a "not available" page, a backend route nothing in OSS
called, and docs that disagreed with the build.

**Decision.** The asset **console** stays in Team for now, but instead of reading
as a permanent paid feature it is surfaced in OSS as *coming soon to open-core* —
it is on the graduation roadmap and will return to free feature-by-feature. The
asset **engine** stays free throughout.
1. **Frontend.** The `Assets` nav tab stays **visible** in OSS (not hidden). A
   visit to `/assets` renders a `ComingSoon` placeholder ("Asset console is
   coming in an upcoming release"); the real `AssetsView` lives in `ee/team/` and
   renders only in paid builds. The copy is deliberately **tier-agnostic** (no
   "open-core" wording) so the same notice is reusable in paid builds too.
   `ComingSoon` and `EeFeatureFallback` both render a shared, presentational
   `EmptyState` primitive (icon + title + optional description + action slot;
   styled via `.empty-state` design tokens, so it themes light/dark). They differ
   only in copy and icon: `EeFeatureFallback` marks a permanently Team-tier view
   ("not available in this edition", lock icon), `ComingSoon` signals a
   not-yet-shipped capability ("coming in an upcoming release", sparkles icon).
   (Other paid view-modes — Gantt, calendar — keep the `EeFeatureFallback`
   Team-tier message.)
2. **Backend.** The free `GET /api/assets` route moved out of OSS:
   `routes/assets.py` → `ee/team/assets_list.py` (added to `team.MODULES`), and
   `assets` was dropped from `main.py`'s `ROUTE_MODULES`. The shared
   `_build_assets_from_pipelines` helper moved with it; `ee/team/assets.py`,
   `ee/team/matrix.py`, and their tests now import it from `ee.team.assets_list`.
3. **DAL stays free.** `dal/assets_repo.py` (and its `dal/__init__` re-exports)
   remain in the free tree because the Team asset routes import them via `dal`;
   in the OSS build they are present but unused (harmless), avoiding a `dal`
   restructure.
4. **Engine untouched.** The SDK's asset modules and the SAM asset tables /
   lambdas stay free — OSS pipelines can still produce and wait on assets.
5. **Docs.** README presents the asset console (matrix + lineage) as coming to
   open-core in an upcoming release; the engine is described as already free.

**Reversibility.** This is the РОЗЧЕПЛЕННЯ/overlay model working as intended:
re-freeing an individual asset feature later is a `git mv` from `ee/` into the
free tree (plus a `ROUTE_MODULES`/nav line), not a rewrite (ADR #99).

### 106. Assets are a current-state surface; per-date history lives only on the matrix

**Context.** The asset console read the global date picker (`useAppStore.date`):
the recent-events feed, the queued-events view, and the queue mutations
(skip/clear/trigger/force) all scoped to the selected day. But three of the four
asset tabs are current by nature — lineage is the live dependency graph, the queue
is what is pending *right now*, and "recent events" reads best as the latest
activity, not "events on 2026-07-05." The only genuinely per-date asset view is the
**matrix** (asset × date grid over a configurable range), which already has its own
range control and its own backfill entry point (clicking a missing/failed cell).
So the global date picker was scoping surfaces that don't need a date, and creating
an app-wide date dependency (pipelines + runs + assets) where assets didn't belong.

**Decision.** Assets are a **current-state** surface. Per-date asset history is
inspected on the **matrix** (its own range control), not through a global date.
1. **Backend (Team).** `GET /api/assets/recent-events` and `GET /api/assets/queued`
   no longer take a `date`; they always use today (the events GSI stays efficient —
   it just always queries today's partition). Default recent-events limit is 30. The
   queue mutations (`skip-in-queue`, `clear-queue`) always act on today's queue.
   `asset-trigger` and force-trigger keep a date (`execution_date` / partition key)
   because that is the *materialization* date and the matrix's backfill needs it.
2. **Frontend (Team).** `useAssetsDataQuery()` drops its `date` argument and the
   `assetsData` query key drops the date; the asset mutations drop their `date`
   argument (trigger/force pass today). The recent-events panel shows the latest 30
   in an internally scrolling container so it doesn't stretch the page.
3. **Frontend (free).** The global date picker is removed from `/assets` (it now
   shows only on `/pipelines` and `/runs`). The pipeline cockpit and the Runs
   workspace own their own date controls (redesign batches), after which the topbar
   picker goes away entirely.

**Consequences.** A user can no longer browse *past-day* asset events or queue from
the console; that history is on the matrix (which also opens backfill). This is
consistent with "assets = current" and removes an app-wide date dependency, at the
cost of the rarely-used past-day event/queue lookup, which the matrix covers.

**Follow-up (v0.93.0).** The same confusion outlived this ADR in the last place still
holding the global date: the **Runs** workspace. Its picker wrote `useAppStore.date` —
the *pipeline page's scope* — so clearing it meant "every date" to the feed and "a day
called `''`" to that page, which then rendered a bare graph with the run it should have
shown sitting right there. Runs now owns `runFilter.date` in its own URL, matching Tasks
and the pipeline page's history drawer. **`useAppStore.date` is the pipeline page's scope
and nothing else** — a workspace view must never read it.

Two ideas, one value, is the whole trap: a feed's empty date means *every date*, a page's
empty date means *nothing at all*. They cannot share storage no matter how convenient it
looks. The backend had the mirror image of it — `params.get('date', <today>)` defaults only
when the param is *absent*, while a cleared picker sends it present and empty — so four
routes matched `date=''` literally. Both halves are pinned by guard tests that walk the
code rather than rely on anyone remembering.

### 107. Notifications: one per run, two attention states (failure vs decision-required)

**Context.** The header notification feed scanned task-instance records for
`status == 'failed'` and deduped to one entry per run. Two gaps surfaced in live use:
(1) a task that failed and then **paused awaiting a manual decision** carries status
`waiting_decision`, not `failed`, so it never produced a notification — even though it
is the case that most needs attention (the run is blocked on a human). (2) There was
no distinction between "a task failed" and "the run needs a decision"; conceptually
"task failed" and "pipeline failed" are the same event (a run fails *because* a task
fails), so alerting on both would double-notify. Industry practice (DAG-level
callbacks, Dagster/Prefect run-level alerts) is to alert at the **run level** with the task as context, and
to treat human-gated pauses as their own signal.

**Decision.** Notifications are computed **one per run**, classified into two states:
1. **`failure`** (red) — a task in the run has `status == 'failed'`. Time-windowed on
   `finished_at` (must fall within the query window). Names the failed task as context.
2. **`decision_required`** (orange, `--decision` / `text-orange-500`) — a task in the
   run has `status == 'waiting_decision'`. A paused task has no `finished_at`, so it is
   anchored on `running_at`/`started_at` and always surfaces (it is actively blocking).
3. **Priority:** when a run has both, `decision_required` wins (it needs human action).
   The `notifications.py` filter is `Attr('status').is_in(['failed','waiting_decision'])`
   and per-run state is accumulated (not first-wins) so the decision state can upgrade a
   failure entry for the same run.

**Consequences.** A blocked-on-decision run now alerts (previously silent), and a run
never yields more than one notification regardless of how many tasks failed. We do
*not* add a separate "task failed while the run is still running" notification (kept out
to avoid noise — in-progress task failures are visible on the DAG). The classification
is derived purely from task records; no run-level status record is introduced.

### 108. A pipeline's runs are read through an inverted GSI; the cross-pipeline feed keeps its per-date fan-out

**Context.** Runs are derived, not stored: task rows are grouped by `pipeline_execution`
and the run status comes from `derive_execution_status()` (ADR #112). The only index into
them was `date-pipeline-index` (**HASH `date`**, RANGE `pipeline_name`), which answers
"who ran on day D". Answering the question the UI actually asks — *"this pipeline's runs,
newest first"* — therefore required looping a day at a time, and every caller had to pick
how many days to loop. That produced four dialects of one workaround (`/runs` hardcoded
`sla_days = 14`; `/pipeline-executions` a caller-supplied range; the asset matrix 14/60
**parallel**; the calendar a month) plus a hidden `executions[:50]` cap, and it capped
visible history at an arbitrary window rather than at the data's own lifetime. It also
forced two UI workarounds: a "No executions for {date}" dead end and a "Latest" escape
hatch, both artefacts of the drawer being scoped to a single date.

**Decision.** Add `pipeline-date-index` — the **inverse** of the existing index
(**HASH `pipeline_name`, RANGE `date`**, projection INCLUDE). Both key attributes are
already written on every task row by the wrapper, so this needs **no write-path change,
no new Step Functions state, and no migration** — DynamoDB backfills the index from
existing rows, and old runs are visible immediately. `ExecutionsRepo.query_runs_by_pipeline`
reads it newest-first and pages with an opaque date cursor, cutting only on a date
boundary so an execution's task set is never split across pages.

**The cross-pipeline feed (`/runs` with no pipeline filter) deliberately keeps its
per-date fan-out.** There is nothing to hash on for "everything, newest first"; a
constant partition key (`feed_pk = "RUN"`) would funnel every write into one DynamoDB
partition — the hot-partition antipattern — and would need a new attribute that only new
rows carry, blanking the feed until the old rows age out. **`date` is the natural shard
key for a time-ordered feed**, so the loop there is correct design, not debt. What was
removed is the *dialects*: the window now comes from `Limits.SLA_DAYS` and the worker cap
from `Limits.PARALLEL_DATE_QUERIES` (previously duplicated verbatim in EE `drift.py` and
`matrix.py`).

**Consequences.**
- Depth is governed by the row TTL alone (~31 days today, per the wrapper's `ttl`). Raising
  the TTL deepens visible history **with no code change** — the point of the exercise.
- One extra GSI write per task-row write (PAY_PER_REQUEST) plus INCLUDE-projection storage;
  ~\$0.45/month at ~60k executions/month. No capacity planning.
- The history drawer lists every run it can see, so "Latest" is gone (click the top entry).
  The page still scopes the DAG to a date, so "No executions for {date}" / "View latest run"
  remain — but they are no longer a dead end, since the drawer reaches any run regardless.
- Two access patterns remain, one per question, both behind the DAL. That is deliberate:
  forcing them into one would mean the hot-partition antipattern above.

### 113. The History feed pages on a `started_at` cursor; `next` is the only end-of-feed signal

**Context.** ADR #108 removed the *window* dialects from the runs feed but left the
**cap** untouched: `get_all_runs` ended in `all_runs = all_runs[:limit]`, unconditionally,
on every path. `/api/tasks` did the same with `items[:limit]`, on top of a full-table Scan
whose row order is arbitrary — so "the newest 100 tasks" was the newest 100 of whichever
10,000 rows the Scan happened to read first. The UI then rendered the survivors under the
label `50 runs`, which is not a count of anything: it is the page size wearing a count's
clothes. Nothing in the response distinguished "there are exactly 50 runs" from "there are
5,000 and you may have the first 50", so the UI could not have told the truth even if it
had wanted to, and there was no way to ask for the 51st.

**Decision.** Both feeds page on an opaque **`before` cursor carrying a `started_at`**
("give me what is older than T") and answer with **`next`** — the last returned row's
`started_at`, or `null` when nothing older exists. `null` is the point: a full page is not
an end-of-feed signal, so without an explicit one the UI must either lie or guess.

`started_at` rather than a DynamoDB key, because the runs feed merges two sources with no
common key — executions and Backfills (ADR #95) — whose only shared ordering attribute is
`started_at`, which is also what the feed already sorts by. One timestamp pages the merged
list; a key-based cursor would have to encode both sources, and a composite cursor would
buy nothing the sort order doesn't already give.

The **cursor is a filter, not a scope**: it says which rows are eligible, never where they
come from. Sources stay exactly as ADR #108 left them, and both routes now share all three:
- explicit `date` → one indexed query (already bounded);
- `pipeline`, no date → `pipeline-date-index`, no window (bounded by the row TTL);
- neither → the `Limits.SLA_DAYS` fan-out over `date`, **started at the cursor's date**
  rather than at today. `date` remains the natural shard key for a cross-pipeline time
  series (ADR #108) — the walk is design, not debt, and paging deeper now costs *fewer*
  queries, not more.

Starting the walk at the cursor's date is safe because a row's `started_at` is never
earlier than its logical `date`: no row older than T can hide on a date newer than T's.

**Consequences.**
- `/api/tasks` loses its Scan. It was the more expensive read *and* the less honest one —
  unbounded cost, arbitrary order, and no cursor could have paged it truthfully. Its
  pipeline-filtered path now reads `pipeline-date-index`, so both halves of History reach
  equally far back; its unfiltered path inherits the same SLA window the runs half has
  always had, in exchange for a bounded, ordered read.
- `backfills_repo.list_recent` grew `before`, applied **before** its `limit` slice. After
  it, the newest `limit` records are all that survive to be filtered — which is exactly the
  set any cursor has already served — so Backfills would silently drop out of the feed on
  page two. The cursor filter stays in memory: DynamoDB applies `FilterExpression` after
  reading, so pushing it down saves no capacity, and it would drop records with no
  `started_at` instead of sorting them last, where the rest of the feed puts them.
- **Every bounded source reads one row past the page.** `next` is inferred from having more
  rows than fit, so a source that returns exactly `limit` makes a full page look identical
  to an exhausted feed — reporting end-of-data with rows still behind it, which is the same
  silent truncation this ADR exists to remove, merely moved one layer down. Sources that
  read a whole window or date get the surplus for free; the two bounded ones must ask for
  it: `feed.pipeline_rows_before` requests `want + 1` and stops on `count_fn(rows) > want`
  (strictly), and `_build_backfill_run_rows` asks `list_recent` for `limit + 1`. Landing on
  exactly `limit` is then only reachable by exhausting the source, which is the one case
  where it is true.
- The index read needs a **fill loop** (`feed.pipeline_rows_before`). Its repo cursor is a
  *date*, so on a busy cut date one read returns only rows the cursor already served; a
  single read would hand back a short page and, worse, never advance past that date —
  paging would stall with older runs still behind it. The loop pulls whole dates until it
  overshoots the page or the index runs out. It costs nothing on the first page, where the
  first read is always enough. (The stall needs the repo to hand back a date cursor, which
  needs a >1MB query page — real for a busy pipeline, and the same cursor the history
  drawer already pages on, but not reproducible under `moto`, so it is pinned by a unit
  test against the repo's documented contract rather than end-to-end.)
- The count is now honest, and says so: `50+ runs` while `next` is non-null, `73 runs` once
  it isn't. Two paginations coexist in the footer and are not the same thing — the pager
  walks loaded rows, `Show older` extends the feed.
- Rows without a `started_at` sort last and cannot be a cursor, so a page ending on one
  stops paging. Every task row is stamped at registration and every Backfill at start; this
  is a broken-data guard, not a supported mode.
- **Nothing is ever cut inside a unit, at any layer.** This turned out to be the whole of
  it, and the review found two places that were: the day read cut inside a *pipeline*, and
  the page cut inside an *instant*. The rule is now uniform —
  `query_runs_by_pipeline` cuts on whole dates, `query_runs_by_date` on whole pipelines,
  `page_by_started_at` on whole `started_at` values.
  - The day read was the serious one. `date-pipeline-index` is ordered by `pipeline_name`,
    so a row-count cut lands mid-pipeline — and a run's status is *derived* from the rows
    the caller got (ADR #112), so a split run is not a missing run, it is a **wrong** one:
    a failed run whose failing task fell past the cut renders `success`. Measured at three
    hourly 8-task pipelines (576 rows on one day, cap 500): 23 of 72 failed runs green.
    `ExecutionsRepo.query_runs_by_date` reads whole pipelines and drops the one it stopped
    inside, keeping the read while only one pipeline is buffered so a single busy pipeline
    is never the one dropped. The budgets are therefore floors, not caps
    (`*_MIN_ROWS_*`), and a date that fits one DynamoDB page comes back whole — which
    costs more Lambda memory than the old truncation and is the right trade: the cap was
    never saving capacity anyway (DynamoDB had already returned the page; the truncation
    only threw it away).
  - The page cut was the subtle one. The cursor is a strict `<`, so a row sharing the
    boundary's `started_at` is filtered out by the very cursor meant to fetch it. The page
    absorbs its twins instead. This is what makes a plain timestamp sufficient rather than
    a compromise: a composite cursor would only buy back what the boundary already gives.
- What remains, honestly: on a day too big for one DynamoDB page, the pipelines past the cut
  are still missing from the *unfiltered* feed (filter to a pipeline and they are all there,
  windowless). That is a completeness gap, not a correctness one, and closing it needs a
  per-day cursor over `pipeline_name` — which conflicts with ordering the feed by
  `started_at`. Deliberately not answered here. `query_all` already logs `Hit max_items
  limit`, and `query_runs_by_date` cuts on the same condition, so whether this is live is
  measurable rather than arguable.

### 118. `polyris-deploy --all` / `--only` — bulk deploy and destroy across directories

**Context.** `polyris-deploy` only ever handled one file (default `dag.py`) per invocation
— deploying N example/pipeline directories meant N manual `cd && polyris-deploy` calls,
with no way to batch them, no way to skip a bad one and keep going, and no summary of what
actually happened. This became acute once the example set grew past a handful of
directories (`examples_temp/01_single_task` through `15_assets_every_event`) and needed
redeploying together after a rename sweep.

**Decision.** Two new mutually-exclusive flags: `--all` (every immediate subdirectory of
the current directory) and `--only DIR [DIR ...]` (just the listed ones). Both apply the
same discovery rule per directory: scan **every** `.py` file in it (not only `dag.py`) and
deploy **every** DAG object found in each — a directory may hold more than one independent
pipeline file, and a file with zero DAGs (a shared `config.py`, a `utils.py`) is silently
skipped, not an error. This reuses `_load_dag_from_file`'s existing multi-DAG-per-file
scanning verbatim; nothing new was invented for "how many DAGs can one file hold."

`--select DAG_ID` (pre-existing, for picking one DAG out of a multi-DAG file) composes with
bulk mode: applied independently inside each directory, so a directory that doesn't contain
a DAG with that ID is skipped rather than counted as a failure.

**Naming.** The obvious name for "which folders" was `--select`, but that flag already means
"which DAG, by ID, within one file" and is shared with `polyris-output`, documented in three
places (`CLI.md` ×2, `DEPLOY.md`). Renaming the established flag to free up the name would
be the actual breaking change here, not adding a new one — `--only` was chosen instead,
leaving `--select`'s existing meaning untouched. `--dirs` was considered and rejected as
less natural to read at the call site than `--only`.

**Failure isolation.** One DAG failing must not stop the batch — the whole point of bulk
mode is walking away with a complete picture, not a `sys.exit` on directory #2 of 15. Rather
than refactor every internal `sys.exit(1)` path inside `deploy_pipeline` (validation
failures, AWS errors, and more) into a raised exception, each per-DAG call in the bulk loop
is wrapped in its own `try/except SystemExit`, converting an exit-with-nonzero-code into a
recorded failure and moving on. This is deliberately the least invasive option: it changes
nothing about `deploy_pipeline`'s internals or its single-file callers, and it composes with
any *future* internal exit path the same way, without needing to remember to wrap it.

**Sequential, not parallel — for now.** Deploys run one at a time. Considered and set aside:
concurrent deploys would interleave output from multiple pipelines, need explicit
concurrency limits (CloudFormation and the AWS API throttle), and add real complexity for a
15-pipeline batch that finishes in minutes sequentially. Revisit if bulk batches grow large
enough that sequential time becomes the actual bottleneck.

**Consequences.**
- `--all`/`--only` are mutually exclusive with each other and with `--file` (bulk mode
  discovers files itself — passing both is a usage error, not a silent override).
- `--file`'s CLI default changed internally from the literal `"dag.py"` to `None`, so
  `main()` can tell "not passed" from "passed," and reject `--file` when bulk mode is
  active. Single-file behavior (`polyris-deploy` with no flags) is unchanged; `Path(args.file
  or "dag.py")` reproduces the old default exactly.
- A final summary lists every DAG attempted with ✅/❌, and the process exits non-zero if
  anything failed — safe to gate a CI job or script on.
- `polyris-output`/`polyris-validate` were not given the same bulk treatment — out of scope
  for this decision; revisit if the same need shows up there.

### 119. Unified DAG visualization node/edge builder

**Context.** Two independent implementations built (nodes, edges) for DAG visualization:
`generate_dag_json` (the public `polyris-output`/`polyris-validate` surface) and
`_build_pipeline_metadata`'s `dag_metadata` construction (stored as the `dag` field in both
the pipeline registry and per-execution snapshots — what `/pipeline-dag`'s registry
fallback returns for "current structure" mode, ADR #118's neighbor feature). They looked
similar enough to read as "the same thing, written twice" but had quietly drifted:
`generate_dag_json` had a `type` marker, `trigger_rule` (non-default only), and tracked
Glue/ECS/Athena steps (`dag.steps`) that `_build_pipeline_metadata`'s version entirely
lacked; `_build_pipeline_metadata` had `wait_for` (asset pull-dependency metadata) that
`generate_dag_json` lacked. Neither author appears to have known the other existed —
each accreted fields independently as their own caller needed them.

Found while fixing a narrower bug (task-type badges missing on "current structure"
blueprint nodes, since that data only ever flowed through `_build_pipeline_metadata`'s
path) — patching both call sites in parallel would have re-created the exact condition
that caused the drift in the first place: two implementations, one obligation to keep
them in sync, no mechanism enforcing it.

**Decision.** One shared function, `_build_dag_visualization_nodes_edges(dag)`, returning
the canonical `(nodes, edges)` pair with the **union** of every field either caller had
accumulated — not the intersection, and not "whichever the newer caller happens to need."
Both `generate_dag_json` and `_build_pipeline_metadata` now call it and use its output
directly. A single regression guard test (`test_both_callers_produce_identical_nodes_and_edges`)
asserts their outputs are identical for the same DAG — if this class of drift recurs, it
fails there, immediately, rather than silently producing two different graphs depending
on which endpoint answered the request.

**Consequences.**
- Registry/snapshot-sourced DAG data (used by "current structure" mode, and by historical
  per-execution snapshots) now includes `type`, `trigger_rule`, and tracked Glue/ECS/Athena
  steps it never had before. A pipeline using a direct Glue/ECS/Athena step (not through
  `@task`) previously showed an **incomplete graph** in blueprint mode — that step's node
  was simply missing. Fixed as a consequence of the unification, not a separate patch.
- `generate_dag_json`'s output (`polyris-output --json`, `--graph`) now includes `wait_for`
  on nodes that have it — previously absent from that surface entirely.
- All 27 ASL snapshot tests were regenerated (`SNAPSHOT_UPDATE=1`) to reflect the new
  `type` field appearing in registry-stored `dag_metadata`; the diff in each was
  confirmed to be exactly that one field, nothing else, before regenerating.
- Frontend (`DAGGraphFlow.tsx`) was updated to thread `node.task_type` into the node's
  `data.task` in both the real-task and blueprint-fallback cases — the structural
  `task_type` field now always wins as a fallback when no execution-sourced value exists.

### 120. Manual task actions: recorded input, synthetic output markers, asset events on Mark Successful, safe direct Restart from `waiting_decision`

**Context.** Skip / Mark Successful / Mark Failed / Stop all resolve a task's orchestration
token directly from `console_api`, bypassing every state in the `run_task` wrapper a normal
completion goes through. Investigation (full detail in
`docs/reference/SPIKE_TASK_ACTIONS_DATA_LIFECYCLE.md`) found this had three consequences
beyond the originally-reported "Input tab is empty for a skipped task": a downstream task
calling `xcom.pull()` on a manually-resolved task crashes with `PullError`; the manual
action's immediate `notify_dependents_via_sfn` call can unblock downstream trigger rules
before a subsequent Restart's own outcome is known; and asset events for a task's outlets
are never emitted, so push-triggered downstream pipelines silently never fire. Separately,
a request to make Restart work directly from `waiting_decision` (instead of requiring Stop
first) surfaced a field-name mismatch that made `restart_task`'s "stop the old wrapper"
step a silent no-op for every restart, consequential specifically for this new case since
`waiting_decision`'s wrapper is still genuinely alive.

**Decisions, six pieces:**
1. **`task_input` written on start, not just success** — new `Save_Task_Input` state
   (`run_task/sfn.tpl.json`), positioned after upstream/variables resolve, before
   dispatch. `updateItem`, not `putItem` like `Save_Canonical_Output` — a same-day
   re-run's input write must never clobber a prior run's real `result` at the same
   `output#pipeline#task#date` key.
2. **Synthetic output marker on every manual action** — `{"_manually_resolved": true,
   "_resolution": ..., "_reason": ...}` written to the same canonical key `xcom.pull()`
   reads, conditioned on `result` not already existing. `xcom.pull()`'s own contract
   (raise loud on genuinely missing output) is unchanged — this fills the specific gap
   where "missing" actually means "resolved without running," not "something is wrong."
3. **Asset events emitted for Mark Successful only** — the one action that explicitly
   claims real work happened ("verified via logs/S3"); Skip/Fail/Stop correctly continue
   not to. Looks up outlets from the registry's `dag_metadata` (ADR #119's unified nodes)
   since outlets are a deploy-time property never stored per-execution. Scoped to the
   push-model SFN notification (already fully wired in the SAM template); the pull-based
   subscriber Lambda was not wired — a real infrastructure change (new IAM permission +
   env var), left as a separate decision.
4. **Fixed `restart_task`'s `Stop_Old_Wrapper` field-name mismatch** — it read
   `wrapper_execution_arn`, a field the wrapper never wrote (it writes the same value as
   `run_task_helper_arn`). Harmless for a genuinely terminal restart (nothing left running
   to stop); consequential for restarting directly from `waiting_decision`, where the
   wrapper is still alive. (A first fix attempt used `helper_arn` — also wrong, since
   `:helper_arn` in the wrapper's own `UpdateExpression` is only the value-placeholder
   name, not the field; caught before shipping.)
5. **Attempt-keyed `ConditionExpression` on every status-writing state** (`Save_Success`,
   `Save_Failed`, `Save_Error_Waiting`) — (4) only makes killing a stale wrapper *likely*;
   this makes a stale write from a surviving ghost structurally impossible to apply,
   regardless of whether the kill succeeded. Rejected writes route to a new
   `Stale_Attempt_Superseded` (`Succeed`) state — no misleading finished/failed event, no
   stale dependent notification.
6. **`RESTARTABLE_STATUSES` extended to `waiting_decision`** — Restart now works directly,
   without Stop first. `restart_task_helper`'s `Stop_Old_Wrapper` is a hard
   `states:StopExecution` kill, not `send_task_success`/`failure`, so unlike Stop it never
   triggers `notify_dependents_via_sfn`'s immediate downstream notification — using
   Restart as intended (retry, don't tell anyone yet) no longer risks a downstream task
   reacting to a stale abort before the retry's real outcome is known. Found and fixed a
   related drift bug while wiring this up: the fallback (no-`RESTART_HELPER_ARN`) path had
   its own, separately hand-maintained `RESTART_CONDITION` string that was already missing
   `waiting_decision` — now derived from `RESTARTABLE_STATUSES` directly, eliminating this
   class of drift going forward rather than adding one more hardcoded value to it.

**Consequences.**
- Stop remains available alongside Restart on a `waiting_decision` task — they serve
  different intents ("done, tell everyone now" vs. "let me retry, don't tell anyone yet"),
  not one superseding the other.
- Every new safety property (field-name correctness, catch ordering, the
  `ConditionExpression` guard, the synthetic marker's never-overwrite guard, the
  asset-event gating) was mutation-tested: the corresponding fix was temporarily reverted
  and the new test confirmed to fail, not just pass with the fix in place.
- §7c's pull-based subscriber notification and §7a's "should the marker be visually
  badged in the UI" remain open, deliberately out of scope for this pass.

### 121. Restart correctness: two-level wrapper stop, `task_config`/`outlets` reconstruction, `restart-` name prefix

**Context.** Live testing of ADR #120's direct-Restart-from-`waiting_decision` surfaced
three further, real gaps — full detail in `docs/reference/SPIKE_TASK_ACTIONS_DATA_LIFECYCLE.md`
§9.5–9.6.

**Decisions:**
1. **Two executions must be stopped, in order, not one.** There are two levels: the
   outer `dependency_wrapper` (waits for dependencies, then synchronously calls the inner
   `run_task` via `startExecution.sync:2`) and the inner `run_task` itself. An initial fix
   read `wrapper_execution_arn`, found it unused in `run_task`'s own template, and
   concluded (incompletely — only that one template was checked) it must be a typo for
   `run_task_helper_arn`. It's genuinely written, by `registration_helper`'s
   `Save_Task_Record`, for the outer wrapper. Killing only the inner wrapper directly
   left the outer one alive, synchronously waiting on it — its own `Catch` fired,
   cascading a false `notify_dependents('failed')` to downstream tasks *immediately*,
   before the restarted attempt's own work even began. Confirmed live: a downstream task
   was marked `upstream_failed` within seconds while the restarted task's underlying SFN
   was still genuinely running (and later genuinely succeeded). Fixed: split into
   `Stop_Old_Outer_Wrapper` (external `StopExecution` cannot trigger a wrapper's own
   `Catch`) then `Stop_Old_Inner_Wrapper` (safe once the outer wrapper is already dead).
2. **`task_config`/`outlets` reconstruction.** Both are deploy-time DAG properties, never
   stored on the per-execution record — `Start_New_Wrapper` tried to read
   `item.task_config.S`/`item.outlets.S`, fields that never existed, always silently
   yielding `{}`/`[]`. Fixed at the source: extracted `_build_task_config_and_arn`
   (shared between the original dispatch and the DAG builder, so this can't drift again),
   added `task_config` to the registry's `dag_metadata` (`outlets` was already there per
   ADR #119/#120), and `restart_task`'s Python handler now looks both up via a new shared
   `_lookup_dag_node` (also used by ADR #120's asset-event lookup) and passes them
   explicitly in `restart_task_helper`'s input.
3. **`restart-` name prefix.** Requested directly, to distinguish a restarted attempt's
   underlying business SFN invocation from the original in the AWS console without
   checking duration/timestamps. Only `Run_Task_SFN` needed it — the only `Run_Task_*`
   variant that names a child Step Function execution (the others call AWS services
   directly). Retired the dead `is_restart` flag `Start_New_Wrapper` set (never read
   anywhere) — `attempt > 1` already captures the same thing, more precisely, and is what
   the prefix condition uses.

**Consequences.**
- `task_config` is now visible in `generate_dag_json`'s public output (`polyris-output
  --json`) and the registry, in addition to already being implicitly present in the
  deployed wrapper's own SFN Argument (visible to anyone who can read the deployed
  CloudFormation/SFN definition already). Not currently read or displayed anywhere in the
  UI. Worth keeping in mind if a task's `task_config` is ever used to carry something
  more sensitive than retry/worker settings (e.g. an inline Lambda payload with a
  secret) — that would already be a questionable practice given it's baked into the
  deployed template either way, but this makes it one step easier to notice.
- ASL snapshots regenerated; diffed before accepting to confirm the only change was the
  new `task_config` field appearing on nodes that have one, nothing else.
- Every new behavior (both wrapper levels stopped in order, task_config/outlets
  correctly threaded, the name prefix) was mutation-tested: reverting each fix was
  confirmed to make its corresponding test fail.
- `_build_task_config_and_arn` also gained a direct, isolated unit test suite
  (`tests/sdk/test_task_config_contract.py::TestBuildTaskConfigAndArnDirectly`) —
  previously covered only indirectly through its two callers. Pins the lambda-specific
  `task_arn` resolution (discarded by the existing indirect tests, which only read
  `task_config`), the sfn-stays-empty contract, and retry-key conditionality, in
  isolation from either caller's surrounding construction.
