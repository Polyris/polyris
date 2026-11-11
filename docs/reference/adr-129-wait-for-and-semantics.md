# ADR #129 — `wait_for=[a, b]` AND semantics via DynamoDB StringSet gate

> **Status:** ACCEPTED — replaces the broken OR-semantics of the previous
> `assets_ready: bool` flag with a per-asset StringSet gate that correctly
> holds the signal until every listed asset has arrived.

## Context

A task declaring `wait_for=[asset_a, asset_b]` should wait for **both** assets
before running (AND semantics). The previous implementation stored a single
`assets_ready: bool` on the task's `pipeline-tokens` record. The first asset
to arrive would call `notify_asset_subscribers`, flip the flag to `true`, and
immediately send `SendTaskSuccess` — waking the task before `asset_b` existed.
`wait_for` was silently acting as OR.

Bug confirmed live 2026-11-11: a consumer with `wait_for=[regression_data_a,
regression_data_b]` entered SUCCESS status while the producer of
`regression_data_b` was still running. The `wait_for_a` task (single-asset
subscriber) behaved correctly; only the multi-asset gate failed.

## Decision

Replace the boolean flag with two DynamoDB StringSet fields on the
`pipeline-tokens` record:

| Field | Writer | Semantics |
|-------|--------|-----------|
| `wait_for_assets` | `check_assets` | The set of asset names that still needed subscribing (were not yet present at registration time). The gate compares against this. |
| `assets_ready_set` | `notify_asset_subscribers` (ADD) | Assets that have since arrived. Atomically grown via DDB `ADD` as each asset event fires. |

Signal condition: `assets_ready_set ⊇ wait_for_assets`.

### check_assets changes

Write `wait_for_assets = set(missing_names)` to the task record via
`tokens_repo.write_pending_assets` **before** saving subscriptions. Writing the
gate first ensures that any event arriving during the subscription-save window
encounters a complete gate immediately; a subscription saved before the gate
would fall back to first-arrival (OR) semantics. This write is scoped to the
missing assets only — assets already present at registration time are not
subscribed and therefore not included. This correctly handles the partial-ready
case: if `asset_a` is already there but `asset_b` isn't,
`wait_for_assets = {'b'}` and only `asset_b`'s arrival needs to close the gate.

After saving subscriptions, check_assets re-evaluates to catch assets that
arrived during the save window. If some (but not all) arrived, they are
credited to `assets_ready_set` via an atomic `ADD`
(`tokens_repo.add_to_assets_ready_set_and_get`, `ReturnValues=ALL_NEW`).
The returned record is checked for gate completeness
(`assets_ready_set ⊇ wait_for_assets`): if complete, check_assets returns
`ready=True` immediately (all remaining subscriptions cleaned up). If not
complete, only the newly-credited subscriptions are deleted and the rest stay
active for the still-missing assets. Subscriptions are deleted only after a
successful write — if the ADD fails the subscriptions remain as a safeguard
(no subscription + no credit = guaranteed hang). The `ADD` is atomic, so
exactly one side — `notify_asset_subscribers` or this partial-recheck path —
observes the gate flip from incomplete to complete and acts on it.

**OR-group limitation:** `wait_for_assets` is not written (and the AND gate
does not apply) when any item in `wait_for` uses OR semantics — first-arrival
semantics apply there, and the gate would incorrectly require all OR members.
Mixed specs (`wait_for=[a, b | c]`) are therefore a known limitation; pure-AND
lists (`wait_for=[a, b]`) are fully gated.

For the immediately-ready path (all assets present at registration), call
`tokens_repo.mark_assets_ready(execution_name, all_names)` which writes the
legacy `assets_ready = true` bool AND the new StringSet fields in one
UpdateExpression. This ensures evaluate_deps sees a consistent record whether
it's invoked by `notify_dependents` (task_deps gate) or directly.

### notify_asset_subscribers changes

When an asset event arrives for a subscriber:

1. Atomically `ADD asset_name` to `assets_ready_set` on the task record
   (`tokens_repo.add_to_assets_ready_set_and_get`). DDB `ADD` is safe under
   concurrent asset arrivals — two Lambdas writing different asset names
   commute; no last-writer-wins risk.
