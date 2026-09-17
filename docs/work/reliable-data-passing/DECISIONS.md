# Reliable task-to-task data passing — decisions

Status: settled

---

## What the codebase already has

The full pipeline exists end-to-end. No component is missing:

- **`Read_Upstream_Outputs` Map** (`run_task/sfn.tpl.json`) — fetches all declared
  dependency outputs from DDB in parallel, merges into
  `upstream: {dep_name: {output, status}}`, passes to downstream task.
- **Lambda / SFN** — receive `upstream` injected automatically into `event`/`$states.input`.
- **Service tasks (Glue/ECS/Batch/EMR)** — call `xcom.pull("dep")` which reads
  `output#{pipeline}#{task}#{date}` from DDB directly.
- **`Save_Canonical_Output`** — writes task result under the canonical key.
- **`Save_Task_Input`** — writes `{upstream, variables}` to `task_input` field at task
  start (used by Console "Input" tab).
- **`get_task_output` API** — returns `{output, input, truncated}` for Console display.
- **`DATA_PASSING.md`** — documents the contract and all task-type paths.

---

## Settled

### S1 — Tier: OSS / free SDK

Core data-passing is a free feature. No paid boundary. All changes go in this repo.
Rationale: it's the fundamental plumbing that makes orchestration useful.
One-way: no.

### S2 — Three concrete gaps, one open question

Codebase inspection found three concrete bugs/gaps and one ambiguity that needs
your input (see **Open** section).

### S3 — Gap 1 (confirmed bug): `$isJson` heuristic is incomplete

In `run_task/sfn.tpl.json`, `Read_Upstream_Outputs.Get_Dep_Output` uses:

```jsonata
$isJson := $exists($safe) and (
    $safe = '{}' or $safe = '[]' or
    $substring($safe, 0, 2) = '{"' or   ← objects
    $substring($safe, 0, 2) = '["' or   ← arrays of strings only
    $substring($safe, 0, 2) = '[{'       ← arrays of objects only
)
$parsed := $isJson ? $parse($safe) : {'_raw': $safe}
```

**What breaks:** any upstream output that is not a plain object or
array-of-strings/objects falls through to `{'_raw': raw_string}` instead
of being parsed:

| Upstream returns (Python) | Stored as | Downstream receives via `event["upstream"]["t"]["output"]` |
|---------------------------|-----------|-------------------------------------------------------------|
| `{"k": 1}` ✅             | `'{"k":1}'` | `{"k": 1}` — correct |
| `[1, 2, 3]` ❌            | `'[1,2,3]'` | `{"_raw": "[1,2,3]"}` — WRONG |
| `None` ❌                 | `'null'`    | `{"_raw": "null"}` — WRONG |
| `True` ❌                 | `'true'`    | `{"_raw": "true"}` — WRONG |
| `42` ❌                   | `'42'`      | `{"_raw": "42"}` — WRONG |

Note: `xcom.pull()` (service-task path) is NOT affected — it uses `json.loads()`
which handles all JSON types correctly. This bug is SFN-side injection only.

**Fix direction:** replace the heuristic with `$parse()` unconditionally, and
detect failure via `$exists()`. JSONata's `$parse()` returns undefined for
invalid JSON (not an error), so `$exists($parse($safe)) ? $parse($safe) :
{'_raw': $safe}` is the correct expression. Double-call `$parse()` is acceptable
for correctness (expressions are stateless here).
One-way: no.

### S4 — Gap 2 (confirmed missing test): no multi-upstream contract test

`test_run_task_template.py` has `test_lambda_user_payload_merges_under_orchestration`
which tests ONE upstream. The multi-upstream merge path (two producers A + B,
one downstream C) has no test. The Asana task explicitly requires it.

**Fix direction:** add a test that evaluates the `Read_Upstream_Outputs` +
`Prepare_Task_Input` + `Run_Task_Lambda` JSONata chain with two upstream outputs
and asserts both appear under the correct keys in the downstream event.
One-way: no.

### S5 — Gap 3 (confirmed discrepancy): `task_input` omits `payload=` fields

`Save_Task_Input` stores only `{upstream, variables}`. But the Lambda actually
receives the full merged event: `payload fields + metadata + variables + upstream`.
Example:

```python
@task.lambda_function(function_name="fn", payload={"source": "crm"})
def extract(): pass
```

- Runtime event: `{"source": "crm", "current_date": "...", ..., "upstream": {...}}`
- Console "Input" tab: `{"upstream": {...}, "variables": {...}}` — `source` not shown

A user debugging why their Lambda doesn't see `source` gets no help from the Console.

**Fix direction decision needed — see Open section.**

