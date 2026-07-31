# ADR #110 — Task & execution intervention moved from Team to free; config mutation stays paid

> **Status:** ACCEPTED — implemented. Re-classifies existing routes/handlers
> across the open-core seam; the *total* API surface is unchanged (63 routes).
> No IAM, DynamoDB, or SFN-flow changes. Supersedes the "intervention = Team"
> wording in the open-core boundary (ADR #98/#99); the render-prop provider
> pattern (ADR #104) is unchanged.

## Context

The open-core boundary (ADR #98/#99) drew the line as "authoring + basic read =
free; operations / observability / **intervention** = Team." In practice the
*intervention* half of that rule was too broad. A free user can author a
pipeline, deploy it, run it, and read its state — but under the old split could
not skip a stuck task, mark a manually-recovered task successful, stop a runaway
task, restart a failed one, or stop / pause / resume a live execution. Those are
not a premium tier; they are the minimum needed to *operate* a pipeline you are
allowed to run. A free product that can start work but cannot unstick it is
broken.

The handlers had been over-stripped into `ee/team/tasks.py` and
`ee/team/executions.py`. Worse, because the UI provider that drives them lived in
`src/ee/team/`, the public build silently shipped an **empty**
`PipelineActionsProvider` slot — so the toolbar, pause-banner, and task-modal
intervention controls rendered but did nothing in OSS (the host's
`paidSurface.PipelineActionsProvider ? … : content(null)` branch fell through to
`content(null)`).

## Decision

Move live-run intervention to free; keep durable configuration and fleet-level
lifecycle paid. The revised boundary:

- **Free** = author + deploy + run + read + **intervene on a live run**: task
  actions `skip` / `fail` / `success` / `stop` / `restart` (and `retry`, a
  deprecated alias of `restart`), and execution control `stop` / `pause` /
  `resume` / `extend`.
- **Team** = pipeline-level lifecycle (`pipeline-pause` / `pipeline-restart`),
  backfill, the asset console (ADR #105), alert integrations (Slack / PagerDuty
  config + interactive callbacks), Personal Access Tokens, per-pipeline
  observability (`pipeline-logs` / `pipeline-metrics`), and **configuration
  mutation** — `PUT /api/task-config` (edit `timeout_seconds` / `max_retries`)
  and `PUT /api/settings/decision-timeout`. The read counterpart
  `GET /api/settings/decision-timeout` is free (ADR #103).

The distinction is *one-off intervention on a run you own* (free) vs *changing
stored configuration or driving pipeline-level lifecycle across the fleet*
(paid). Editing a task's timeout is a durable config change, reversible and safe
to gate; skipping a stuck task is a here-and-now fix a free operator needs.

### 1. Backend

`ee/team/executions.py` moved wholesale into `routes/executions.py` (all four
handlers + their private helpers — nothing paid remained, so the module was
deleted and dropped from `team.MODULES`). The six task handlers plus
`_execute_task_action` / `_write_notify_warning` moved from `ee/team/tasks.py`
into `routes/tasks.py`; `update_task_config` (PUT) is all that stays in the
now-slim `ee/team/tasks.py`. The shared `resolve_task_item` helper was already
free (proprietary imports free — never the reverse; ADR #98), so no import
direction was inverted. The moved handlers are re-exported from the OSS barrel
(`routes/__init__.py`). The **total** route surface is unchanged (63); this is a
Team→free reclassification, so free went 17 → 27 and Team shrank correspondingly.

### 2. UI

`usePipelineActions`, `ActionModal`, and `PipelineActionsProvider` moved from
`src/ee/team/` into the free `src/hooks/` and `src/components/`. The provider only
ever hit `task-*`, `execution-*`, and `pipeline-run` — all free — so the whole
render-prop provider is free; there were no paid handlers to leave behind.
`PipelineActionsParams` / `PipelineActions` moved from `ee-contract.ts` to
`@/types` (the host↔provider contract no longer crosses the open-core seam), and
the `PipelineActionsProvider` slot was removed from `PaidSurface`.
`PipelineDetail` now imports the provider directly and wraps its content
**unconditionally** — the old `paidSurface.PipelineActionsProvider ? … :
content(null)` branch is gone, which is what fixes the dead-controls bug in the
public build.

### 3. Guards & tests

The full-build route-table guard (`ee/team/tests/test_route_table_ee.py`) was
rewritten to **derive** the free/Team split from module namespace rather than pin
a magic count or a hand-maintained `team_critical` list. A route's tier is a
property of the module that registers it — `ee.*` modules are paid, `routes.*`
modules are free, which is the open-core invariant itself (ADR #98, "OSS must
never import ee"). The guard re-runs each tier's `register()` on a throwaway
`Router`, then asserts the two tiers are disjoint and together cover the whole
`ROUTES` table (`len(ROUTES) == len(free) + len(team)`, computed — not the old
literal `== 63`). This is strictly stronger: a route accidentally owned by both
tiers, or a future re-strip of an intervention route back into `ee/`, fails the
guard immediately, whereas a fixed count could not catch a same-key move. The
free-subset guard in `tests/sdk/test_templates.py` (which runs in both builds)
asserts the free intervention routes are present and that `PUT /api/task-config`
is Team-only in the stripped build.

Two source-location smoke tests migrated from `ee/team/tests/test_smoke_ee.py` to
the free `tests/sdk/test_smoke.py`, now asserting the handlers live in
`routes/tasks.py`. The behavioural tests (`test_stop_restart`,
`test_executions_pause`, task-action idempotency) moved with the handlers into
`tests/backend/`; the Slack idempotency tests stayed in `ee/team/tests/` (Slack
callbacks are still paid).

## Reversibility

This is the РОЗЧЕПЛЕННЯ / overlay model working as intended in the *re-freeing*
direction: promoting a feature from Team to free was a `git mv` of the handlers
into the free tree plus register lines — not a rewrite (ADR #99). The render-prop
provider *pattern* (ADR #104) is unchanged and still applies to any future paid
cross-cutting handler — `PipelineActionsProvider` simply is no longer one of its
examples, being free now.