2. Read `wait_for_assets` from the returned record. If present and
   `assets_ready_set ⊄ wait_for_assets`, return False — this is not the last
   arriving asset. Delete the subscription (the asset has arrived; there's
   nothing to wake up for later), but do not signal.
3. If the set is satisfied (or `wait_for_assets` is absent — legacy fallback),
   proceed to the existing evaluate_deps invocation for the Case B task_deps
   check.

The subscription is always deleted after the ADD, regardless of whether the
gate is satisfied. The last arriving asset both deletes its own subscription
AND sends the signal; earlier arrivals only delete.

### evaluate_deps changes

evaluate_deps accepts two new optional fields:

```json
{
  "wait_for_assets": ["a", "b"],
  "assets_ready_set": ["a", "b"]
}
```

When both are present alongside `wait_for`, evaluate_deps uses the set
comparison (`set(wait_for_assets) <= set(assets_ready_set)`) instead of the
legacy bool. The legacy `assets_ready: bool` path remains for old
`pipeline-tokens` rows written before this deploy.

### notify_dependents SFN template changes

Two new fields added to the evaluate_deps invocation payload in
`sfn_templates/helpers/notify_dependents/sfn.tpl.json`:

```json
"wait_for_assets": "{% $exists($states.input.record.wait_for_assets.SS) ? $states.input.record.wait_for_assets.SS : null %}",
"assets_ready_set": "{% $exists($states.input.record.assets_ready_set.SS) ? $states.input.record.assets_ready_set.SS : [] %}"
```

DynamoDB SS (StringSet) attributes come through the raw DDB JSON format as
`{SS: ["a", "b"]}`. The JSONata `.SS` accessor extracts the array.
`wait_for_assets` uses `null` when absent (not empty list) so evaluate_deps
can distinguish "not set" (legacy row) from "empty set" (edge case).

## Key design choices

### `wait_for_assets` = subscribed names, not all declared names

If `wait_for=[a, b]` and `a` is already present at registration, only `b` gets
a subscription. `wait_for_assets = {'b'}`. When `b` arrives, `assets_ready_set
= {'b'}` and the sets are equal → signal. If we stored all declared names
(`{'a', 'b'}`), `a` would never be added to `assets_ready_set` (there's no
subscription for it) and the gate would never close.

### No write to `wait_for_assets` for single-asset subscribers

Tasks with `wait_for=[a]` (one asset) work correctly with or without the new
gate: the single subscription arrival closes the gate immediately. The new code
path is exercised, but the behavior is identical to before. Backward
compatibility is maintained for existing DDB rows via the legacy bool path.

### DDB ADD is the right primitive

`ADD :name_set` grows the StringSet atomically. Two concurrent Lambda
invocations (one for asset_a, one for asset_b) each ADD their own name. DDB
guarantees both names end up in the set. No conditional expressions needed.
`ConditionExpression='attribute_exists(execution_name)'` on the `update_item`
ensures we don't create orphan rows for TTL'd or non-existent records; on
failure the exception is caught, logged, and fails-open (signal anyway).

### Fail-open on DDB failure

A DDB failure on the AND-completeness write (`write_pending_assets`) degrades
to OR behavior — the first asset wakes the task just as the old code did. The
failure is logged at ERROR so it's visible in CloudWatch. Failing closed
(blocking the signal) would be worse: a transient DDB issue would leave tasks
hung to orchestration timeout.

Similarly, `add_to_assets_ready_set_and_get` returning `None` (record not
found) falls back to signaling immediately — same as the legacy path.

## Consequences

- Multi-asset `wait_for=[a, b]` now correctly waits for all listed assets.
- Single-asset `wait_for=[a]` behavior unchanged.
- Old `pipeline-tokens` rows (pre-deploy) lacking `wait_for_assets` continue
  to work via the `assets_ready: bool` fallback in `evaluate_deps`. No
  migration needed.
- Two new DDB fields (`wait_for_assets`, `assets_ready_set`) written per
  `wait_for` task execution. Both are StringSets; both expire with the row's
  TTL.
- One additional DDB `update_item` per asset arrival (`ADD` to
  `assets_ready_set`). Previously this was a `SET assets_ready = true` —
  same number of DDB writes, different semantics.