### S6 — `task_input` 25 KB truncation is consistent, not a mismatch

When the full `{upstream, variables}` JSON exceeds 25 KB, `task_input` stores
`{"variables": ..., "_upstream_omitted": true, "_size": N}`. At the same time,
the Map itself already truncated each upstream item at 25 KB before injecting it
into the event. So Console and runtime both see truncated data — this is NOT an
independent mismatch. No separate fix needed here.

### S7 — Missing upstream output → silent `{}` is expected behavior, not a bug

If a dep's DDB record is absent (shouldn't happen in normal flow since the
dependency wrapper guarantees ordering), the downstream receives `output: {}`.
This is edge-case behavior that's acceptable. No change needed here.

---

## Settled (continued)

### S8 — O1 resolved: the mismatch is Console showing N upstreams while Lambda gets fewer

UAT evidence:
- "Це не є input до самої таски, це є input до врапера" — observed that Console Input
  tab showed (e.g.) two upstream entries, but Lambda actually received only 1 meaningful
  input. The person's point: Console shows the wrapper's view, not what the task got.
- "Upstream-downstream, щось воно зламалося" — explicit upstream data-passing failure.

Pipeline evidence (`scenario-4/dag.py` — the concrete test case from pipelines.zip):
```python
@task.lambda_function(function_name="polyris-test-lambda", trigger_rule="one_success")
def daily_report(event):
    sales_data = event["upstream"]["extract_core_sales"]["output"]
    customer_segments = event["upstream"]["enrich_customer_segments"]["output"]
    return {"sales_data": sales_data, "customer_segments": customer_segments}

[extract_core_sales(), enrich_customer_segments()] >> daily_report()
```

Two candidates identified from full code read (dependency_wrapper → registration →
notify_dependents → run_task):

**Candidate A — `$isJson` heuristic (S3).** If an upstream returns a primitive (`42`,
`null`, `[1,2,3]`), `Get_Dep_Output` wraps it in `{'_raw': '...'}` instead of the
real value. Both Console AND Lambda see the `_raw` wrapper — so they AGREE, but
neither shows the correct value. From user perspective: "output came through wrong."

**Candidate B — multi-upstream merge untested.** The `Read_Upstream_Outputs` Map
output expression `$merge($append([], $map($states.result, ...)))` is not tested with
2 dep iterations. Current test `test_lambda_user_payload_merges_under_orchestration`
only passes 1 upstream. If the merge silently drops one entry, only 1 would appear
in both Console and Lambda — which would mean Console and Lambda agree (both wrong).

**Decision:** the multi-upstream test (S4) is the gate — it will confirm or rule out
candidate B. The `$isJson` fix (S3) addresses candidate A. Both ship together. The
`payload=` fields (original Option A) are OUT OF SCOPE — not the mismatch observed.
One-way: no.

---

## Explicitly out of scope

- **DSL-time XComArg validation** — checking at deploy-time that `downstream(extract())`
  references a real task that produces output. Not in the Asana description.
  XComArg today is a dependency handle, not a data reference.
- **`xcom.pull()` changes** — the service-task path is correct and fully tested.
  No changes needed.
- **S3 offload path** — works correctly. Not in scope.
- **`task_input` 25 KB limit** — the limit itself is not changing. Only what fields
  are stored within that limit.

---

## Vocabulary

- **upstream output** — the value stored under `output#{pipeline}#{task}#{date}` in DDB,
  written by `Save_Canonical_Output` when a task succeeds.
- **upstream map** — the `{dep_name: {output, status}}` dict injected into a Lambda/SFN
  event under the key `"upstream"`.
- **`task_input`** — the DDB field on the `output#...` record that stores what the task
  received; shown as "Input" in the Console.
- **`payload=`** — the static key-value dict a user sets on `@task.lambda_function`; merged
  into the Lambda event at runtime.

---

## Summary of work

All items settled. Concrete test case: `scenario-4/dag.py` from pipelines.zip.

1. Fix `$isJson` heuristic in `run_task/sfn.tpl.json` → `Get_Dep_Output` (S3)
   - Replace with `$exists($parse($safe)) ? $parse($safe) : {'_raw': $safe}`
2. Add `test_two_producers_one_consumer` using `scenario-4` pattern (S4)
   - `[extract_core_sales(), enrich_customer_segments()] >> daily_report()`
   - Assert both upstream keys appear in downstream Lambda event
3. Snapshot update + primitive-value regression test (follows from 1)
4. Extend `task_input` to include `payload=` fields in `Save_Task_Input` and
   `Save_Canonical_Output` (S5 / S8 — Option A)
