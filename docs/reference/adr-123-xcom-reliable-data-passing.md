# ADR-123: XCom reliable data passing — separate input record, `xcom.push()` for service tasks

Date: 2026-09-11
Status: Accepted

## Context

Polyris has two XCom paths — `event.upstream` (Lambda / SFN pre-fetched inject) and `xcom.pull()` (Glue / ECS / Batch / EMR direct DDB read). UAT reported "Console shows N upstreams, Lambda got fewer." Investigation across the wrapper, SDK, backend, and UI turned up eight independent problems, not one bug:

1. `$isJson` heuristic in `Get_Dep_Output` wrapped every JSON type that wasn't a plain object or an array of strings/objects (primitives, arrays of numbers, `null`, booleans) into `{"_raw": raw_string}`. Downstream code that read `event.upstream[X].output.field` got `KeyError` because the wrapper hid the real value.
2. Service tasks (Glue / ECS / Batch / EMR) stored the AWS API response (`{JobRunId}`, `{TaskArn}`, `{QueryExecutionId}`, `{StepId}`, `{JobId}`) as `result` — silent metadata leak. Downstream tasks thought that was user data.
3. `task_input` on the canonical `output#{pipeline}#{task}#{date}` row was truncated at 25KB — a shared budget with `result` (350KB). Any combined-size overflow replaced the whole upstream with `{"_upstream_omitted": true, "_size": N}`.
4. The Console UI rendered these markers as raw JSON — no interpretation, no actionable hint.
5. Two reader shapes forced users to maintain two mental models: `event.upstream[X].output` (wrapped in `{output, status}`) vs `xcom.pull(X)` (raw value).
6. Missing / failed dependencies silently returned `{}` — bugs hidden until downstream misbehaved.
7. `DATA_PASSING.md` claimed automatic S3 offload existed. It didn't — no code wrote `{"_s3_ref": "..."}` anywhere in the wrapper. Only the reader-side resolver existed.
8. `PolyrisResultsBucketRead` IAM statement granted `s3:GetObject` on `ResultsBucket`, which is polyris-deploy's CloudFormation artifact bucket — user tasks never wrote there, never read from there for XCom.

## Decision

Ship one PR that closes all eight, without breaking any existing pipeline:

### 1. Separate DDB records for canonical output and Console snapshot

- `output#{pipeline}#{task}#{date}` — carries `result`, `status`, `_pushed_by_task` marker, `run_id`, existing wrapper metadata. Unchanged key format; new fields.
- `input#{pipeline}#{task}#{date}` — new record, carries `task_input` (upstream + variables) up to ~380KB. Own 400KB DDB item budget → no more 25KB truncation, no more `_upstream_omitted` marker for realistic payloads.

Console API reads the new record first, falls back to the legacy `task_input` field on the `output#` row when the new record is absent. Non-breaking through the migration window.

Field name is deliberately `task_date` (not `date`) so `input#` rows never populate the `date-pipeline-index` GSI. Belt-and-suspenders with `is_internal_record()` filtering by the `input#*` prefix.

**Cross-record failure semantics.** The two records are written independently by two SFN states (`Init_Output_Row`, then `Save_Input_Record`), each with a `States.ALL` Catch that logs and continues rather than failing the task. This is deliberate: `input#` is Console-preview-only (never read at runtime by downstream tasks or by the SDK), so a lost `input#` write only degrades a UI snapshot — the task still runs, produces its `result`, and downstream `xcom.get()` reads succeed. Conversely, a lost `Init_Output_Row` write leaves the wrapper without a canonical row until `Save_Success` `UpdateItem`-creates it at task end; concurrent same-date reads during that window get `XComMissingError`, which is the correct semantics ("not yet run"). No cross-record atomic guarantee is attempted because none is needed — the reader contracts (SDK + Console) tolerate either record being absent.

The same "best-effort, log and continue" pattern applies to `_write_synthetic_output_marker` (console_api): a DDB failure during a manual resolution logs a warning but does not block the manual action from completing (its own `try/except ClientError`). Downstream reads on a marker-write failure hit `XComMissingError` from the SDK — safe fail, not silent corruption.

### 2. `xcom.push(value)` API for service tasks

Glue / ECS / Batch job code can write real output directly to DDB with a marker the wrapper detects. `Save_Success_Preserve` (new state) skips overwriting `result` when the marker is present.

