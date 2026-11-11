# ADR #128 — `mark_success` wakes `wait_for` subscribers via direct Lambda invoke

> **Status:** ACCEPTED — extends the natural-completion wake-up path to cover
> the manual-intervention path, so human `mark_success` and automatic task
> success behave identically for downstream pull-waiters.

## Context

Two surfaces can mark an asset-producing task as succeeded:

1. **Natural completion.** `run_task/sfn.tpl.json` → `Emit_Asset_Events` → `Notify_Asset_Subscribers`. The `Notify_Asset_Subscribers` state is a `lambda:invoke` on `NotifyAssetSubscribersFunction` with payload
   `{outlets, source_task, source_dag, event_time}`. That Lambda queries dependency subscriptions for each outlet and sends `SendTaskSuccess` to every `wait_for` subscriber. Push consumers (`schedule=[asset]`) get a separate SFN start. Both run on every successful execution of a task that declares `outlets=`.

2. **Manual `mark_success`.** The Console API's `tasks.mark_success` route calls `_execute_task_action` with `emit_asset_events=True`. The shared helper then calls `notify_asset_consumers_for_manual_success` (in `console_api/task_actions.py`), which:
   - Writes an asset event row to `asset-events` for each outlet.
   - Starts the `NotifyAssetConsumersSfn` SFN per outlet to handle push consumers (`schedule=[asset]`).
   - **Did not** notify pull-waiters (`wait_for=[asset]`).

The asymmetry is user-visible: a human marking a task successful would wake push-triggered consumer pipelines but leave `wait_for` subscribers stuck. The feature `mark_success` exists to let an operator complete a task after verifying the work happened (logs, S3). If the downstream pipelines don't react the same way they would on natural completion, intervention is only half-effective.

## Decision

`notify_asset_consumers_for_manual_success` additionally invokes `NotifyAssetSubscribersFunction` directly — fire-and-forget (`InvocationType='Event'`) — with the same payload shape (`outlets`, `source_task`, `source_dag`, `event_time`) that the `run_task` SFN sends on natural completion.

One invocation per `mark_success` call regardless of outlet count: the Lambda already loops across outlets internally to look up subscriptions.

New env var on `ConsoleApiFunction`:
```yaml
NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN: !GetAtt NotifyAssetSubscribersFunction.Arn
```

New IAM Statement on `ConsoleApiRole` policy:
```yaml
- Effect: Allow
  Action: lambda:InvokeFunction
  Resource: !GetAtt NotifyAssetSubscribersFunction.Arn
```

The grant is scoped to a single Resource (not `*`), matching the existing pattern used for `NotifyFunction`.

## Key design choices

### Direct Lambda invoke, not a new SFN step

The push path (`NotifyAssetConsumersSfn`) is a separate SFN because its logic — fan out per subscriber, count AND-trigger satisfaction, start downstream pipelines — needs orchestration. The pull-waiter wake-up is a single Lambda call that runs one DDB query per outlet and sends `SendTaskSuccess`. Wrapping it in a new SFN would add latency and a state machine without gaining anything.

### Fire-and-forget (`InvocationType='Event'`)

Pull-waiter wake-up matches the "best-effort" semantics of the push-SFN start directly above it. A failed invoke (throttling, service outage, IAM denial) logs an error and continues. The `mark_success` HTTP response still returns success to the user.

Rationale: the alternative — synchronous invoke with error surfacing — would require `mark_success` to roll back the DDB status update when the wake-up fails. That's architecturally wrong because `mark_success`'s primary effect (the status change) IS successful; only the downstream notification side-effect failed. Users retry the wake-up by re-invoking Lambda from the console or deploying a fix.

### Symmetry with `run_task` — only after the `SUBSCRIPTIONS_TABLE` routing fix

The invoked Lambda's DAL writes `dependency_key`/`subscriber` composite keys. Before the `SUBSCRIPTIONS_TABLE` routing fix in this same release, the Lambda's env var pointed at `AssetSubscriptionsTable` (schema `asset_name`/`pipeline_name`) — every DDB call hit `ValidationException` caught and logged as warn. The mark_success invoke was therefore technically correct at this level but had no end-to-end effect.

The routing fix (env var repoint to `DependencySubscriptionsTable`) ships in the same PR as this ADR. See the "Fixed — `wait_for` subscription table routing" entry in `CHANGELOG.md` and the regression gate in `tests/sdk/test_wait_for_subscription_table_parity.py`. The two together make the wake-up actually work.

### Log-level discipline

`notify_asset_consumers_for_manual_success` logs ERROR (not WARN) for a failed invoke, matching the log-level rule added to `polyris/CLAUDE.md` in the same release: catches on a wake-up path are ERROR, never WARN.

### Single aggregated warn for missing env vars

A deploy that has either `NOTIFY_ASSET_CONSUMERS_SFN_ARN` or `NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN` unset logs one aggregated warn listing all missing targets, not one warn per missing env var interleaved through the outlet loop. Reduces noise on mis-wired deploys.

## Consequences

- `mark_success` and natural completion wake the same set of consumers. No more divergent intervention path.
- `ConsoleApiFunction` depends on `NotifyAssetSubscribersFunction` via `!GetAtt`. One-way dependency; no circular reference.
- `polyris-ee` Slack handler for mark_success (`polyris-ee/sam/lambdas/console_api/ee/team/slack.py`) invokes a separate code path and does NOT call `notify_asset_consumers_for_manual_success`; it is a pre-existing gap that this ADR does not close.
- Tests: `test_notify_asset_consumers_for_manual_success.py` (9 cases) pins the invoke happens; `test_mark_success_wakeup_wiring.py` (2 cases) pins the template.yaml env var and IAM grant so a CFN revert fails CI even though Python mock-based tests would stay green.

## Rejected alternatives

1. **Route `mark_success` through a new unified "task-succeeded" SFN that does both push and pull notification.** Larger change, requires rewriting the natural-completion path too, and gains nothing because the push and pull paths are intentionally separate (SFN for push's AND-coordination, Lambda for pull's simple DDB query + SendTaskSuccess).
2. **Have `mark_success` emit an EventBridge event that both paths subscribe to.** Adds a new event schema + IAM; delays notification; no benefit over direct invoke.
3. **Keep the asymmetry; document it as a known limitation.** The documented asset behavior is "a task that produces asset X wakes consumers of X." Having that promise silently not apply to the human-intervention path is exactly the class of "docs lie, reality differs" bug this release train is closing.
