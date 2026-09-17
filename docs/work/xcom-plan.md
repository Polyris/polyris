# XCom Reliable Data Passing — Plan

> **Historical — implementation plan for polyris 0.100.0, shipped.** Current
> user-facing docs: [`CHANGELOG.md`](../../CHANGELOG.md) 0.100.0 entry,
> [ADR-123](../reference/adr-123-xcom-reliable-data-passing.md) for the
> settled design, and [`task-detail-ui-spike.md`](./task-detail-ui-spike.md)
> for the follow-up UI decisions. Kept here as the frozen technical rationale
> shipped code still cross-references (§X.Y coordinates below).

Last updated: 2026-09-11

Single-PR delivery. Non-breaking. Backward compat maintained throughout.

**Minimum SDK version for new features:** `polyris` 0.100.0+ (SDK-side `xcom.get()` / `xcom.push()` require this; backend/wrapper deploy alone doesn't enable them until user Lambdas/Glue bundles are updated).

**Approvals:** Architect ✅ + Fullstack Dev ✅ + Sr Dev implementation review ✅. **No remaining pre-implementation blockers.**

Changelog v5 (post-final-signoff — 8 inconsistencies + 4 gaps fixed):
- **I1** — §2.0 file order fixed: error classes BEFORE XComArg (both sections consistent)
- **I2** — §4.5 imports explicit: `useState/useEffect/useCallback`, `Info` icon, testing-library
- **I3** — §1.2 JSON comment explains `task_date` (not `date`) for GSI-pollution avoidance
- **I4** — new §2.8 "Coupled constants (SDK ↔ SFN template)" with parity test
- **I5** — §1.2 and §3.2 cross-reference §2.8 coupled constants
- **I6** — §1.6 includes verified list of 15+ `Save_Task_Input` refs in `test_alerting.py` with per-line update instructions
- **I7** — `datetime` import moved from inline to module-level (§2.0 + §2.3 comment)
- **I8** — new "Push wins over Lambda return" documentation note in §6.1
- **G3** — DDB schema for output# and input# rows documented in §6.1 (including `push_count`)
- **G4** — full ADR draft added as §10a
- **G5** — `TODO(polyris-0.102.0)` cleanup comment template added in §4.5
- **G6** — step-by-step backfill stale-marker verification in §12
- LeadingKeys decision: use as-designed (AWS docs confirm), fallback documented if runtime rejects

Changelog v4 (post-Sr-Dev implementation review):
- Verified 8 pre-implementation checks (existing state names, DAL contract, jsonata patterns, CSS refs, `test_xcom_pull` rename impact, TTL flow, `$states.context.Execution.Id` availability)
- **Decision: defer EMR `xcom.push()` support** — no Environment field in `addStep.sync`, injecting via `HadoopJarStep.Args` too invasive. Documented as limitation.
- Changed `Execution.Name` → `Execution.Id` (safer, matches existing convention lines 135, 263)
- Inlined all literal SFN Retry blocks (no more `/* same as ... */` placeholders)
- Added §2.0 explicit file-structure & class-declaration ordering
- Added §1.8 jsonata test harness pattern for `$states.context` expressions
- Added §3.3 `_MultiKeyTable` fixture with full code
- Replaced §4.2 CSS abstract descriptions with concrete rules + existing pattern references (`.bd-status-hint--warning/danger`)
- Added §4.5 full `OnboardingBanner` React implementation (was prose)
- Simplified §1.4 wiring: removed `Route_Canonical_Output` contradiction (Choice happens once)
- Added §11a pre-implementation verification report

Changelog v3 (post-second-review):
- Added IAM policy attachment matrix (Ops persona feedback)
- Improved `xcom.push()` error message for missing `POLYRIS_TASK_NAME` env — actionable hint
- Added §4.5 first-time UI change banner (users notice Console UI shift)
- Added all_done migration example in docs (breaking risk for silent-{} readers)
- Added SDK version requirement statement (users who don't update SDK get no new features)
- Added cost impact note to Breaking-risk audit + CHANGELOG (~$0.30/day per 100k tasks)

Changelog v2 (post-architect-review):
- Added B1/B2/B3 fix: run-versioned push marker, Lambda push warning, `is_internal_record()` filter update
- Split `Save_Task_Input` → `Init_Output_Row` + `Save_Input_Record` (H1/H5)
- Removed premature `key='return_value'` param (H2, Principle #18)
- Documented cross-account push limitation (H3)
- Fixed deploy sequence claim (M4) — `sam deploy` is enough, no per-pipeline
- Added race-condition tests, rollback tests (G6, G7)
- Added breaking-risk audit section (A1)
- Realistic estimate: ~1800 LOC / 4-5 days
- Added commit-by-commit sequence + verification checklist (A3, A4)

---

## Context

### How XCom works today

Two paths for upstream → downstream data:

| Path | Task types | Mechanism | Limit |
|------|-----------|-----------|-------|
| **Runtime injection** | Lambda, SFN | `event["upstream"][dep]["output"]` — pre-fetched by SFN Map | 25KB per dep (SFN 256KB) |
| **`xcom.pull()`** | Glue, ECS, Batch, EMR | Task code reads DDB directly | 350KB (DDB item) |

Storage: DDB `pipeline-tokens`, key `output#{pipeline}#{task}#{date}`, field `result`.
Console visibility: `task_input` field in the same row (currently truncated to 25KB).

### Problems addressed

| # | Problem | Impact |
|---|---------|--------|
| 1 | `$isJson` heuristic wraps primitives in `{_raw: ...}` | LOW (already fixed) |
| 2 | Service tasks (Glue/ECS/Batch) store AWS metadata, not real data | **HIGH** |
| 3 | Console `task_input` 25KB → wipes upstream to `_upstream_omitted` marker | **HIGH — UAT bug** |
| 4 | UI prints raw JSON, no interpretation of markers | **HIGH — UAT bug** |
| 5 | Two reader shapes: `event.upstream[X].output` vs `xcom.pull(X)` | MEDIUM |
| 6 | Missing/failed dep = silent `{}` | MEDIUM |
| 7 | Docs claim "auto S3 offload" — doesn't exist | LOW |
| 8 | `PolyrisResultsBucketRead` IAM points to unused bucket | LOW |

### Size constraints (verified against code)

| Constraint | Value | Location |
|-----------|-------|-------|
| SFN state payload | 256KB | AWS hard limit |
| DDB item | 400KB | AWS hard limit |
| Runtime per-dep truncation | 25KB | `run_task/sfn.tpl.json:297` — real (SFN / 10 deps), keeping |
| DDB `result` field | 350KB | `run_task/sfn.tpl.json:721, 786` — real (DDB item budget), keeping |
| DDB `task_input` field | 25KB | `run_task/sfn.tpl.json:344, 789` — arbitrary, moving to separate record |

### Design decisions (finalized)

- ✅ **Keep** `_s3_ref` reader (`polyris/xcom.py:143-155`) as manual claim-check escape hatch
- ✅ **Add** `xcom.get()` uniform reader + `xcom.push()` for service tasks
- ✅ **Split** `Save_Task_Input` into `Init_Output_Row` + `Save_Input_Record`
- ✅ **Version** push marker with `run_id` to prevent stale-marker corruption
- ✅ **Warn** on `xcom.push()` from Lambda runtime
- ❌ **Not building** auto S3 offload
- ❌ **Remove** misleading `PolyrisResultsBucketRead` IAM
- ❌ **Skip** premature `key='return_value'` multi-key param
- 📄 **Document** cross-account `xcom.push()` as unsupported

---

# Implementation — file-by-file

## 1. SFN template — `sam/sfn_templates/helpers/run_task/sfn.tpl.json`

### 1.1. `$isJson` → `$exists($parse($safe))` in `Get_Dep_Output` ✅ DONE

- **File:** `sam/sfn_templates/helpers/run_task/sfn.tpl.json:297`
- **State:** `Read_Upstream_Outputs.ItemProcessor.States.Get_Dep_Output.Output`
- **Status:** committed. Fixes primitive parsing (`42`, `null`, `[1,2,3]`).
- **Test:** `tests/sdk/test_run_task_template.py::test_get_dep_output_parses_primitive_values` — DONE

### 1.2. Split `Save_Task_Input` into `Init_Output_Row` + `Save_Input_Record`

**Rationale (why split, not just move):**
- Old `Save_Task_Input` did TWO things: (a) set `task_name`, `updated_at`, `ttl` on `output#...` row for Console visibility during running, (b) store `task_input` blob with 25KB truncation
- New design: keep (a) on output# row, move (b) to separate `input#...` record with own 400KB item budget
- Additionally: (a) now also **clears `_pushed_by_task` marker** to prevent stale-marker corruption (fixes B1)

**Replace current state (lines 324-378) with TWO consecutive states:**

```jsonc
"Init_Output_Row": {
  "Type": "Task",
  "Comment": "Initialize output row: set task_name/ttl/updated_at AND clear any stale _pushed_by_task marker from a prior same-date run. Uses updateItem (not putItem) because output# row may already have result from concurrent runs (backfill semantics). REMOVE clause on _pushed_by_task ensures xcom.push() from a previous run doesn't confuse Check_Task_Pushed of the current run (B1 fix).",
  "Resource": "arn:aws:states:::dynamodb:updateItem",
  "Arguments": {
    "TableName": "${tokens_table}",
    "Key": {"execution_name": {"S": "{% 'output#' & $states.input.pipeline_name & '#' & $states.input.task_name & '#' & $states.input.date %}"}},
    "UpdateExpression": "SET task_name = :tn, updated_at = :ua, #ttl_field = if_not_exists(#ttl_field, :ttl), run_id = :rid REMOVE #pushed_marker, pushed_at, pushed_run_id",
    "ExpressionAttributeNames": {
      "#ttl_field": "ttl",
      "#pushed_marker": "_pushed_by_task"
    },
    "ExpressionAttributeValues": {
      ":tn": {"S": "{% $states.input.task_name %}"},
      ":ua": {"S": "{% $now() %}"},
      ":ttl": {"N": "{% $string($states.input.ttl) %}"},
      ":rid": {"S": "{% $states.context.Execution.Id %}"}
    }
  },
  "Retry": [{
    "ErrorEquals": [
      "DynamoDB.ProvisionedThroughputExceededException",
      "DynamoDB.ThrottlingException",
      "DynamoDB.InternalServerError",
      "States.Timeout"
    ],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2,
    "JitterStrategy": "FULL"
  }],
  "Catch": [{
    "ErrorEquals": ["States.ALL"],
    "Comment": "DDB failure - continue; init is best-effort (same pattern as existing template)",
    "Output": "{% $states.input %}",
    "Next": "Save_Input_Record"
  }],
  "Output": "{% $states.input %}",
  "Next": "Save_Input_Record"
},

"Save_Input_Record": {
  "Type": "Task",
  "Comment": "Store task_input (upstream + variables) in a separate DDB record. Uses updateItem (not putItem) — same rationale as Init_Output_Row: concurrent same-date runs (backfill) share this key, blind putItem could clobber a prior run's task_input mid-flight. No 25KB truncation: this record's own DDB item budget is 400KB, allowing ~380KB task_input. NOTE field name: 'task_date' NOT 'date' — deliberately mismatched from date-pipeline-index GSI's key attribute to avoid populating that GSI with internal input# records (is_internal_record prefix filter is belt-and-suspenders; keeping GSI clean matters for query perf).",
  "Resource": "arn:aws:states:::dynamodb:updateItem",
  "Arguments": {
    "TableName": "${tokens_table}",
    "Key": {"execution_name": {"S": "{% 'input#' & $states.input.pipeline_name & '#' & $states.input.task_name & '#' & $states.input.date %}"}},
    "UpdateExpression": "SET task_name = :tn, pipeline_name = :pn, task_date = :td, task_input = :ti, updated_at = :ua, #ttl_field = if_not_exists(#ttl_field, :ttl)",
    "ExpressionAttributeNames": {"#ttl_field": "ttl"},
    "ExpressionAttributeValues": {
      ":tn": {"S": "{% $states.input.task_name %}"},
      ":pn": {"S": "{% $states.input.pipeline_name %}"},
      ":td": {"S": "{% $states.input.date %}"},
      ":ti": {"S": "{% $string({'upstream': $exists($states.input.upstream) ? $states.input.upstream : {}, 'variables': $exists($states.input.variables) ? $states.input.variables : {}}) %}"},
      ":ua": {"S": "{% $now() %}"},
      ":ttl": {"N": "{% $string($states.input.ttl) %}"}
    }
  },
  "Retry": [{
    "ErrorEquals": [
      "DynamoDB.ProvisionedThroughputExceededException",
      "DynamoDB.ThrottlingException",
      "DynamoDB.InternalServerError",
      "States.Timeout"
    ],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2,
    "JitterStrategy": "FULL"
  }],
  "Catch": [{"ErrorEquals": ["States.ALL"], "Output": "{% $states.input %}", "Next": "Check_Task_Type"}],
  "Output": "{% $states.input %}",
  "Next": "Check_Task_Type"
}
```

**Key changes vs old Save_Task_Input:**
- Old: `SET task_name, task_input, updated_at, ttl` on `output#` row with 25KB truncation
- New: Init_Output_Row sets `task_name, updated_at, ttl, run_id` and REMOVES stale marker fields on `output#` row
- New: Save_Input_Record writes `task_input` (no truncation, up to ~380KB) on separate `input#` row

**Backward compat:** old wrapper's `task_input` field on `output#` rows remains readable by Console API (§3 fallback).

**Coupled with SDK:** field names (`_pushed_by_task`, `pushed_run_id`, `pushed_at`) and key formats (`output#...`, `input#...`) must match `polyris/xcom.py`. See §2.8 for full list + parity test.

### 1.3. Remove `task_input` field from `Save_Canonical_Output`

- **File:** `sam/sfn_templates/helpers/run_task/sfn.tpl.json:788-790`
- **Delete** entire `task_input` key from `Save_Canonical_Output.Arguments.Item`. Task_input now lives in separate record (§1.2), no longer in output# row.
- **Result:** `Save_Canonical_Output` writes only `execution_name, task_name, status, result, updated_at, ttl` (removes task_input).

### 1.4. `Save_Success` — respect `_pushed_by_task` marker with run versioning

**Problem re-stated:** `xcom.push()` writes `result` field. `Save_Success` normally overwrites with `task_output` (AWS response for service tasks). Need to detect push and skip overwrite. **But** stale marker from previous run must not confuse current run (B1).

**Solution:** Check_Task_Pushed reads marker + `pushed_run_id`. Only respects marker if `pushed_run_id == current_run_id`. Init_Output_Row (§1.2) also clears marker as belt-and-suspenders.

**Insert new states BEFORE current `Save_Success` (line 699):**

```jsonc
"Check_Task_Pushed": {
  "Type": "Task",
  "Comment": "Check if task called xcom.push() during this run. Compares stored pushed_run_id against current wrapper run_id to reject stale markers (B1). If DDB fails, defaults to _task_pushed=false (safe: wrapper writes result as usual, at worst overwriting a push we didn't detect).",
  "Resource": "arn:aws:states:::dynamodb:getItem",
  "Arguments": {
    "TableName": "${tokens_table}",
    "Key": {"execution_name": {"S": "{% 'output#' & $states.input.pipeline_name & '#' & $states.input.task_name & '#' & $states.input.date %}"}},
    "ProjectionExpression": "#p, pushed_run_id",
    "ExpressionAttributeNames": {"#p": "_pushed_by_task"}
  },
  "Output": "{% $states.input ~> |$| {'_task_pushed': $exists($states.result.Item._pushed_by_task) and $states.result.Item._pushed_by_task.BOOL = true and $exists($states.result.Item.pushed_run_id) and $states.result.Item.pushed_run_id.S = $states.context.Execution.Id} | %}",
  "Next": "Route_Save_Success",
  "Retry": [{
    "ErrorEquals": ["DynamoDB.ProvisionedThroughputExceededException", "DynamoDB.ThrottlingException"],
    "IntervalSeconds": 1, "MaxAttempts": 2, "BackoffRate": 2
  }],
  "Catch": [{
    "ErrorEquals": ["States.ALL"],
    "Comment": "Best-effort: on DDB failure treat as not pushed (safe default)",
    "Output": "{% $states.input ~> |$| {'_task_pushed': false} | %}",
    "Next": "Route_Save_Success"
  }]
},

"Route_Save_Success": {
  "Type": "Choice",
  "Choices": [{"Condition": "{% $states.input._task_pushed = true %}", "Next": "Save_Success_Preserve"}],
  "Default": "Save_Success"
},

"Save_Success_Preserve": {
  "Type": "Task",
  "Comment": "Task pushed via xcom.push() — preserve stored result, only update status/finished_at/task_execution_arn. Keeps stale-attempt guard (attempt = :expectedAttempt) identical to Save_Success.",
  "Resource": "arn:aws:states:::dynamodb:updateItem",
  "Arguments": {
    "TableName": "${tokens_table}",
    "Key": {"execution_name": {"S": "{% $states.input.execution_name %}"}},
    "UpdateExpression": "SET #s = :status, finished_at = :finished, task_execution_arn = :taskArn",
    "ConditionExpression": "attempt = :expectedAttempt",
    "ExpressionAttributeNames": {"#s": "status"},
    "ExpressionAttributeValues": {
      ":status": {"S": "success"},
      ":finished": {"S": "{% $now() %}"},
      ":taskArn": {"S": "{% $exists($states.input.task_execution_arn) ? $states.input.task_execution_arn : '' %}"},
      ":expectedAttempt": {"N": "{% $string($exists($states.input.attempt) ? $states.input.attempt : 1) %}"}
    }
  },
  "Output": "{% $states.input ~> |$| {'final_status': 'success'} | %}",
  "Next": "Save_Canonical_Output_Preserve",
  "Retry": [{
    "ErrorEquals": [
      "DynamoDB.ProvisionedThroughputExceededException",
      "DynamoDB.ThrottlingException",
      "DynamoDB.InternalServerError",
      "States.Timeout"
    ],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2,
    "JitterStrategy": "FULL"
  }],
  "Catch": [
    {"ErrorEquals": ["DynamoDB.ConditionalCheckFailedException"], "Output": "{% $states.input %}", "Next": "Stale_Attempt_Superseded"},
    {"ErrorEquals": ["States.ALL"], "Output": "{% $states.input %}", "Next": "Emit_Task_Finished_Success"}
  ]
},

"Save_Canonical_Output_Preserve": {
  "Type": "Task",
  "Comment": "Task pushed via xcom.push() — canonical row already has correct result. UpdateItem only touches status/updated_at (not putItem, which would clobber pushed result).",
  "Resource": "arn:aws:states:::dynamodb:updateItem",
  "Arguments": {
    "TableName": "${tokens_table}",
    "Key": {"execution_name": {"S": "{% 'output#' & $states.input.pipeline_name & '#' & $states.input.task_name & '#' & $states.input.date %}"}},
    "UpdateExpression": "SET #s = :status, updated_at = :ua, #ttl_field = if_not_exists(#ttl_field, :ttl)",
    "ExpressionAttributeNames": {"#s": "status", "#ttl_field": "ttl"},
    "ExpressionAttributeValues": {
      ":status": {"S": "success"},
      ":ua": {"S": "{% $now() %}"},
      ":ttl": {"N": "{% $string($states.input.ttl) %}"}
    }
  },
  "Output": "{% $states.input %}",
  "Next": "Emit_Task_Finished_Success",
  "Retry": [{
    "ErrorEquals": [
      "DynamoDB.ProvisionedThroughputExceededException",
      "DynamoDB.ThrottlingException",
      "DynamoDB.InternalServerError",
      "States.Timeout"
    ],
    "IntervalSeconds": 2,
    "MaxAttempts": 3,
    "BackoffRate": 2,
    "JitterStrategy": "FULL"
  }],
  "Catch": [{"ErrorEquals": ["States.ALL"], "Output": "{% $states.input %}", "Next": "Emit_Task_Finished_Success"}]
}
```

**Verified downstream states:**
- `Emit_Task_Finished_Success` (line 826) — takes `$states.input`, needs fields: `task_run_id`, `execution_name`, `pipeline_execution`, `task_name`, `ttl`, `parent_execution_id`, `backfill_id`, `partition_key`, `current_date`. **All flow through** from Save_Success_Preserve's `$states.input ~> |$| {'final_status':'success'} |` output. ✓
- `Stale_Attempt_Superseded` (line 695) — `Type: Succeed`, accepts any input. ✓

**Final flow — simplified (no `Route_Canonical_Output`):**
```
Task_X_Success → Check_Task_Pushed → Route_Save_Success →
  ├─ (default) Save_Success → Save_Canonical_Output → Emit_Task_Finished_Success
  └─ (pushed) Save_Success_Preserve → Save_Canonical_Output_Preserve → Emit_Task_Finished_Success
```

**Wire changes to existing template (verified line numbers):**

Each `Run_Task_*` state currently has `"Next": "Save_Success"` — change to `"Next": "Check_Task_Pushed"`:
- `Run_Task_SFN` — line 434
- `Run_Task_Lambda` — line 469
- `Run_Task_Glue` — line 503
- `Run_Task_ECS` — line 523
- `Run_Task_Athena` — line 543 (via Check_Should_Retry earlier)
- `Run_Task_EMR` — line 574
- `Run_Task_Batch` — line 600

`Save_Success.Next` stays `Save_Canonical_Output` — no change (default path).
`Save_Success_Preserve.Next` = `Save_Canonical_Output_Preserve`.
`Save_Canonical_Output_Preserve.Next` = `Emit_Task_Finished_Success`.

**No `Route_Canonical_Output` needed** — Choice happens once (Route_Save_Success), and each branch has its own canonical write. Direct wiring keeps flow clear.

### 1.5. Add `POLYRIS_TASK_NAME` and `POLYRIS_WRAPPER_RUN_ID` env variables

**Verified lines:**
- `Run_Task_Glue.Arguments.Arguments` — **line 492**: add both to args dict
- `Run_Task_ECS.Arguments.Overrides.ContainerOverrides.Environment` — **line 512**: add both env entries
- `Run_Task_Batch.ContainerOverrides.Environment` — **line 588**: add both env entries
- `Run_Task_EMR` — **line 545-563**: **special case** (see below)

**Glue (line 492) — full replacement:**
```jsonc
"Arguments": "{% $merge([{
  'JobName': $states.input.task_config.job_name,
  'Arguments': $merge([
    $exists($states.input.task_config.arguments) ? $states.input.task_config.arguments : {},
    {
      '--POLYRIS_PIPELINE_NAME': $states.input.pipeline_name,
      '--POLYRIS_RUN_DATE': $states.input.date,
      '--POLYRIS_TOKENS_TABLE': '${tokens_table}',
      '--POLYRIS_TASK_NAME': $states.input.task_name,
      '--POLYRIS_WRAPPER_RUN_ID': $states.context.Execution.Id
    }
  ])
}, ...rest unchanged...]) %}"
```

**ECS (line 512) — add two entries to Environment array:**
```jsonc
$penv := [
  {'Name': 'POLYRIS_PIPELINE_NAME', 'Value': $states.input.pipeline_name},
  {'Name': 'POLYRIS_RUN_DATE', 'Value': $states.input.date},
  {'Name': 'POLYRIS_TOKENS_TABLE', 'Value': '${tokens_table}'},
  {'Name': 'POLYRIS_TASK_NAME', 'Value': $states.input.task_name},
  {'Name': 'POLYRIS_WRAPPER_RUN_ID', 'Value': $states.context.Execution.Id}
];
```

**Batch (line 588) — same shape as ECS in ContainerOverrides.**

**EMR (line 545) — special case, no direct env mechanism:**

EMR `addStep.sync` has no `Environment` field — only `Step.HadoopJarStep.Args` (command-line args to Spark/Hadoop). Two options:

- **(a) Inject as CLI args:** append `--POLYRIS_TASK_NAME <name>` and `--POLYRIS_WRAPPER_RUN_ID <id>` to `Args` array. User's Spark code must read them from `sys.argv`. **Breaking:** user's existing arg-parser must accept unknown args.
- **(b) Document EMR `xcom.push()` as unsupported in this PR** — deferred to future work. EMR is niche.

**Decision: (b)** — defer EMR push support. Rationale:
- EMR users are a small fraction of the polyris user base
- CLI arg injection is invasive and might break existing Spark parsers
- EMR steps typically write results to S3 anyway (their natural output pattern)
- Documented as future work in DATA_PASSING.md limitations section

**Reason for POLYRIS_ prefix:** matches existing convention (POLYRIS_PIPELINE_NAME, POLYRIS_RUN_DATE, POLYRIS_TOKENS_TABLE).

**Backward compat:** new env vars, no collision. Old task code ignores them. Tasks that don't call `xcom.push()` don't need them.

**Verified:** `$states.context.Execution.Id` (full ARN) is used elsewhere in template (lines 135, 263) — safer than `.Name` (short) for uniqueness. Use `.Id` throughout.

### 1.6. Test coverage for SFN template changes

- **File:** `tests/sdk/test_run_task_template.py` (extend)
- **Add tests:**
  - `test_init_output_row_clears_stale_push_marker` — verify REMOVE clause
  - `test_save_input_record_uses_input_key_prefix` — verify key = "input#..."
  - `test_save_input_record_no_truncation` — 200KB task_input goes through
  - `test_save_input_record_uses_updateitem_not_putitem` — concurrent-safe semantics
  - `test_save_input_record_uses_task_date_field_not_date` — regression on GSI-pollution fix (I3)
  - `test_save_canonical_output_no_task_input_field` — field removed
  - `test_check_task_pushed_reads_marker_and_run_id` — projection includes both
  - `test_check_task_pushed_requires_matching_run_id` — stale marker → _task_pushed=false
  - `test_save_success_preserves_result_when_pushed` — SET missing #r
  - `test_save_canonical_output_preserve_uses_updateitem` — not putItem
  - `test_route_save_success_defaults_to_current_when_not_pushed`
  - `test_polyris_task_name_env_reaches_glue_arguments`
  - `test_polyris_task_name_env_reaches_ecs_container_env`
  - `test_polyris_task_name_env_reaches_batch_container_env`

**Existing tests to update — `tests/backend/test_alerting.py`** (verified via grep, I6):
15+ references to `Save_Task_Input` state across 2 test methods:
- Line 293-309 (retry loop feeders assertion): update `{'Save_Task_Input', 'Increment_Retry'}` → `{'Save_Input_Record', 'Increment_Retry'}` (last state in the chain before Check_Task_Type)
- Line 317-320 (linear chain): update `Prepare_Task_Input['Next']` assertion from `'Save_Task_Input'` → `'Init_Output_Row'`; add new assertion `Init_Output_Row['Next'] == 'Save_Input_Record'`; keep `Save_Input_Record['Next'] == 'Check_Task_Type'`
- Line 704-754 (task_input write specifics): update `self.rt['States']['Save_Task_Input']` → split into two:
  - `self.rt['States']['Init_Output_Row']` for task_name/ttl/clear-marker assertions
  - `self.rt['States']['Save_Input_Record']` for task_input write + `input#` key prefix + no-truncation assertions
- Line 724 (key format assertion): update expected key from `'output#...'` → `'input#...'` (for Save_Input_Record) or keep `'output#...'` (for Init_Output_Row)

**Grep command for coder to re-run before starting §1.2:**
```bash
grep -rn "Save_Task_Input" /home/makskoval/my/sam/oss/polyris/tests \
    /home/makskoval/my/sam/oss/polyris/sam/lambdas/*/tests 2>/dev/null | grep -v __pycache__
```

### 1.7. Reachability check

- **Existing:** `tests/sdk/test_run_task_template.py:139` walks state graph.
- **Verify:** new states (`Init_Output_Row`, `Save_Input_Record`, `Check_Task_Pushed`, `Route_Save_Success`, `Save_Success_Preserve`, `Save_Canonical_Output_Preserve`) are reachable from StartAt.

### 1.8. Test harness pattern for `$states.context` expressions

**Problem:** jsonata-python (`tests/sdk/test_run_task_template.py`) only binds `$states.input` and `$states.result` via `j.assign("states", ...)`. Nothing today binds `$states.context.Execution.Id` — new states use it.

**Solution — add helper to `test_run_task_template.py`:**
```python
def _eval_with_context(template, state_path, wrapper_input,
                       context_execution_id="arn:aws:states:us-east-1:111111111111:execution:wrapper:test-run-id",
                       result_dict=None):
    """Evaluate a state's Output (or Arguments) with $states.context bound.

    Use for Init_Output_Row, Save_Input_Record, Check_Task_Pushed which read
    $states.context.Execution.Id for run versioning.
    """
    jsonata = pytest.importorskip("jsonata")
    # state_path is a dotted path like "Read_Upstream_Outputs.Output"
    node = template["States"]
    for part in state_path.split("."):
        node = node[part]
    expr = node
    body = expr[2:-2].strip() if isinstance(expr, str) and expr.startswith("{%") else expr
    j = jsonata.Jsonata(body)
    states = {
        "input": wrapper_input,
        "context": {"Execution": {"Id": context_execution_id}},
    }
    if result_dict is not None:
        states["result"] = result_dict
    j.assign("states", states)
    return j.evaluate({})
```

**Use in new tests:**
```python
def test_check_task_pushed_matches_run_id(template):
    output_expr = template["States"]["Check_Task_Pushed"]["Output"]
    result_matching = {"Item": {
        "_pushed_by_task": {"BOOL": True},
        "pushed_run_id": {"S": "arn:aws:states:us-east-1:111111111111:execution:wrapper:test-run-id"},
    }}
    output = _eval_with_context(
        template, "Check_Task_Pushed.Output",
        wrapper_input={"pipeline_name": "p", "task_name": "t", "date": "d"},
        result_dict=result_matching,
    )
    assert output["_task_pushed"] is True

def test_check_task_pushed_rejects_stale_run_id(template):
    result_stale = {"Item": {
        "_pushed_by_task": {"BOOL": True},
        "pushed_run_id": {"S": "arn:...:execution:wrapper:OLD-run"},
    }}
    output = _eval_with_context(
        template, "Check_Task_Pushed.Output",
        wrapper_input={"pipeline_name": "p", "task_name": "t", "date": "d"},
        result_dict=result_stale,
    )
    assert output["_task_pushed"] is False  # stale marker rejected
```

---

## 2. SDK — `polyris/xcom.py`

### 2.0. File structure & order (explicit)

**Rationale:** existing `pull()` (line ~80) raises `PullError`. When `PullError` becomes an alias for `XComMissingError`, all classes must be defined BEFORE `_resolve()` (which raises `PullError`) and before `pull()`.

**Final file order (top-to-bottom):**

```
1. Module docstring (rewrite per §6.7)
2. Imports:
   - stdlib: json, os, warnings
   - stdlib: from datetime import datetime, timezone  # NEW: module-level (I7)
   - typing: Optional, Any, TYPE_CHECKING
3. Env var constants: ENV_PIPELINE, ENV_DATE, ENV_TABLE, ENV_TASK_NAME (new), ENV_RUN_ID (new)
4. Constant: _PUSH_MARKER_FIELD = "_pushed_by_task"
5. Error classes: XComError → XComMissingError → XComUpstreamFailedError → XComTruncatedError (new, §2.1)
   ← This replaces existing `class PullError(RuntimeError)` at line 32-33
6. Backward-compat alias: PullError = XComMissingError (immediately after error classes)
7. XComArg class (existing, currently line 36-51 — keep verbatim, just shift down)
8. Private helpers: _resolve (existing 54), _resolve_s3_pointer (existing 72), _get_from_ddb (new, §2.2)
9. Public API: pull (existing 80), get (new, §2.2), push (new, §2.3)
10. __all__ list (new, §2.5)
```

**Insertion instructions for coder:**
1. Delete existing `class PullError(RuntimeError): ...` at line 32-33
2. At same line 32, insert error class hierarchy from §2.1 (XComError → XComMissingError → XComUpstreamFailedError → XComTruncatedError)
3. Immediately after (still before XComArg), add `PullError = XComMissingError` alias line
4. `XComArg` class (line 36-51) shifts down but content unchanged
5. `_resolve()` and everything below shifts down — no logical changes

### 2.1. Error classes

**Location:** insert at line 32-33 (replacing old `class PullError` definition; BEFORE XComArg at line 36).

```python
class XComError(RuntimeError):
    """Base class for all XCom errors."""

class XComMissingError(XComError):
    """Upstream task has no stored output."""
    def __init__(self, task_name, pipeline=None, date=None):
        self.task_name = task_name
        self.pipeline = pipeline
        self.date = date
        msg = f"no output stored for task '{task_name}'"
        if pipeline and date:
            msg += f" (pipeline '{pipeline}', date '{date}')"
        msg += " — did the task run and return anything?"
        super().__init__(msg)

class XComUpstreamFailedError(XComError):
    """Upstream task did not succeed."""
    def __init__(self, task_name, status):
        self.task_name = task_name
        self.status = status
        super().__init__(
            f"upstream '{task_name}' did not succeed (status: {status}). "
            f"Pass raise_on_failure=False to xcom.get() to opt into reading its output."
        )

class XComTruncatedError(XComError):
    """Upstream output too large for inline transport."""
    def __init__(self, task_name, size_bytes=None):
        self.task_name = task_name
        self.size_bytes = size_bytes
        msg = f"output for task '{task_name}' is truncated"
        if size_bytes:
            msg += f" ({size_bytes} bytes)"
        msg += " — use Claim Check pattern: write to S3 and return {'_s3_ref': 's3://...'}. See docs/features/DATA_PASSING.md#large-outputs."
        super().__init__(msg)

# Backward compat alias — old code importing PullError still works
PullError = XComMissingError  # NOTE: this replaces the old class defined at line 32
```

**Change to line 32-33:** delete the old `class PullError(RuntimeError)` — replaced by alias above.

**Risk (from A1 breaking audit):** if any user code did `class MyError(PullError): pass` — still works (`PullError` is still a class). If any code did `isinstance(e, RuntimeError)` — still works (`XComMissingError` extends `RuntimeError` via `XComError`). **Safe.**

### 2.2. `xcom.get()` — uniform reader

```python
def get(event=None, task_name=None, *,
        raise_on_missing=True, raise_on_failure=True,
        ddb_client=None, s3_client=None):
    """Read upstream task output. Universal API for Lambda / Glue / ECS / Batch / EMR.

    In Lambda / SFN handlers: reads from pre-fetched event.upstream first (fast).
    Falls back to DDB pull() for undeclared deps or truncated inline data.

    In Glue / ECS / Batch / EMR: pass event=None, reads from DDB directly.

    Args:
        event: Lambda handler event, or None for service tasks.
        task_name: upstream task_id to read from.
        raise_on_missing: if True (default), raise XComMissingError for missing dep.
        raise_on_failure: if True (default), raise XComUpstreamFailedError if upstream status != success.
        ddb_client, s3_client: injected for tests; created on demand otherwise.

    Returns:
        The upstream output (dict, list, primitive, None), whatever shape it stored.

    Raises:
        XComMissingError: dep has no recorded output (default; opt out via raise_on_missing=False).
        XComUpstreamFailedError: upstream status != success (default; opt out via raise_on_failure=False).
        XComTruncatedError: both event and DDB paths returned truncated markers.
    """
    if not task_name:
        raise ValueError("xcom.get() requires task_name")

    # 1. Try Lambda/SFN pre-fetched inject
    if event and isinstance(event, dict):
        upstream = event.get("upstream")
        if isinstance(upstream, dict) and task_name in upstream:
            entry = upstream[task_name]
            if not isinstance(entry, dict):
                # Legacy raw shape or malformed — pass through
                return entry

            status = entry.get("status", "unknown")
            output = entry.get("output")

            # Missing / not-yet-run (Get_Dep_Output returns {output: {}, status: "unknown"})
            if status == "unknown":
                if raise_on_missing:
                    raise XComMissingError(task_name)
                return None

            # Non-success status
            if status != "success":
                if raise_on_failure:
                    raise XComUpstreamFailedError(task_name, status)
                # fall through to return output as-is

            # Truncated marker in inject path — try DDB (which may have full data)
            if isinstance(output, dict) and output.get("_truncated"):
                try:
                    return _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing)
                except XComMissingError:
                    # DDB also empty/missing — original truncation stands
                    raise XComTruncatedError(task_name, output.get("_size"))

            # S3 claim-check pointer — auto-resolve
            if isinstance(output, dict) and "_s3_ref" in output:
                if s3_client is None:
                    import boto3  # pragma: no cover
                    s3_client = boto3.client("s3")  # pragma: no cover
                return _resolve_s3_pointer(output["_s3_ref"], s3_client)

            return output

    # 2. Fallback: DDB read (Glue/ECS/Batch, or Lambda undeclared dep)
    return _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing)


def _get_from_ddb(task_name, event, ddb_client, s3_client, raise_on_missing):
    """DDB-based fallback: wraps pull() with unified error handling."""
    try:
        return pull(task_name, event, ddb_client=ddb_client, s3_client=s3_client)
    except PullError as e:
        # pull() raises PullError for: no context, no item, unreadable JSON, truncated
        msg = str(e)
        if not raise_on_missing and ("no output stored" in msg or "no context" in msg):
            return None
        if "truncated" in msg:
            raise XComTruncatedError(task_name) from e
        raise  # PullError == XComMissingError alias; propagates as expected
```

**Design notes:**
- No `key='return_value'` param (H2 fix — Principle #18)
- Falls back to DDB when event has `_truncated` marker (uses larger DDB budget)
- Auto-resolves `_s3_ref` in event path (DDB path already does this via `pull()`)
- Missing detection uses ONLY `status == "unknown"` (M5 fix — empty dict is valid user output)

**Placement in file:** insert `_get_from_ddb()` helper immediately after `_resolve_s3_pointer` (current line 72), then insert public `get()` immediately after existing `pull()` (current line ~155). Keep `push()` after `get()`.

### 2.3. `xcom.push()` — writer for service tasks

```python
_PUSH_MARKER_FIELD = "_pushed_by_task"
ENV_TASK_NAME = "POLYRIS_TASK_NAME"
ENV_RUN_ID = "POLYRIS_WRAPPER_RUN_ID"  # SFN wrapper execution name


def push(value, *,
         pipeline=None, task=None, date=None, table=None, run_id=None,
         ddb_client=None):
    """Store this task's output for downstream reading.

    Required for service tasks (Glue/ECS/Batch/EMR) whose wrapper only sees
    AWS API response (JobRunId etc.) not the actual work output. Lambda tasks
    should prefer `return value` — see warning below.

    Args:
        value: JSON-serializable output (dict, list, primitive, None).
        pipeline / task / date / table / run_id: context, from POLYRIS_* env vars if omitted.
        ddb_client: injected for tests.

    Raises:
        XComError: if value is not JSON-serializable, or serialized > 350KB.
        ClientError: if DDB UpdateItem fails (permission / throttling).

    Note (from Lambda):
        `xcom.push()` from a Lambda handler races with the wrapper's Save_Success
        write. Prefer `return value` from Lambda. This function emits a UserWarning
        if AWS_LAMBDA_FUNCTION_NAME is set.

    Note (cross-account):
        Cross-account tasks (task's role in a different account) cannot call
        xcom.push() without additional IAM setup. See docs.
    """
    import warnings

    # B2 fix — Lambda-runtime warning
    if os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        warnings.warn(
            "xcom.push() called from Lambda handler. Prefer 'return value' — "
            "async push after handler return may race with wrapper's Save_Success. "
            "See docs/features/DATA_PASSING.md#lambda-write-pattern.",
            UserWarning,
            stacklevel=2,
        )

    # L1 fix — clear json error message
    try:
        serialized = json.dumps(value)
    except TypeError as e:
        raise XComError(
            f"xcom.push() value must be JSON-serializable: {e}. "
            "Convert datetimes to strings, Decimals to floats, etc. before pushing."
        ) from e

    if len(serialized) > 350_000:
        raise XComError(
            f"xcom.push() value is {len(serialized)} bytes, exceeds 350KB DDB item limit. "
            "Write to S3 and push {'_s3_ref': 's3://...'} instead. "
            "See docs/features/DATA_PASSING.md#large-outputs."
        )

    ctx = {}
    pipeline = _resolve(pipeline, ctx, (), ENV_PIPELINE, "pipeline name")
    date = _resolve(date, ctx, (), ENV_DATE, "run date")
    table = _resolve(table, ctx, (), ENV_TABLE, "table name")

    # POLYRIS_TASK_NAME and POLYRIS_WRAPPER_RUN_ID are NEW env vars (SFN template §1.5)
    # — old wrapper pre-dating this PR does not set them. Give clear actionable hint.
    try:
        task = _resolve(task, ctx, (), ENV_TASK_NAME, "task name")
    except PullError:
        raise XComError(
            f"xcom.push() requires the '{ENV_TASK_NAME}' environment variable. "
            "This env var is set by the polyris wrapper — if you're seeing this error, "
            "your pipeline is running under an older wrapper. Run `sam deploy` on the "
            "polyris SAM template to update the wrapper (no per-pipeline redeploy needed). "
            "If you're testing xcom.push() locally, pass task=... explicitly."
        ) from None
    try:
        run_id = _resolve(run_id, ctx, (), ENV_RUN_ID, "wrapper run id")
    except PullError:
        raise XComError(
            f"xcom.push() requires the '{ENV_RUN_ID}' environment variable "
            "(prevents stale push marker corruption across backfill runs). "
            "This env var is set by the polyris wrapper — run `sam deploy` to update it."
        ) from None

    if ddb_client is None:
        import boto3  # pragma: no cover
        ddb_client = boto3.client("dynamodb")  # pragma: no cover

    # NOTE: `datetime, timezone` imported at module top (§2.0 file structure)
    key_name = f"output#{pipeline}#{task}#{date}"
    ddb_client.update_item(
        TableName=table,
        Key={"execution_name": {"S": key_name}},
        UpdateExpression=(
            "SET #r = :r, #p = :p, pushed_at = :now, pushed_run_id = :rid, "
            "task_name = :tn ADD push_count :one"
        ),
        ExpressionAttributeNames={"#r": "result", "#p": _PUSH_MARKER_FIELD},
        ExpressionAttributeValues={
            ":r": {"S": serialized},
            ":p": {"BOOL": True},
            ":now": {"S": datetime.now(timezone.utc).isoformat()},
            ":rid": {"S": run_id},
            ":tn": {"S": task},
            ":one": {"N": "1"},
        },
    )
```

**New env var contract:**
- `POLYRIS_TASK_NAME` — added by SFN template §1.5 for all service task types
- `POLYRIS_WRAPPER_RUN_ID` — SFN execution name of the wrapper for this run

**§1.5 addendum:** also add `POLYRIS_WRAPPER_RUN_ID` env var. Value: `{% $states.context.Execution.Id %}` (the wrapper's own execution name).

### 2.4. `pull()` — no behavior change

- Existing `pull()` (lines 80-155) unchanged.
- Line 32-33 `class PullError` → replaced by alias `PullError = XComMissingError` after error classes definition.
- Docstring (lines 90-112) update: mention `xcom.get()` as recommended alternative, `pull()` as low-level.

### 2.5. Module exports

- **File:** `polyris/xcom.py` — add `__all__`:
  ```python
  __all__ = [
      'XComArg', 'PullError',
      'XComError', 'XComMissingError', 'XComUpstreamFailedError', 'XComTruncatedError',
      'get', 'push', 'pull',
      'ENV_PIPELINE', 'ENV_DATE', 'ENV_TABLE', 'ENV_TASK_NAME', 'ENV_RUN_ID',
  ]
  ```

### 2.6. Tests

- **File:** rename `tests/sdk/test_xcom_pull.py` → `tests/sdk/test_xcom.py`
- **Keep** all 20+ existing `pull()` tests as regression protection
- **Add for `get()`:**
  - `test_get_from_event_upstream_dict` — pre-fetched dict
  - `test_get_from_event_upstream_primitive` — post $isJson fix
  - `test_get_from_event_upstream_none` — `output: null`
  - `test_get_missing_status_unknown_raises_missing` — status="unknown" → XComMissingError
  - `test_get_missing_status_unknown_no_raise_returns_none` — raise_on_missing=False
  - `test_get_failed_status_raises_failure` — status="failed" → XComUpstreamFailedError
  - `test_get_failed_no_raise_returns_output` — raise_on_failure=False + get failed output
  - `test_get_success_with_empty_dict_returns_empty_dict` — status="success", output={} → valid empty (M5 fix)
  - `test_get_event_truncated_falls_back_to_pull` — event has `{_truncated}` → get() calls pull(), returns full data from DDB
  - `test_get_both_truncated_raises_truncated_error` — event AND DDB truncated → XComTruncatedError
  - `test_get_s3_ref_in_event_auto_resolves` — event has `{_s3_ref}` → S3 fetched
  - `test_get_undeclared_dep_falls_back_to_pull` — task_name not in event.upstream → get() calls pull()
  - `test_get_no_event_uses_pull` — Glue/ECS: event=None → pull()
  - `test_get_missing_task_name_raises_value_error` — task_name=None → ValueError
- **Add for `push()`:**
  - `test_push_writes_updateitem_with_marker` — verify UpdateExpression contains _pushed_by_task
  - `test_push_writes_pushed_run_id` — for B1 stale-marker rejection
  - `test_push_serializes_dict` — real json.dumps
  - `test_push_serializes_primitive` — int, None, list
  - `test_push_non_serializable_raises_xcom_error` — L1: datetime not serializable → clear error
  - `test_push_too_large_raises_with_s3_hint` — 400KB value → XComError mentions S3
  - `test_push_reads_context_from_env` — POLYRIS_TASK_NAME etc.
  - `test_push_missing_env_raises_clear_error` — no POLYRIS_TASK_NAME → clear error mentions env var
  - `test_push_from_lambda_emits_warning` — B2: AWS_LAMBDA_FUNCTION_NAME set → UserWarning
  - `test_push_from_glue_no_warning` — env unset → no warning
- **Add for aliases:**
  - `test_pull_error_is_xcom_missing_error_alias` — `PullError is XComMissingError`
  - `test_existing_pull_still_works_end_to_end` — regression
- **Add for race scenarios (G6):**
  - `test_push_then_pull_returns_pushed_value` — same test process: push then immediately pull → get pushed
  - `test_two_pushes_last_wins` — sequential pushes → last value stored (update_count=2)

### 2.8. Coupled constants (SDK ↔ SFN template) — **maintenance critical**

The following field names and key formats are hardcoded in **BOTH** `polyris/xcom.py` AND `sam/sfn_templates/helpers/run_task/sfn.tpl.json`. They MUST be kept in sync — silent divergence causes data corruption (marker never detected → wrapper overwrites pushed value).

| Coupled item | Value | SDK location | SFN location |
|--------------|-------|--------------|--------------|
| Push marker field name | `_pushed_by_task` | `xcom.py` `_PUSH_MARKER_FIELD` constant | `Init_Output_Row` ExpressionAttributeNames `#pushed_marker`; `Check_Task_Pushed` projection + condition |
| Push run-id field name | `pushed_run_id` | `xcom.py` `push()` UpdateExpression `:rid` alias | `Check_Task_Pushed` projection + Output condition; `Init_Output_Row` REMOVE |
| Push timestamp field name | `pushed_at` | `xcom.py` `push()` UpdateExpression | `Init_Output_Row` REMOVE |
| Push count field name | `push_count` | `xcom.py` `push()` ADD clause | (read-only, debug — Console UI may show) |
| Output key format | `output#{pipeline}#{task}#{date}` | `xcom.py` `push()` + `pull()` key building | `Init_Output_Row`, `Check_Task_Pushed`, `Save_Canonical_Output_Preserve`, `Get_Dep_Output`, `Save_Success/Save_Canonical_Output` |
| Input record key format | `input#{pipeline}#{task}#{date}` | (backend `get_task_output` read only) | `Save_Input_Record` |
| Wrapper run-id env var | `POLYRIS_WRAPPER_RUN_ID` | `xcom.py` `ENV_RUN_ID` | `run_task/sfn.tpl.json` §1.5 env injection for service tasks |
| Task name env var | `POLYRIS_TASK_NAME` | `xcom.py` `ENV_TASK_NAME` | `run_task/sfn.tpl.json` §1.5 env injection for service tasks |

**When changing any coupled item:** update BOTH files in the **same commit**. Add contract test similar to `test_shared_constants_parity.py` — read constants from both sides, assert equal.

**New parity test to add** (§2.6 additions):
```python
# tests/sdk/test_xcom_sfn_contract.py — new file
def test_push_marker_field_matches_sfn_template():
    """xcom._PUSH_MARKER_FIELD must match Init_Output_Row's REMOVE clause and
    Check_Task_Pushed's projection field name in run_task/sfn.tpl.json."""
    from polyris.xcom import _PUSH_MARKER_FIELD
    template = _load_run_task_template()
    init = template["States"]["Init_Output_Row"]["Arguments"]
    assert _PUSH_MARKER_FIELD in init["ExpressionAttributeNames"].values()
    check = template["States"]["Check_Task_Pushed"]["Arguments"]
    assert _PUSH_MARKER_FIELD in check["ExpressionAttributeNames"].values()

def test_push_run_id_env_var_matches_sfn_template():
    """xcom.ENV_RUN_ID must match POLYRIS_WRAPPER_RUN_ID injected by wrapper."""
    from polyris.xcom import ENV_RUN_ID
    assert ENV_RUN_ID == "POLYRIS_WRAPPER_RUN_ID"
    template_raw = _load_run_task_template_raw_text()  # unparsed, before ${} substitution
    assert "POLYRIS_WRAPPER_RUN_ID" in template_raw
```

### 2.7. `polyris/__init__.py`

- **File:** `polyris/__init__.py` (extend existing exports around line 55)
- **Current:** `from .xcom import XComArg`
- **Change to:**
  ```python
  from .xcom import (
      XComArg,
      XComError, XComMissingError, XComUpstreamFailedError, XComTruncatedError,
      PullError,  # backward compat alias
  )
  from . import xcom  # allow `from polyris import xcom` idiom
  ```
- **Update `__all__`** (line 132+): add `'XComError', 'XComMissingError', 'XComUpstreamFailedError', 'XComTruncatedError', 'PullError', 'xcom'`

---

## 3. Backend — `sam/lambdas/console_api/`

### 3.1. `is_internal_record()` — filter new `input#` records (B3 BLOCKER)

- **File:** `sam/lambdas/console_api/utils.py:69-82`
- **Current:**
  ```python
  return execution_name.startswith('_') or execution_name.startswith('output#')
  ```
- **New:**
  ```python
  return (execution_name.startswith('_')
          or execution_name.startswith('output#')
          or execution_name.startswith('input#'))  # B3: task_input storage
  ```
- **Update docstring:** mention `input#pipeline#task#date` as another canonical-storage record type.
- **Test to add** (`test_utils.py`, line 621 area): `assert is_internal_record("input#sales#extract#2026-07-08") is True`

**Impact if we miss this:** `input#` records leak into all execution listings — All Tasks view, Runs view, pipeline detail. Users see garbage rows.

### 3.2. `get_task_output` — read from new record, fallback to legacy field

- **File:** `sam/lambdas/console_api/routes/tasks.py:380-428`
- **Current logic (lines 404-417):** reads `result` and `task_input` from single row `output#...`.
- **New logic:**
  ```python
  if pipeline_name:
      output_key = f"output#{pipeline_name}#{plain_task}#{run_date}"
      input_key = f"input#{pipeline_name}#{plain_task}#{run_date}"
      try:
          # Read output row (has result field)
          store_item = executions_repo.get(output_key) or {}
          raw = store_item.get('result')
          if raw:
              parsed = json.loads(raw)
              if isinstance(parsed, dict) and parsed.get('_truncated'):
                  truncated = True
              else:
                  output = retrieve_result(parsed)

          # Prefer new input record; fall back to legacy field for pre-migration pipelines
          input_item = executions_repo.get(input_key) or {}
          raw_input = input_item.get('task_input')
          if not raw_input:
              # Pipeline hasn't been re-run since PR shipped — task_input still in output# row
              raw_input = store_item.get('task_input')
          if raw_input:
              task_input = json.loads(raw_input)
      except (ClientError, BotoCoreError, ValueError) as e:
          log.error("get_task_output", "Error reading task input/output",
                    error=str(e), task_name=task_name)
  ```
- **Cost:** +1 DDB GetItem per task-output request. Console API is not hot path — acceptable.
- **Update docstring** (lines 380-388): remove "~25 KB" hardcode, mention separate input record.
- **Coupled with SFN template:** `input#{pipeline}#{task}#{date}` key format must match `Save_Input_Record` write key. See §2.8.

### 3.3. Tests for `get_task_output`

- **File:** `sam/lambdas/console_api/tests/routes/test_task_output.py` (extend existing 107 lines)

**New helper — multi-key table fixture (replaces `_Table`):**
```python
class _MultiKeyTable:
    """Fake table returning different items per execution_name key.

    Supports the 2-GetItem pattern: one for output# row, one for input# row.
    """
    def __init__(self, items_by_key=None):
        self._items = items_by_key or {}
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["Key"]["execution_name"]
        item = self._items.get(key)
        return {"Item": item} if item is not None else {}


def _patch_multi(mocker, *, output_store=None, input_store=None,
                 pipeline_name="sales", task_name="extract", date="2026-07-07"):
    """Patch `resolve_task_item` + repo `.table` with a multi-key table."""
    task_item = {"pipeline_name": pipeline_name, "task_name": task_name, "date": date}
    mocker.patch("routes.tasks.resolve_task_item",
                 return_value=(task_item, f"{task_name}-{date}-abc"))
    items = {}
    if output_store is not None:
        items[f"output#{pipeline_name}#{task_name}#{date}"] = output_store
    if input_store is not None:
        items[f"input#{pipeline_name}#{task_name}#{date}"] = input_store
    table = _MultiKeyTable(items)
    from dal.executions_repo import ExecutionsRepo
    mocker.patch.object(ExecutionsRepo, 'table', new_callable=mocker.PropertyMock,
                        return_value=table)
    return table
```

**Existing 11 tests — update to `_patch_multi(output_store=...)` instead of `_patch(store=...)`.** Non-breaking rewrite of the fixture.

**New tests to add:**
- `test_returns_input_from_new_record_when_present`:
  ```python
  def test_returns_input_from_new_record_when_present(mocker):
      new_input = {"upstream": {"a": {"output": {"n": 1}, "status": "success"}}, "variables": {}}
      _patch_multi(mocker,
                   output_store={"result": json.dumps({"rows": 5})},
                   input_store={"task_input": json.dumps(new_input)})
      body = _body(get_task_output("extract", _event()))
      assert body["input"] == new_input
  ```
- `test_falls_back_to_legacy_task_input_field` — new missing → old field used
- `test_prefers_new_record_over_legacy_when_both_present` — both → new wins
- `test_input_record_error_falls_back_gracefully` — DDB error on `input#` → try legacy
- `test_neither_record_returns_null_input` — both missing → input=None

### 3.4. Rollback safety test (G7)

- **File:** same test file — new tests
- `test_input_from_legacy_field_only` — simulates rollback: only legacy field populated, verify Console still shows it
- `test_stale_input_record_after_rollback` — stale `input#` row + fresh `task_input` field → prefer new (documented as accepted trade-off — TTL cleans up)

### 3.5. `pipelines_list.py` — check unaffected

- **File:** `sam/lambdas/console_api/routes/pipelines_list.py:318`
- **Verified:** uses `retrieve_result` on `result` field only. Unchanged.

---

## 4. UI — `ui/src/components/TaskDetailModal/TaskDetailModal.tsx`

### 4.1. Input rendering with marker interpretation

- **File:** `ui/src/components/TaskDetailModal/TaskDetailModal.tsx:646-690`
- **Current OutputTab** just does `JSON.stringify(input, null, 2)`.
- **Split into components:**

```tsx
// Add near top of file
function formatBytes(n: number): string {
    if (n < 1024) return `${n}B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
    return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

// Detect AWS service task metadata (Save_Success stored the AWS API response, not real user data)
function looksLikeAwsMetadata(output: unknown): boolean {
    if (!output || typeof output !== 'object') return false;
    const obj = output as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length > 4) return false; // real user data usually has more fields
    const metadataKeys = ['JobRunId', 'TaskArn', 'JobId', 'QueryExecutionId', 'StepId', 'ExecutionArn'];
    return keys.some(k => metadataKeys.includes(k));
}

function InputSection({ input }: { input: unknown }) {
    if (!input || typeof input !== 'object') {
        return (
            <div className="td-tab-empty td-tab-empty--inline">
                <Database size={14} /> No input recorded (upstream data + run variables).
            </div>
        );
    }

    const inp = input as Record<string, unknown>;

    // Whole-input omission marker (legacy pre-migration pipelines only)
    if (inp._upstream_omitted) {
        return (
            <div className="td-banner td-banner--warn">
                <AlertTriangle size={14} />
                <div>
                    Upstream data was <strong>{formatBytes(Number(inp._size))}</strong> — too large for legacy Console preview.
                    <br />
                    Task received the full data at runtime. Re-deploy this pipeline to enable the new preview (up to 380KB).
                </div>
            </div>
        );
    }

    const variables = (inp.variables as Record<string, unknown>) ?? {};
    const upstream = (inp.upstream as Record<string, unknown>) ?? {};

    return (
        <div className="td-input-section">
            {Object.keys(variables).length > 0 && (
                <div className="td-io-block">
                    <div className="td-io-sublabel">Variables</div>
                    <pre className="td-output-json">{JSON.stringify(variables, null, 2)}</pre>
                </div>
            )}
            {Object.keys(upstream).length > 0 && (
                <div className="td-io-block">
                    <div className="td-io-sublabel">Upstream ({Object.keys(upstream).length})</div>
                    {Object.entries(upstream).map(([dep, entry]) => (
                        <UpstreamDep key={dep} name={dep} entry={entry} />
                    ))}
                </div>
            )}
            {Object.keys(variables).length === 0 && Object.keys(upstream).length === 0 && (
                <div className="td-tab-empty td-tab-empty--inline">
                    <Database size={14} /> No upstream or variables recorded.
                </div>
            )}
        </div>
    );
}

function UpstreamDep({ name, entry }: { name: string; entry: unknown }) {
    if (!entry || typeof entry !== 'object') {
        return (
            <div className="td-banner td-banner--warn">
                <AlertTriangle size={14} />
                <strong>{name}</strong>: malformed upstream entry.
                <details><pre>{JSON.stringify(entry, null, 2)}</pre></details>
            </div>
        );
    }
    const e = entry as { status?: string; output?: unknown };
    const status = e.status ?? 'unknown';
    const output = e.output;

    // Missing dep — no output was recorded
    if (status === 'unknown') {
        return (
            <div className="td-upstream-dep td-banner td-banner--warn">
                <AlertTriangle size={14} />
                <div>
                    <strong>{name}</strong>
                    <div className="td-banner-detail">No output recorded (task may have been skipped, failed, or hasn't run for this date).</div>
                </div>
            </div>
        );
    }

    // Non-success status (skipped / failed / aborted)
    if (status !== 'success') {
        return (
            <div className="td-upstream-dep td-banner td-banner--error">
                <XCircle size={14} />
                <div>
                    <strong>{name}</strong> — status: <code>{status}</code>
                    {output !== undefined && output !== null && typeof output === 'object' && Object.keys(output as object).length > 0 && (
                        <details>
                            <summary>Output</summary>
                            <pre className="td-output-json">{JSON.stringify(output, null, 2)}</pre>
                        </details>
                    )}
                </div>
            </div>
        );
    }

    // Truncated marker (dep output was > 25KB inline cap)
    if (output && typeof output === 'object' && (output as Record<string, unknown>)._truncated) {
        const size = Number((output as Record<string, unknown>)._size);
        return (
            <div className="td-upstream-dep td-banner td-banner--warn">
                <AlertTriangle size={14} />
                <div>
                    <strong>{name}</strong>
                    <div className="td-banner-detail">
                        Output was <strong>{formatBytes(size)}</strong>, truncated for runtime injection (25KB cap).
                        Task can read full data via <code>xcom.get(event, "{name}")</code> which falls back to DDB.
                    </div>
                </div>
            </div>
        );
    }

    // Success — collapsible clean JSON
    return (
        <details className="td-upstream-dep td-upstream-dep--success">
            <summary>
                <CheckCircle2 size={14} /> <strong>{name}</strong>
                <span className="td-status-badge">success</span>
            </summary>
            <pre className="td-output-json">{JSON.stringify(output, null, 2)}</pre>
        </details>
    );
}

function OutputSection({ output, truncated }: { output: unknown; truncated: boolean }) {
    if (truncated) {
        return (
            <div className="td-banner td-banner--warn">
                <AlertTriangle size={14} />
                Output too large to store inline. If your task needs to return this much data,
                write to S3 and return <code>{'{"_s3_ref": "s3://..."}'}</code>.
            </div>
        );
    }
    if (output === null || output === undefined) {
        return (
            <div className="td-tab-empty td-tab-empty--inline">
                <Database size={14} /> This task stored no output.
            </div>
        );
    }
    return (
        <>
            {looksLikeAwsMetadata(output) && (
                <div className="td-banner td-banner--warn">
                    <AlertTriangle size={14} />
                    <div>
                        This output is an AWS API response, not application data. For Glue / ECS / Batch / EMR tasks,
                        call <code>xcom.push(value)</code> in your job code to store real output.
                        See <a href="/docs/features/DATA_PASSING.md#service-task-metadata-trap">docs</a>.
                    </div>
                </div>
            )}
            <pre className="td-output-json">{JSON.stringify(output, null, 2)}</pre>
        </>
    );
}

// New OutputTab uses the split components
function OutputTab({ input, output, truncated, loading, loaded }: OutputTabProps) {
    if (loading) return <div className="td-tab-empty"><Hourglass size={16} /> Loading…</div>;
    if (!loaded) return <div className="td-tab-empty"><Database size={16} /> Open to load input and output.</div>;
    return (
        <div className="td-output-tab">
            <div className="td-io-section">
                <div className="td-io-label">Input</div>
                <InputSection input={input} />
            </div>
            <div className="td-io-section">
                <div className="td-io-label">Output</div>
                <OutputSection output={output} truncated={truncated} />
            </div>
        </div>
    );
}
```

### 4.2. CSS additions

- **File:** `ui/src/styles/modules/_modals.css` (existing — where `.td-*` classes live, verified via grep)

- **Style pattern references** (existing classes to base on):
  - Container/layout: mimic `.td-io-section` (flex-column, gap 0.375rem) — already at line ~[grep for it]
  - Warn banner: mimic `.bd-status-hint--warning` — uses `background: rgba(245, 158, 11, 0.08)`, `border: 1px solid rgba(245, 158, 11, 0.35)`, `color: var(--text-primary)`, svg color `var(--warning)`
  - Error banner: mimic `.bd-status-hint--danger` — `background: rgba(239, 68, 68, 0.08)`, `border: 1px solid rgba(239, 68, 68, 0.35)`, svg color `var(--error)`
  - Muted variant: mimic `.bd-status-hint--muted` — `background: var(--bg-tertiary)`, `border: 1px solid var(--border)`
  - Status badge: mimic `.action-btn.success/warning/danger` pattern — colored `border-left: 3px solid var(--[color])`
  - Empty state: reuse `.td-tab-empty--inline` (already exists) — `padding: 0.625rem 0.75rem; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 6px`

- **New classes to add:**
  ```css
  .td-input-section {
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
  }

  .td-io-block {
      margin-bottom: 0.5rem;
  }

  .td-io-sublabel {
      font-size: 0.85rem;
      opacity: 0.7;
      margin-bottom: 0.25rem;
      font-weight: 500;
  }

  .td-upstream-dep {
      margin-bottom: 0.5rem;
      padding: 0.5rem 0.75rem;
      border-radius: 4px;
      border-left: 3px solid var(--border);
  }
  .td-upstream-dep--success { border-left-color: var(--success); }
  .td-upstream-dep summary {
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.9rem;
  }
  .td-upstream-dep summary strong { flex: 1; }

  .td-banner {
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
      padding: 0.625rem 0.75rem;
      border-radius: 4px;
      font-size: 0.85rem;
      margin-bottom: 0.375rem;
  }
  .td-banner svg { flex-shrink: 0; margin-top: 0.15rem; }
  .td-banner--warn {
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.35);
      color: var(--text-primary);
  }
  .td-banner--warn svg { color: var(--warning); }
  .td-banner--error {
      background: rgba(239, 68, 68, 0.08);
      border: 1px solid rgba(239, 68, 68, 0.35);
      color: var(--text-primary);
  }
  .td-banner--error svg { color: var(--error); }
  .td-banner-detail {
      margin-top: 0.25rem;
      font-size: 0.8rem;
      color: var(--text-secondary);
  }

  .td-status-badge {
      display: inline-block;
      padding: 0.1rem 0.4rem;
      border-radius: 8px;
      font-size: 0.7rem;
      background: var(--success-light, rgba(34, 197, 94, 0.15));
      color: var(--success);
      margin-left: 0.5rem;
  }

  .td-onboarding-banner {
      display: flex;
      align-items: flex-start;
      gap: 0.5rem;
      padding: 0.75rem 1rem;
      background: var(--bg-tertiary);
      border: 1px solid var(--border);
      border-radius: 6px;
      margin-bottom: 0.75rem;
      font-size: 0.85rem;
  }
  .td-onboarding-banner .td-banner-dismiss {
      margin-left: auto;
      background: none;
      border: 1px solid var(--border);
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      font-size: 0.75rem;
      cursor: pointer;
  }
  ```

- **Mobile:** existing `_mobile.css` responsive rules cover `.td-*` classes with `@media (max-width: 768px)` — new classes inherit automatically. No new mobile CSS needed.

### 4.3. Test coverage

- **File:** `ui/src/components/TaskDetailModal/TaskDetailModal.test.tsx` (extend existing)
- **Add:**
  - `renders_no_input_state_when_input_null` — null input → empty state
  - `renders_upstream_omitted_banner` — input._upstream_omitted → banner with size
  - `renders_variables_section` — variables shown
  - `renders_upstream_section_with_count`
  - `renders_missing_dep_banner` — status="unknown" → warn banner
  - `renders_failed_dep_banner` — status="failed" → error banner + details
  - `renders_truncated_dep_banner` — output._truncated → warn with pull() hint
  - `renders_success_dep_collapsed` — clean summary with badge
  - `renders_multiple_deps_correctly` — mix of statuses
  - `renders_aws_metadata_warning` — output looks like {JobRunId} → banner
  - `renders_truncated_output_hint` — output.truncated → S3 hint banner

### 4.4. `useTaskOutput` — unchanged

- **File:** `ui/src/hooks/useTaskOutput.ts`
- **No change** — hook shape (input, output, truncated) is stable. Payload may now be larger — hook fine as-is.

### 4.5. First-time UI change onboarding banner

**Problem (second-review Persona 4 feedback):** Task Detail modal Input/Output tab visibly changes. Users accustomed to raw JSON may be confused.

**Full implementation (add to `TaskDetailModal.tsx`):**

```tsx
// Add to imports at top of TaskDetailModal.tsx:
//   (react already imports useState/useMemo at line 1 — ADD useEffect, useCallback)
import { useState, useMemo, useEffect, useCallback } from 'react';
//   (icons already import Info at line 27 — VERIFY, add if missing)
import { Info, /* ...existing... */ } from '../../utils/icons';

// TODO(polyris-0.102.0): remove OnboardingBanner + localStorage key
// polyris.ui.taskDetailBannerDismissed_v100. See xcom-plan.md §4.5 cleanup section.
// Two releases after 0.100.0 introduces this — gives users time to see the banner once.

// Add near top of file, before TaskDetailModal component:
const ONBOARDING_BANNER_KEY = 'polyris.ui.taskDetailBannerDismissed_v100';

function useDismissibleBanner(key: string) {
    const [dismissed, setDismissed] = useState<boolean>(() => {
        if (typeof window === 'undefined') return true; // SSR-safe: hide during SSR
        try {
            return localStorage.getItem(key) === 'true';
        } catch {
            return false; // localStorage may be blocked (private mode) — show banner
        }
    });

    const dismiss = useCallback(() => {
        try {
            localStorage.setItem(key, 'true');
        } catch {
            /* ignore localStorage errors */
        }
        setDismissed(true);
    }, [key]);

    return { dismissed, dismiss };
}

function OnboardingBanner() {
    const { dismissed, dismiss } = useDismissibleBanner(ONBOARDING_BANNER_KEY);
    if (dismissed) return null;
    return (
        <div className="td-onboarding-banner" role="status">
            <Info size={14} />
            <div>
                <strong>Updated in 0.100.0:</strong> Upstream and output are now shown as
                colored cards with actionable messages. Click ▶ next to a dep to expand its raw data.
                <br />
                <a
                    href="/docs/features/DATA_PASSING.md"
                    target="_blank"
                    rel="noopener noreferrer"
                >
                    Learn more
                </a>
            </div>
            <button
                onClick={dismiss}
                className="td-banner-dismiss"
                aria-label="Dismiss onboarding banner"
            >
                Dismiss
            </button>
        </div>
    );
}

// In OutputTab component body (replaces old function):
function OutputTab({ input, output, truncated, loading, loaded }: OutputTabProps) {
    if (loading) return <div className="td-tab-empty"><Hourglass size={16} /> Loading…</div>;
    if (!loaded) return <div className="td-tab-empty"><Database size={16} /> Open to load input and output.</div>;
    return (
        <div className="td-output-tab">
            <OnboardingBanner />
            <div className="td-io-section">
                <div className="td-io-label">Input</div>
                <InputSection input={input} />
            </div>
            <div className="td-io-section">
                <div className="td-io-label">Output</div>
                <OutputSection output={output} truncated={truncated} />
            </div>
        </div>
    );
}
```

**Tests to add** in `TaskDetailModal.test.tsx`:
```tsx
// Add to test file imports (verify existing testing-library setup):
// import { render, screen } from '@testing-library/react';
// import userEvent from '@testing-library/user-event';
// import { describe, it, expect, beforeEach, vi } from 'vitest';

describe('OnboardingBanner', () => {
    beforeEach(() => { localStorage.clear(); });

    it('renders when not dismissed', () => {
        render(<OutputTab input={{}} output={null} truncated={false} loading={false} loaded={true} />);
        expect(screen.getByText(/Updated in 0.100.0/)).toBeInTheDocument();
    });

    it('hides after dismiss and persists to localStorage', async () => {
        const { rerender } = render(/*...*/);
        await userEvent.click(screen.getByRole('button', { name: /Dismiss/ }));
        expect(localStorage.getItem(ONBOARDING_BANNER_KEY)).toBe('true');
        rerender(/*...*/);
        expect(screen.queryByText(/Updated in 0.100.0/)).not.toBeInTheDocument();
    });

    it('hidden on subsequent renders if already dismissed', () => {
        localStorage.setItem(ONBOARDING_BANNER_KEY, 'true');
        render(/*...*/);
        expect(screen.queryByText(/Updated in 0.100.0/)).not.toBeInTheDocument();
    });

    it('shows when localStorage throws', () => {
        vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
        render(/*...*/);
        expect(screen.getByText(/Updated in 0.100.0/)).toBeInTheDocument();
    });
});
```

**Cleanup:** in polyris 0.102.0 (2 releases later), remove `OnboardingBanner` component + `_v100` localStorage key from user's browser (leave orphaned — cheap).

---

## 5. IAM — `sam/template.yaml`

### 5.1. Remove `PolyrisResultsBucketRead` (dead permission)

- **File:** `sam/template.yaml:2148-2152`
- **Delete** the entire `PolyrisResultsBucketRead` statement.
- **Update Description** (line 2137-2138):
  ```
  Read access to the Polyris output store. Attach to the execution role of any
  Lambda/ECS/Glue/Batch task that calls xcom.get() or xcom.pull().
  ```
- **Update comment** (line 2128-2131):
  ```
  # Managed policy that user task roles (Lambda/ECS/Glue/Batch) attach so their
  # code can call xcom.get() / xcom.pull() — least-privilege read of the output store.
  ```

**Breaking risk (from A1 audit):** if a user attached `PolyrisTaskReadPolicy` for reasons OTHER than xcom (e.g., their code reads ResultsBucket directly), removal breaks them. **Mitigation:** ResultsBucket exists but is used only for CFN template artifacts (per `polyris/deploy.py:755`). No documented user pattern that needs S3 read of ResultsBucket. **Low risk, document in CHANGELOG.**

### 5.2. Add `PolyrisTaskWritePolicy`

- **File:** `sam/template.yaml` — insert after `PolyrisTaskReadPolicy` (line 2153)
- **New resource:**
  ```yaml
  # Managed policy for tasks that call xcom.push() — required only for Glue/ECS/
  # Batch/EMR jobs whose code stores real output. Lambda return values do NOT
  # need this policy (captured by the wrapper).
  PolyrisTaskWritePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      ManagedPolicyName: !Sub "${Namespace}-${Stage}-polyris-task-write"
      Description: >-
        Write access to Polyris output store for xcom.push(). Attach to task roles
        of Glue/ECS/Batch/EMR jobs that call xcom.push().
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: PolyrisOutputStoreWrite
            Effect: Allow
            Action:
              - dynamodb:UpdateItem
            Resource:
              - !GetAtt PipelineTokensTable.Arn
            Condition:
              "ForAllValues:StringLike":
                "dynamodb:LeadingKeys":
                  - "output#*"
  ```
- **New Output:**
  ```yaml
  PolyrisTaskWritePolicyArn:
    Description: Attach to task roles that call xcom.push() (Glue/ECS/Batch/EMR)
    Value: !Ref PolyrisTaskWritePolicy
    Export:
      Name: !Sub "${Namespace}-${Stage}-polyris-task-write-policy"
  ```

### 5.3. Add clarifying comment to `ResultsBucket`

- **File:** `sam/template.yaml:520-543`
- **Update comment** (line 520-522):
  ```yaml
  # S3 Bucket — polyris-deploy CloudFormation artifact storage (see polyris/deploy.py).
  # NOT used for XCom data. XCom uses DynamoDB; large payloads use the manual
  # Claim Check pattern with user's own bucket. See docs/features/DATA_PASSING.md.
  ```

---

## 6. Documentation

### 6.1. `docs/features/DATA_PASSING.md` — rewrite

- **Full rewrite.** Sections:
  1. Overview — one `xcom.get()` / `xcom.push()` mental model
  2. **Version requirements** — new: "requires polyris 0.100.0+ in your task deployment bundles (Lambda zip, Glue --additional-python-modules, etc.). Old SDK continues to work with `event.upstream` / `pull()`, but doesn't get new features."
  3. Cheat sheet — universal `xcom.get(event, "extract")` example per task type
  4. Reading upstream — `xcom.get()` full API, kwargs, optional patterns
  5. **Migrating from `event["upstream"][X]["output"]` to `xcom.get()`** — new subsection
     - Direct replacement (happy path unchanged)
     - **⚠️ Breaking behavior change for `all_done` trigger users:**
       ```python
       # Before (silent — bug source):
       data = event["upstream"]["maybe_failed_task"]["output"]  # returns {} if failed

       # After (loud by default — will crash if you ran with all_done trigger):
       data = xcom.get(event, "maybe_failed_task")  # raises XComUpstreamFailedError

       # After (explicit tolerance — safe migration for all_done users):
       data = xcom.get(event, "maybe_failed_task", raise_on_failure=False) or {}
       ```
     - Migration recipe: sweep code for `event["upstream"]` reads → wrap upstream deps that use `trigger_rule="all_done"` or `"one_success"` with `raise_on_failure=False`
  6. Writing output
     - Lambda: `return value` (natural)
     - Service tasks: `xcom.push(value)` (**mandatory example for Glue**)
     - Athena: SQL `variables=` templating + Lambda-front pattern
  7. Failure handling — status field, `raise_on_*`, `all_done` trigger example
  8. **The service task metadata trap** — new section, why Glue/ECS/Batch need push
  9. Size limits — verified table
  10. **Manual Claim Check Pattern (BYO S3 bucket)** — no more "auto S3" lie
  11. **Installing polyris SDK in Glue/ECS/Batch/EMR** — G1 fix, new section
  12. **Cross-account limitations** — H3 documented
  13. **Lambda write pattern** — B2 warning explained
      - **Also document: push-wins-over-return semantics (I8):**
        ```
        If a Lambda calls xcom.push() AND returns a value, the pushed value wins.
        The wrapper's Check_Task_Pushed detects the marker and Save_Success_Preserve
        skips overwriting result with the Lambda's return value.

        Example (surprising behavior):
            def handler(event):
                xcom.push({"path": "s3://..."})   # ← wins
                return {"other": "data"}          # ← IGNORED downstream

        Best practice for Lambda: pick ONE — return value OR xcom.push(), not both.
        (For Glue/ECS/Batch, xcom.push() is the only real option.)
        ```
  14. **IAM policy attachment matrix** — new subsection (Ops persona feedback):

      | Task type + usage | `PolyrisTaskReadPolicy` | `PolyrisTaskWritePolicy` |
      |-------------------|:-----------------------:|:------------------------:|
      | Lambda — `return value` only | not needed | not needed |
      | Lambda — calls `xcom.get()` / `xcom.pull()` | **required** | not needed |
      | SFN — child SFN Output (no SDK) | not needed | not needed |
      | Glue / ECS / Batch / EMR — reads only via `xcom.get()` / `xcom.pull()` | **required** | not needed |
      | Glue / ECS / Batch / EMR — calls `xcom.push()` | **required** | **required** |
      | Athena — SQL only (no SDK) | not needed | not needed |

      **CFN attach example** (user's task role):
      ```yaml
      MyGlueTaskRole:
        Type: AWS::IAM::Role
        Properties:
          ManagedPolicyArns:
            - !ImportValue myorg-dev-polyris-task-read-policy
            - !ImportValue myorg-dev-polyris-task-write-policy  # only if using xcom.push()
      ```
  15. Anti-patterns table
  16. Where data lives — DDB tables, keys, TTL, **new `input#...` prefix documented**
      - **DDB schema for `output#{pipeline}#{task}#{date}` rows (extended fields, G3):**

        | Field | Type | When set | Purpose |
        |-------|------|----------|---------|
        | `execution_name` | S (PK) | always | `output#{pipeline}#{task}#{date}` |
        | `task_name` | S | Init_Output_Row | task_id |
        | `pipeline_name` | S | Save_Canonical_Output | pipeline identifier |
        | `status` | S | Save_Success* | `success` / `failed` / etc. |
        | `result` | S (JSON) | Save_Success or xcom.push() | task output |
        | `updated_at` | S (ISO) | every state that writes | last update |
        | `finished_at` | S (ISO) | Save_Success* | completion timestamp |
        | `ttl` | N | Init_Output_Row (if_not_exists) | expiry (120 days) |
        | `run_id` | S | Init_Output_Row | wrapper ARN of most recent init |
        | `attempt` | N | Update_Status_Running | current retry attempt |
        | `task_execution_arn` | S | Save_Success* | AWS resource ARN |
        | `_pushed_by_task` | BOOL | xcom.push() | marker for Check_Task_Pushed |
        | `pushed_at` | S (ISO) | xcom.push() | when push() was called |
        | `pushed_run_id` | S | xcom.push() | wrapper ARN that owned the push |
        | `push_count` | N | xcom.push() (ADD) | debug: how many push() calls this run |

        Init_Output_Row REMOVEs `_pushed_by_task`, `pushed_at`, `pushed_run_id` at
        start of every run — stale markers from prior same-date runs never leak
        into Check_Task_Pushed decisions.

      - **DDB schema for `input#{pipeline}#{task}#{date}` rows (new in 0.100.0):**

        | Field | Type | Purpose |
        |-------|------|---------|
        | `execution_name` | S (PK) | `input#{pipeline}#{task}#{date}` |
        | `task_name` | S | task_id |
        | `pipeline_name` | S | pipeline identifier |
        | `task_date` | S | task run date **(not `date`)** — see GSI note below |
        | `task_input` | S (JSON) | full `{upstream, variables}` snapshot up to ~380KB |
        | `updated_at` | S (ISO) | last update |
        | `ttl` | N | expiry (120 days) |

        **Note on `task_date` field naming:** deliberately mismatched from
        `date-pipeline-index` GSI's key attribute (`date`) to avoid populating
        that GSI with internal `input#` records.

### 6.2. `docs/reference/DESIGN_DECISIONS.md`

- Update ADR #15 (line 298): note that per-dep 25KB runtime cap unchanged (AWS constraint), but task_input storage now in separate record.
- Add new ADR — "XCom: separate input record + xcom.push() for service tasks":
  - Rationale for splitting records
  - Rationale for `_pushed_by_task` marker with run versioning (B1)
  - Rationale for keeping `_s3_ref` as manual-only

### 6.3. `docs/work/reliable-data-passing/DECISIONS.md`

- Update: add all 8 problems (from table above)
- New S9 "input record separation" — B3 impact on is_internal_record
- New S10 "xcom.get / xcom.push uniform API" — H2, M5 decisions
- New S11 "S3 escape hatch — manual only"
- New S12 "run-versioned push marker" — B1 fix

### 6.4. `CHANGELOG.md`

- Version 0.99.0 → 0.100.0
- Entry:
  ```
  ## 0.100.0 — Reliable data passing

  ### SDK version requirement
  New xcom.get() and xcom.push() APIs live in polyris 0.100.0 — update your task
  deployment bundles (Lambda zip, Glue --additional-python-modules, ECS container
  polyris install, etc.) to use them. Old SDK versions continue to work with
  existing event["upstream"] and pull() patterns, but don't get the new features.

  ### Added
  - xcom.get(event, task): uniform reader (Lambda/Glue/ECS/Batch/EMR), auto S3 resolve
  - xcom.push(value): writer for Glue/ECS/Batch/EMR (fixes AWS metadata leak)
  - XComError, XComMissingError, XComUpstreamFailedError, XComTruncatedError
  - PolyrisTaskWritePolicy managed policy (attach if task calls xcom.push())
  - POLYRIS_TASK_NAME and POLYRIS_WRAPPER_RUN_ID env vars for service tasks
  - Separate DDB record `input#{pipeline}#{task}#{date}` for task_input (up to 380KB)
  - Console UI: color-coded upstream markers with actionable hints
  - Console UI: AWS-metadata warning banner on service task outputs
  - Console UI: one-time onboarding banner explaining the new Task Detail layout

  ### Fixed
  - $isJson heuristic no longer wraps primitives in {_raw: ...}
  - Docs no longer claim auto-S3-offload (manual claim check documented)
  - Stale push marker corruption between backfill runs (run_id versioning)

  ### Deprecated
  - PullError → alias for XComMissingError (backward compat)

  ### Removed
  - PolyrisResultsBucketRead statement (dead permission for unused bucket read)

  ### Breaking behavior for opt-in migration
  - Migrating from `event["upstream"][X]["output"]` to `xcom.get(event, X)`:
    if X uses trigger_rule="all_done" or "one_success", pass raise_on_failure=False.
    Old code silently returned {} for failed upstreams; xcom.get() raises by default.
    See docs/features/DATA_PASSING.md#migrating.

  ### Breaking risk (low probability, documented)
  - Users who attached PolyrisTaskReadPolicy for ResultsBucket S3 read (undocumented
    pattern — bucket is used by polyris-deploy for CloudFormation artifacts) lose
    that access. Attach a direct s3:GetObject on the bucket if you actually needed it.

  ### AWS cost impact
  - +1 DDB GetItem + 1 UpdateItem per task success (Check_Task_Pushed + Init_Output_Row)
  - Approximate impact at 100k tasks/day: ~$0.30/day, ~$9/month
  - No impact for pipelines that don't run any task
  ```

### 6.5. Documentation sweep (L3)

Grep + update these files if they mention XCom:
- `README.md` — root
- `CLAUDE.md` — root (XCom section reference)
- `docs/features/DSL.md` — check for XCom mentions
- `docs/getting-started/QUICKSTART.md` — check first-XCom examples
- `docs/getting-started/TUTORIAL.md` — same
- All docstrings in `polyris/task.py` — service task decorators mention `xcom.push()`
- `polyris/xcom.py` module docstring — full rewrite

### 6.6. `polyris/__init__.py`

Already covered in §2.7.

---

## 7. Test summary

### Python

| File | Add tests |
|------|-----------|
| `tests/sdk/test_xcom_pull.py` → `test_xcom.py` | 14 for get() + 10 for push() + 2 for aliases + 2 for race |
| `tests/sdk/test_run_task_template.py` | 13 tests (SFN state changes + POLYRIS_TASK_NAME env) |
| `sam/lambdas/console_api/tests/routes/test_task_output.py` | 5 tests (new record + fallback + rollback) |
| `sam/lambdas/console_api/tests/test_utils.py` | 1 test — `is_internal_record` filters input# (B3) |

### UI

| File | Add tests |
|------|-----------|
| `ui/src/components/TaskDetailModal/TaskDetailModal.test.tsx` | 11 tests for rendering variants |

### E2E (manual, dev account)

- Deploy scenario-4 with dict/list/primitive/None returns
- Deploy Glue → Lambda with `xcom.push()` — verify Lambda receives pushed value, not JobRunId
- Deploy Glue → Lambda WITHOUT push — verify Console shows AWS metadata warning banner
- Deploy 3-upstream fan-in totaling > 25KB — verify Console shows all (200KB task_input)
- Deploy same-date backfill after normal run — verify no stale push marker corruption
- Simulate rollback: keep old wrapper live, verify Console fallback path

---

## 8. Rollout / Migration

### Non-breaking guarantees (verified)

- Old `event["upstream"][X]["output"]` — works as before (never changed)
- Old `pull()` — works as before (docstring updated, code unchanged)
- `PullError` — still exists as `XComMissingError` alias
- `PolyrisTaskReadPolicy` — still exists, just no more S3 statement (breaking risk noted §5.1)
- Old wrapper's `task_input` field in `output#` row — read by Console API fallback (§3.2)
- Legacy pipelines mid-migration — Console API reads both new and legacy locations

### Deploy sequence (corrected per M4)

1. **`cd sam && sam build && sam deploy`** — updates helper SFN template + IAM.
   `run_task` is a shared helper; updated template takes effect immediately for **all** pipelines on their next execution. **No per-pipeline `polyris-deploy` needed.**
2. **`cd ui && npm ci && npm run build && ./deploy.sh`** — deploys new Console UI.
3. Users who want `xcom.push()`: attach `PolyrisTaskWritePolicy` to their Glue/ECS/Batch/EMR task roles + update SDK version in job requirements.

### Rollback

- Single PR revert — everything reverts.
- Console API fallback handles half-migrated data.
- `input#` records left over from new wrapper — TTL cleans up in 120 days.
- No manual data migration needed either way.

---

## 9. Breaking-risk audit (A1)

| Change | Risk | Mitigation |
|--------|------|-----------|
| Remove `PolyrisResultsBucketRead` | User attached policy for undocumented S3 read | Document in CHANGELOG; direct S3 read patterns are user's own bucket anyway |
| `PullError` becomes alias | User did `class MyError(PullError)` | Still works — `PullError` is still a class, just now aliased |
| New SFN state names (Check_Task_Pushed etc.) | Restart flow references state names | Verified restart_task.tpl.json and restart_wrapper.tpl.json — no state name deps |
| New `input#` DDB key prefix | Code filtering by prefix leaks these rows | §3.1 updates `is_internal_record()` — B3 blocker fix |
| New env vars POLYRIS_TASK_NAME, POLYRIS_WRAPPER_RUN_ID | Existing task code reads env var by same name | Namespaced with POLYRIS_ prefix — collision unlikely |
| xcom.push() adds DDB UpdateItem calls | Cost impact | ~1 extra DDB op per task success — trivial |
| Check_Task_Pushed adds GetItem per task | Cost + latency | +1 GetItem per success, ~50ms latency, ~$0.0000005 per call — trivial |
| task_input now up to 380KB | Console fetches larger payloads | Console API not on hot path; API Gateway 6MB limit unaffected |
| Backfill race: two runs writing to same output# | Stale marker corruption | B1 fix: Init_Output_Row clears marker; Check_Task_Pushed verifies run_id |
| Async push in Lambda | Race with Save_Success | B2 fix: UserWarning; documented in Lambda write pattern section |
| AWS cost | +2 DDB ops per task (~$0.30/day @ 100k tasks) | Documented in CHANGELOG; trivial for typical scale |
| Migrating `event["upstream"]` → `xcom.get()` with `all_done` trigger | Loud errors where old code silently got `{}` | Migration recipe in DATA_PASSING.md; `raise_on_failure=False` opt-out |
| `POLYRIS_TASK_NAME` env missing after SDK update but before wrapper deploy | `xcom.push()` fails at runtime | §2.3 actionable error message: "run `sam deploy` on polyris SAM template first" |
| Ops attaches wrong policy combination | `xcom.push()` fails AccessDenied, or `xcom.get()` fails on read | IAM policy attachment matrix in DATA_PASSING.md §14 |
| User doesn't update SDK version | New features silently unavailable | CHANGELOG SDK version requirement statement + DATA_PASSING.md §2 |
| Users confused by new Console UI | Support tickets "where did raw JSON go?" | §4.5 one-time onboarding banner dismissible |

---

## 10. Estimates (revised realistic)

| Component | LOC (add/edit) |
|-----------|----------------|
| SFN template (Init_Output_Row + Save_Input_Record + Check_Task_Pushed + Route_Save_Success + Save_Success_Preserve + Save_Canonical_Output_Preserve + POLYRIS_TASK_NAME + POLYRIS_WRAPPER_RUN_ID env vars for 5 task types) | ~180 |
| SDK `xcom.py` (get + push + 4 error classes + Lambda warning + json.dumps wrap + all validation) | ~220 |
| Backend routes tasks.py (fallback logic) | ~15 |
| Backend utils.py (is_internal_record) | ~5 |
| UI TaskDetailModal (InputSection + UpstreamDep + OutputSection + AWS-metadata banner + formatBytes) | ~200 |
| UI CSS (banners + upstream deps + status badges + onboarding banner) | ~80 |
| UI onboarding banner + localStorage dismiss logic + tests | ~40 |
| IAM template (delete dead, add write, edit comments) | ~30 |
| Python tests (28 xcom + 13 SFN + 5 backend + 1 utils + race tests) | ~500 |
| UI tests (11 rendering variants) | ~180 |
| Docs (DATA_PASSING full rewrite + ADR + others) | ~500 |
| **Total** | **~1950 LOC** |

**Time:** 4-5 focused days.

---

## 11. Commit-by-commit sequence within the PR (A3)

Structured for reviewer sanity (~10 commits):

1. **`fix: $isJson heuristic in Get_Dep_Output`** (already done)
2. **`refactor: split PullError → XComError hierarchy`** — new error classes, alias
3. **`feat(sdk): xcom.get() uniform reader`** — new function + tests
4. **`feat(sdk): xcom.push() for service tasks`** — new function + tests + json error handling
5. **`fix(sfn): split Save_Task_Input, clear stale push marker`** — Init_Output_Row + Save_Input_Record
6. **`feat(sfn): Check_Task_Pushed + Save_Success_Preserve for xcom.push`** — push condition states
7. **`feat(sfn): POLYRIS_TASK_NAME + POLYRIS_WRAPPER_RUN_ID env vars`** — service task env injection
8. **`fix(backend): input# record with legacy fallback in get_task_output`** — Console API
9. **`fix(backend): is_internal_record filters input# prefix`** — B3 blocker fix
10. **`feat(ui): render upstream markers with actionable hints`** — TaskDetailModal split
11. **`refactor(iam): PolyrisTaskWritePolicy + remove dead ResultsBucket read`**
12. **`docs: rewrite DATA_PASSING.md around xcom.get/push`** — plus ADR + CHANGELOG

Each commit tested independently. Reviewer follows narrative.

---

## 10a. ADR draft — XCom reliable data passing

**Draft for `docs/reference/adr-XXX-xcom-reliable-data-passing.md`** (replace XXX with next ADR number at commit time):

```markdown
# ADR-XXX: XCom reliable data passing — separate input record, xcom.push() for service tasks

Date: 2026-09-11
Status: Accepted

## Context

Polyris has two XCom paths — event.upstream (Lambda/SFN) and xcom.pull() (Glue/ECS/Batch/EMR).
UAT reported "Console shows N upstreams, Lambda got fewer." Investigation revealed multiple
independent issues:

1. `$isJson` heuristic wrapped primitives (`42`, `null`, `[1,2,3]`) in `{_raw: ...}` markers
2. Console `task_input` field truncated to 25KB → wiped all upstream data to `_upstream_omitted`
   marker when combined with variables exceeded the arbitrary cap
3. Service tasks (Glue/ECS/Batch) stored AWS API responses (`{JobRunId}`, `{TaskArn}`) instead
   of user data — silent metadata leak
4. UI rendered raw JSON of these markers with no interpretation, users saw cryptic output
5. Two reader shapes: `event.upstream[X].output` (wrapped) vs `xcom.pull(X)` (raw) forced
   users to maintain two mental models
6. Missing/failed deps returned silent `{}` — bugs hidden until downstream failure

## Decision

**Design changes across SFN template, SDK, backend, UI, IAM, and docs:**

1. **Separate DDB records** for canonical output (`output#{p}#{t}#{d}`) and Console
   snapshot (`input#{p}#{t}#{d}`) — each with its own 400KB item budget. Console
   task_input no longer forced to 25KB.

2. **`xcom.push(value)` API** for service tasks (Glue/ECS/Batch/EMR) — writes real
   output to DDB with `_pushed_by_task` marker. Wrapper detects marker and skips
   overwriting with AWS response.

3. **`_pushed_by_task` marker versioned by wrapper run_id** — Init_Output_Row clears
   stale markers at start of each run, Check_Task_Pushed accepts only markers with
   matching current-run ID. Prevents backfill re-runs seeing corrupt pushes from
   prior runs of the same date.

4. **`xcom.get(event, task)` uniform reader** — one API for all task types.
   Auto-resolves `_s3_ref` claim-check pointers. Falls back to DDB when event
   inject is truncated. Loud errors by default (`XComMissingError`,
   `XComUpstreamFailedError`, `XComTruncatedError`).

5. **`$isJson` heuristic replaced** by `$exists($parse($safe))` — primitives now
   parse correctly.

6. **Console UI renders markers explicitly** — color-coded per-dep banners
   (warn/error/success), AWS-metadata detection with actionable hint about
   xcom.push(), one-time onboarding banner for the new layout.

7. **S3 offload stays as manual claim-check** — no auto-offload. Documented as
   Bring-Your-Own-Bucket pattern with example. Misleading `PolyrisResultsBucketRead`
   IAM statement removed.

8. **New `PolyrisTaskWritePolicy`** — DDB UpdateItem on `output#*` for tasks that
   call xcom.push(). Attach alongside existing `PolyrisTaskReadPolicy`.

## Consequences

**Positive:**
- Console shows meaningful debug info instead of cryptic markers (UAT bug fixed)
- Glue/ECS/Batch producers can pass real data via xcom.push()
- Uniform reader API — one mental model
- Loud errors surface bugs earlier
- Backfill re-runs no longer risk stale-marker data corruption
- 380KB task_input capacity vs 25KB — 99% of "omitted" cases go away

**Neutral:**
- +1 DDB GetItem per task success (Check_Task_Pushed) — ~$0.30/day per 100k tasks
- +1 DDB PutItem per task start (Save_Input_Record separate row) — same cost order
- SFN state graph grows by 6 new states — minor Console visual impact only

**Negative:**
- Migration required for users adopting xcom.get() with `all_done` trigger:
  raise_on_failure=False needed for optional upstreams
- Users with Glue tasks must remember xcom.push() to store real data —
  otherwise still get AWS metadata (Console banner warns, docs prominent)
- xcom.push() from Lambda races with wrapper Save_Success — UserWarning emitted,
  documented that push wins over return value
- EMR xcom.push() deferred — no Environment field in addStep.sync;
  documented as future work
- Cross-account xcom.push() unsupported — documented limitation

**Coupled constants** between SDK (`polyris/xcom.py`) and SFN template
(`run_task/sfn.tpl.json`): `_pushed_by_task`, `pushed_run_id`, `pushed_at`,
key formats `output#*` / `input#*`, env vars `POLYRIS_TASK_NAME` /
`POLYRIS_WRAPPER_RUN_ID`. See xcom-plan.md §2.8 for full list. Parity test
`test_xcom_sfn_contract.py` enforces synchronization.

## Alternatives considered

- **Auto S3 offload:** rejected as over-engineering. Data-pipeline convention is
  "big data in S3 as parquet, XCom carries path" — auto-magic hides where data
  lives. Manual claim-check (`_s3_ref`) kept as escape hatch.
- **Type-schema XCom (Dagster IO Managers):** rejected as architecturally
  incompatible — polyris references external AWS compute, doesn't own it.
- **Multi-key XCom (`xcom.push("secondary", value)`):** deferred (YAGNI). No
  concrete demand today. Can add later without breaking API.
- **DSL-time validation of Glue→Lambda metadata leak:** deferred. Requires static
  code analysis of Lambda handler code, out of scope.
- **`ConditionExpression` on Save_Success (attribute_not_exists push marker)
  instead of pre-read Check_Task_Pushed state:** rejected because catch semantics
  overload with existing stale-attempt-guard catch — hard to route correctly.

## References

- xcom-plan.md — full implementation plan
- ADR-15 — original 25KB per-dep runtime truncation (unchanged, real SFN constraint)
- UAT feedback: "Console shows 2 upstream, Lambda got 1" (root cause: silent `{}`
  and `_upstream_omitted` markers rendered as cryptic JSON)
```

Coder should save this as `docs/reference/adr-<next-number>-xcom-reliable-data-passing.md`
in commit 12 (docs), update `docs/reference/adr-index.md` accordingly.

---

## 11a. Pre-implementation verification (results)

Ran audits before finalizing plan. Findings:

| Check | Result |
|-------|--------|
| `Emit_Task_Finished_Success` exists + takes `$states.input` | ✅ verified at line 826, all required fields (`task_run_id`, `execution_name`, `pipeline_execution`, `task_name`, `ttl`, `parent_execution_id`, `backfill_id`, `partition_key`, `current_date`) flow through from `$states.input ~> |$| {'final_status':'success'} |` output of Save_Success_Preserve |
| `Stale_Attempt_Superseded` accepts arbitrary input | ✅ verified at line 695 — `Type: Succeed`, no shape requirements |
| `Run_Task_EMR` env mechanism | ⚠️ EMR (line 545) uses `addStep.sync`, **no Environment field**. Only `HadoopJarStep.Args`. **Decided: defer EMR `xcom.push()` support** — documented as limitation (§1.5) |
| `$states.context.Execution.Id` available in child states | ✅ used throughout template (lines 135, 263) — safe |
| `$states.input.ttl` populated at `Init_Output_Row` time | ✅ set by dependency_wrapper first state (dependency_wrapper/sfn.tpl.json:9) and propagated via `~> |$| {...} |` merge in every subsequent state |
| `ExecutionsRepo.get(input_key)` accepts `input#*` prefix | ✅ verified — takes arbitrary string, no prefix validation (`sam/lambdas/console_api/dal/executions_repo.py:get()`) |
| `test_xcom_pull.py` rename impact | ✅ safe — only self-references; other tests import from `polyris.xcom` module (not from this test file) |
| `dynamodb:LeadingKeys` with `StringLike` + wildcard | ⚠️ **not verified via test-deploy** — no existing usage in template.yaml. **AWS docs confirm support**: `dynamodb:LeadingKeys` is a documented condition key, `ForAllValues:StringLike` operator supports wildcards ([AWS DynamoDB IAM docs](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html#FGAC_DDB.ConditionKeys)). **Decision: use as designed** in §5.2 PolyrisTaskWritePolicy. **Rollback if broken**: if first `sam deploy` fails or task push AccessDenied traces to Condition mismatch → fallback to unrestricted `Resource` (trust boundary is task role membership within same AWS account — user's Glue task ARN itself is the authorization boundary). Documented in §9 breaking-risk audit. |
| existing `Save_Task_Input` refs in tests | ⚠️ **15+ references in `tests/backend/test_alerting.py`** — verified via grep. Documented in §1.6 with per-line update instructions. |
| CSS pattern references | ✅ verified — `.bd-status-hint--warning/danger` in `_modals.css` are the ideal reference pattern, use same rgba colors (`rgba(245, 158, 11, 0.08)` warn, `rgba(239, 68, 68, 0.08)` error) |

**No remaining pre-implementation blockers.** All verifications complete or documented as design decisions with fallback strategies.

## 12. Verification checklist (A4)

### Before merge

- [ ] `python -m pytest tests/sdk tests/backend -q` — all green
- [ ] `cd sam/lambdas/console_api && python -m pytest tests/ -q` — all green
- [ ] `cfn-lint sam/template.yaml` — 0 errors
- [ ] `cd ui && npx vitest run` — all green
- [ ] `cd ui && npm run typecheck` — no errors
- [ ] `cd ui && npm run build` — succeeds
- [ ] `cd tests/sfn_jsonata && npm test` — all JSONata expressions compile
- [ ] `make test-cov` — coverage floor still met

### After deploy (dev account)

- [ ] `sam deploy` succeeds
- [ ] Scenario-4 run: Console shows both upstream entries as colored cards
- [ ] Scenario-4 with 200KB payload: Console shows full data (no `_upstream_omitted`)
- [ ] Glue → Lambda with `xcom.push()`: Lambda receives pushed value, not JobRunId
- [ ] Glue → Lambda without push: Console shows AWS-metadata warning banner
- [ ] Missing dep scenario: `xcom.get()` raises `XComMissingError` with actionable message
- [ ] Backfill same-date run: no stale marker corruption (verify via DDB inspection — see detailed steps below)

**Backfill stale-marker verification (G6, step-by-step):**
1. Deploy a Glue task with `xcom.push({"v": 1})` in its code
2. Trigger day-1 pipeline run → wait for completion
3. Query DDB `pipeline-tokens`:
   ```bash
   aws dynamodb get-item --table-name <namespace>-<stage>-polyris-pipeline-tokens \
     --key '{"execution_name": {"S": "output#<pipeline>#<glue-task>#<date>"}}'
   ```
   - Verify row has: `_pushed_by_task: {"BOOL": true}`, `result: {"S": "{\"v\": 1}"}`, `pushed_run_id: {"S": "<wrapper-arn-1>"}`
4. Edit Glue code — comment out the `xcom.push()` call. Redeploy Glue job.
5. Trigger day-1 backfill re-run
6. **During the run** (before Save_Success), query DDB again:
   - `_pushed_by_task` field should be **ABSENT** (Init_Output_Row REMOVEd it)
   - `pushed_run_id` field should be **ABSENT**
7. After the run completes, query DDB one more time:
   - `_pushed_by_task` should still be **ABSENT** (task didn't push this run)
   - `result` should now be `{"JobRunId": "..."}` (AWS response, since no push)
   - `run_id` should be `<wrapper-arn-2>` (updated by Init_Output_Row)
8. From downstream, `xcom.get(event, "<glue-task>")` should return `{"JobRunId": ...}`, NOT stale `{"v": 1}`

**If step 6 shows stale `_pushed_by_task: true` → BUG in Init_Output_Row REMOVE clause. Rollback.**
- [ ] `xcom.push()` from Glue task without POLYRIS_TASK_NAME env: error message mentions `sam deploy` (post-second-review)
- [ ] Task Detail modal on first open: onboarding banner visible (post-second-review)
- [ ] Task Detail modal on second open (after dismiss): no banner (localStorage persists)
- [ ] All Tasks / Runs list: no `input#*` rows leak through (B3 verification)
- [ ] IAM matrix from DATA_PASSING.md: Glue task without WritePolicy attempting push → AccessDenied with actionable error
- [ ] All tasks/day metric before + after: cost delta within expected ~$0.30/day per 100k range

### Rollback criteria

Roll back if:
- Console API `/api/task-output` returns 500 on any task
- `xcom.pull()` raises unexpected errors (regression)
- Existing pipelines fail after `sam deploy` (SFN state graph broken)
- `_upstream_omitted` still shown for < 200KB inputs (fix incomplete)
- All Tasks / Runs listing shows `input#*` rows (B3 regression)

Rollback command: single PR revert (all changes reversible).

---

## 13. Explicitly out of scope

- Auto S3 offload for outputs
- New XCom-specific S3 bucket
- Removing `_s3_ref` reader (keeping as escape hatch)
- Bumping runtime 25KB per-dep cap (real AWS SFN constraint)
- DSL-time validation of Glue → Lambda metadata leak (needs static analysis, separate project)
- Type-hint schemas (Dagster-style)
- TaskFlow-style value chaining (architecturally incompatible)
- Multi-key XCom (`xcom.push("secondary", value)`) — deferred until concrete demand
- CLI "test XCom" tool — dev tool, separate
- Cross-account xcom.push() — documented as limitation, IAM extension left to user
- Athena xcom.push() equivalent — SQL cannot call SDK
- polyris.local xcom.push() support — deferred, requires local runner refactor

---

## 14. Final DX/UX verdict

| Scenario | Grade | Notes |
|----------|-------|-------|
| Simple Lambda → Lambda | **A+** | `xcom.get()` just works, primitives fixed |
| Optional upstream (all_done) | A | one kwarg, discoverable via docstring |
| Glue → Lambda with push | **A-** | uniform get, must remember push, Console + docs help |
| > 350KB payload | B | manual Claim Check pattern documented |
| Debug in Console | **A** | color-coded, actionable messages, S3 hints |
| Backfill correctness | A | run-versioned marker prevents corruption |
| Failure loud vs silent | A | raise-by-default, opt out with kwarg |
| Type-safe TaskFlow chaining | N/A | architecturally unbridgeable |

**Overall: A- vs current C+.** Implementation-ready.