The marker is versioned by the current wrapper's `Execution.Id` (stamped as `pushed_run_id` on the DDB row). `Check_Task_Pushed` accepts the marker only when `pushed_run_id` matches the current wrapper's ARN — stale markers from a prior same-date run are rejected. Belt-and-suspenders with `Init_Output_Row`'s `REMOVE _pushed_by_task, pushed_at, pushed_run_id` clause at task start.

New env vars injected by the wrapper into service tasks:
- `POLYRIS_TASK_NAME` — the task_id, needed for the DDB key.
- `POLYRIS_WRAPPER_RUN_ID` — `$states.context.Execution.Id`, needed for run-versioned marker rejection.

EMR is deferred: `addStep.sync` has no Environment field, only `HadoopJarStep.Args`, and unconditionally injecting there would break arbitrary Spark arg parsers. Documented limitation.

### 3. `xcom.get(event, task)` uniform reader

Same call in every task type. Auto-resolves `_s3_ref` claim-check pointers. Falls back from a truncated inline inject to the full-size DDB row automatically. Loud errors by default:

- `XComMissingError` for dep with no recorded output (opt out with `raise_on_missing=False`).
- `XComUpstreamFailedError` for `status != "success"` (opt out with `raise_on_failure=False` — needed for `trigger_rule="all_done"` code).
- `XComTruncatedError` when both inject and DDB come back truncated.

`event["upstream"][X]["output"]` continues to work verbatim. `pull()` continues to work. `PullError` is now an alias for `XComMissingError`.

### 4. `$isJson` heuristic replaced

`$exists($parse($safe)) ? $parse($safe) : {'_raw': $safe}` — one `$parse()` call handles every valid JSON shape; `_raw` fallback stays for actually-corrupted DDB rows only.

### 5. Console UI renders markers explicitly

Every upstream + output state uses the same card grammar — 4px left color stripe + neutral fill, status carried by icon + badge. No full-bleed warn/error/muted banners (they made a fan-in of mixed statuses read as a colour siren). Variants:

- **success** (green) — organic clean output
- **error** (red) — status `failed` / `skipped` / `aborted`; output (if any) available on expand
- **warn** (yellow) — status `unknown` (no output recorded), `_truncated: true` (points at `xcom.get(event, "name")` which auto-falls-back), malformed shape
- **muted** (grey) — `_s3_ref` claim-check pointer
- **manual** (blue) — `_manually_resolved: true` marker (mark_success / skip / fail / stop via UI). Renders a `manual: <resolution>` badge and a one-line human summary — `"Marked success by alice@example.com — verified via S3 logs"` — instead of exposing the raw marker JSON. Applied identically to the downstream's UpstreamDep card and the resolved task's own Output card via one shared `detectManualResolution` helper.

Detection lives in one place (`ui/src/components/TaskDetailModal/manualResolution.ts`) so both surfaces stay in sync when the marker shape evolves. The marker itself carries `_operator` (recorded by `_write_synthetic_output_marker`) so a shared account can attribute intent: Cognito email if the ID token carries it, else `sub`; PAT → `pat:<token_name>`; auth-off → `unknown`. Records written before 0.100.0 lack the field — the UI falls back to a generic `"operator"` label.

The SDK closes the loop: `xcom.get()` and `xcom.pull()` detect the same marker and raise `XComManuallyResolvedError` by default (opt out with `raise_on_manual=False`). Before this change, downstream code doing `xcom.get(dep)["rows"]` on a `mark_success`'d upstream received the marker dict as if it were data and crashed with `KeyError` at the first attribute access; the UI-only fix would have surfaced the intervention visually but left the runtime footgun in place.

The AWS-metadata detection banner (Glue/ECS/Batch → `{JobRunId}` etc.) still shows on the Output card's `warn` variant with the `xcom.push()` hint.

The `_upstream_omitted` legacy marker (pre-0.100.0 pipelines that share a 25KB budget between result and task_input) still gets the "re-deploy this pipeline" hint — a whole-input banner above the card list, not a per-dep variant.

### 6. S3 stays manual claim-check

No auto-offload built. `_s3_ref` reader (10 lines in `xcom.pull()` + `retrieve_result()`) kept as escape hatch. Docs describe the manual pattern honestly — user brings own bucket, own S3 IAM.

Misleading `PolyrisResultsBucketRead` IAM statement removed. New `PolyrisTaskWritePolicy` grants `dynamodb:UpdateItem` on `output#*` keys for `xcom.push()`.

## Consequences

