# ADR #103 — Alert delivery is runtime config in the registry, read by one notify Lambda

## Context

ADR #1 took alerting out of the DSL: what a failure alert goes to is operational config that changes without a redeploy. That left three questions, settled in v0.95.0: where the config and its secrets are stored, who reads them when a task fails, and how long a failed task waits for an operator's decision. Until then the config was baked into each pipeline's state machine (a `slack_channel` field on the registry item, an `alerts` field in the wrapper's input), so a change needed a redeploy, and the delivery logic was spread over Choice states of the wrapper.

## Decision

### 1. Where values are stored

- A pipeline's alert config is the `alert_config` attribute of its `pipeline-registry` item: `enabled_channels` and, per channel, non-secret settings only. `pipelines_repo.get_alert_config` and `set_alert_config` are its only reader and writer in the console; a missing item or attribute reads as `{"enabled_channels": []}`, never as an error.
- Secrets (a webhook URL, a routing key) are never stored in DynamoDB: anyone who can read the table could read them. They are SSM `SecureString` parameters under `/polyris/alerts/{Namespace}/{Stage}/{pipeline}/`, and `alert_config` holds only the parameter name. The notify Lambda reads the parameter with `WithDecryption` when it delivers. `NotifyRole` may read only that prefix, so two stacks in one account cannot read each other's secrets.
- A pipeline registered before this ADR keeps a legacy channel field; `pipelines_repo.migrate_slack_channel` lifts it into `alert_config` once and leaves an existing config alone.

### 1b. The decision-wait timeout is one global setting

How long a failed task waits for an operator's decision is one value per deployment: `decision_timeout_seconds` on the reserved registry item `__global_settings__` (`pipelines_repo.GLOBAL_SETTINGS_KEY`). `run_task` reads it with the same `GetItem` it uses for the registry and falls back to 18,000 s (5 h, `DEFAULT_DECISION_TIMEOUT_SECONDS`) when it is absent. The console API serves it read-only (`GET /api/settings/decision-timeout`, `routes/settings.py`) and answers the default when the read fails, so the console always shows a usable value (`DecisionTimeoutSection`).

### 2. Stage 1: one notify Lambda reads the config and fans out

The notify Lambda (`sam/lambdas/notify/`) is the only code that delivers an alert. Given a pipeline name and a failure, it reads `alert_config` itself (`registry.get_alert_config`) and delivers to each enabled channel in isolation: a channel that fails is a result, never an exception, so one bad channel never blocks the others and the calling state always succeeds. Channels are `Notifier` subclasses registered by explicit registration (ADR #97); this repository ships the generic JSON `webhook` channel, and an unknown channel is a no-op.

### 3. Stage 2: directed actions go through the notify Lambda

What `run_task` and the console ask a notifier to do (post, alert, resolve) is an `action` input of the same Lambda (`actions.py`: `register_action`, `dispatch_action`), not HTTP calls in state machine states. An action no notifier registered is a logged no-op. `run_task` reads no `alerts` field of its input (`test_run_task_reads_no_wrapper_alerts_field`), and no Choice state gates delivery per channel (`test_wrapper_alert_gates_removed`).

### 4. Follow-up C: the console reaches a channel only through the notify Lambda

The console API sends nothing to a channel itself: it invokes the notify Lambda (`ConsoleApiRole` may invoke `NotifyFunction`), so whatever it asks for takes the path a failure takes. A task action that ends a wait for a decision invokes the `resolve` action this way (`utils.py`).

## Consequences

- A channel, its settings or the timeout change without a redeploy; the DSL declares nothing about alerts (`alerts=` raises `TypeError`, ADR #1; `slack_channel=` was removed, ADR #109).
- A new channel is one `Notifier` subclass and one registration.
- An alert costs one Lambda invocation and one SSM read per channel.

## Alternatives

- **Config in the DSL** (the `alerts=` parameter): rejected in ADR #1; a change needed a redeploy and put secrets next to pipeline source.
- **Secrets in the registry item**: rejected; DynamoDB items are readable by anyone with table access.
- **One helper state machine per channel**: replaced by Stage 1 and 2; the delivery logic is Python in one place and is unit-tested there.