### Positive
- Console shows meaningful debug information instead of cryptic markers — closes the UAT-reported problem.
- Glue / ECS / Batch producers can pass real data via `xcom.push()`.
- One reader API — one mental model.
- Loud errors surface bugs earlier.
- Backfill re-runs no longer risk stale-marker data corruption.
- ~380KB `task_input` capacity vs 25KB — 99% of `_upstream_omitted` cases go away.

### Neutral
- +1 DDB `GetItem` per task success (`Check_Task_Pushed`) — ~$0.30/day per 100k tasks.
- +1 DDB `UpdateItem` per task start (`Save_Input_Record` separate row) — same order.
- SFN state graph grows by 6 new states — cosmetic impact on the AWS Console visual only.

### Negative / migration cost
- Users adopting `xcom.get()` with `trigger_rule="all_done"` must pass `raise_on_failure=False` — old code silently returned `{}` for failed upstreams; the new reader raises by default.
- Users with Glue tasks must remember to call `xcom.push()` to store real data — the Console banner warns when metadata is detected, and docs are prominent, but there's no compiler enforcement.
- `xcom.push()` from a Lambda handler races with the wrapper's `Save_Success` — the SDK emits a `UserWarning` and docs recommend `return value` from Lambda.
- EMR `xcom.push()` unsupported in this release — documented as future work.
- Cross-account `xcom.push()` unsupported — the `pipeline-tokens` table lives in the polyris account; cross-account writes need additional IAM setup. Documented limitation.
- Removed `PolyrisResultsBucketRead` — undocumented user pattern of attaching `PolyrisTaskReadPolicy` specifically to read `ResultsBucket` is now broken. Mitigation is to add a direct `s3:GetObject` policy on the bucket if actually needed.

### Coupled constants

Field names and key formats are shared between `polyris/xcom.py` and `sam/sfn_templates/helpers/run_task/sfn.tpl.json`:

| Coupled item | Value | SDK location | SFN location |
|--------------|-------|--------------|--------------|
| Push marker field | `_pushed_by_task` | `_PUSH_MARKER_FIELD` | `Init_Output_Row` REMOVE + `Check_Task_Pushed` projection |
| Run-id field | `pushed_run_id` | `push()` UpdateExpression | `Check_Task_Pushed` output condition |
| Push timestamp field | `pushed_at` | `push()` | `Init_Output_Row` REMOVE |
| Push counter field | `push_count` | `push()` ADD | read-only (debug) |
| Output key format | `output#{pipeline}#{task}#{date}` | `push()` / `pull()` | multiple states |
| Input record key format | `input#{pipeline}#{task}#{date}` | Console API read only | `Save_Input_Record` |
| Task name env var | `POLYRIS_TASK_NAME` | `ENV_TASK_NAME` | Glue Arguments, ECS/Batch env |
| Wrapper run-id env var | `POLYRIS_WRAPPER_RUN_ID` | `ENV_RUN_ID` | Glue Arguments, ECS/Batch env |

Changing any of these requires updating both sides in the same commit. A parity test would prevent silent drift; adding one is straightforward follow-up work.

## Alternatives considered

- **Automatic S3 offload for large outputs** — rejected as over-engineering. Data-pipeline convention is "big data in S3 as parquet, XCom carries a path" — auto-magic hides where data actually lives. Manual claim-check with `_s3_ref` kept as the escape hatch for the rare case of a genuinely large payload.
- **Type-schema XCom (Dagster IO Managers)** — rejected as architecturally incompatible. Polyris references external AWS compute (Lambda ARN, Glue job name, etc.); it does not own the task code and cannot type-check across the boundary. A separate product, not this feature.
- **Multi-key XCom (`xcom.push("secondary", value)`)** — deferred until a concrete demand exists. Can be added later without breaking the current API (single-value case would map to `key="return_value"`).
- **DSL-time validation of Glue→Lambda metadata leak** — deferred. Would require static analysis of Lambda handler code across an AWS deploy boundary. Out of scope.
- **`ConditionExpression` on `Save_Success` (`attribute_not_exists(_pushed_by_task)`) instead of the pre-read `Check_Task_Pushed` state** — rejected because the catch semantics overload with the existing stale-attempt catch (also `ConditionalCheckFailedException`), making correct routing on failure hard to guarantee.

## References

- [`docs/work/xcom-plan.md`](../work/xcom-plan.md) — full implementation plan and per-file design (frozen historical).
- `ADR-15` — original 25KB per-dep runtime truncation. Unchanged in this decision (it's a real AWS SFN state-payload constraint); only the arbitrary 25KB `task_input` storage cap was replaced.
- `docs/features/DATA_PASSING.md` — rewritten in the same delivery as this ADR.
