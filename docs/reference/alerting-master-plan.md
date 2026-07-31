# Master plan: finish alerting → full consolidation

The end state we agreed on (verified by two spikes): **all alert *sending* lives
in the notify Lambda; the SFNs keep only orchestration** (when to alert, when to
wait, the token, the 5h timer). Per-pipeline channel + timeout everywhere.
EventBridge Connection and the three Slack/PD helper SFNs disappear.

This plan gets there in ordered, independently-shippable steps. Each step either
is container-safe or is flagged as needing a real `sam deploy` + dev test, since
nothing touching the live path can be smoke-tested here.

Status today: delivery (Phases 0–4) + channel-mode (B) + test-button (C) DONE and
green. Everything below is what remains.

---

## Stage 1 — container-safe, no deploy risk (do first)

### 1a. failure_handler: collapse 3 alert states → 1 Lambda call
- `Get_Alert_Config` + `Has_Channels` + `Fan_Out_Alerts` (3 SFN states) become one
  `Send_Alerts` Task that `lambda:invoke`s the notify Lambda once.
- The notify Lambda gains a **batch mode**: given `pipeline_name` + the failure,
  it reads `alert_config` from DynamoDB itself, then loops the enabled channels
  with per-channel try/except (so one channel failing can't break another — the
  isolation the SFN `Map` gave us for free now lives in Python).
- failure_handler drops from 11 → 9 states. Everything else
  (Update_Status_Failed, Notify_Dependents, token callback) untouched.
- **Nothing that calls failure_handler changes** — dependency_wrapper, run_task,
  etc. only `startExecution` it; its internals are a black box to them.
- IAM: the notify Lambda needs DynamoDB read on the registry (it currently only
  reads SSM). Add it.
- Tests: notify batch-mode (reads config, fans out, isolates failures);
  failure_handler structure (9 states, Send_Alerts present, old 3 gone).
- Risk: 🟢 low. failure_handler's alert path already goes through the Lambda; this
  just moves the config-read + loop into it. Still: a real deploy test is wise
  before prod since it's the failure path, but there's no critical-path edit.

### 1b. Wait timeout configurable in the UI — DEFERRED to Stage 2

**Why deferred:** the intended path (read the timeout from `$states.input.alerts`)
turned out to be the *legacy* alerts channel — the one fed by the old `alerts=`
DSL argument we deprecated in Phase 0. The new config (channel, severity, and the
timeout) lives in `alert_config` in the registry, which the SFN does **not** see
through its input. Getting the timeout to `Wait_For_Decision` per-pipeline
therefore needs the SFN to read `alert_config` from the registry directly — a
DynamoDB read added to run_task, which is the critical path and a bigger change
than a "small field". That read is the same one Stage 2 introduces when run_task
is edited for consolidation, so the timeout rides along with Stage 2 rather than
being bolted on through the legacy channel now.

**Requirements when we do it (Stage 2):**
- Default timeout is **5 hours** (today's value, `decision_timeout_seconds =
  18000`). Not 3h.
- The **UI must show** the timeout field pre-filled with the 5h default, clearly
  presented as a default the user can change (e.g. "Response timeout (hours)",
  value 5, editable). An empty/unset value falls back to 5h.
- `Wait_For_Decision` stays an SFN `Wait` (a 5h-class timer is SFN's job); only
  its `Seconds` source changes — from the compile-time substitution to the
  per-pipeline value read from `alert_config`.

For now `decision_timeout_seconds` stays hardcoded to `"18000"` (5h) — unchanged,
working.

**End of Stage 1:** delivery fully Lambda-driven, timeout per-pipeline, no helper
SFNs removed yet. All container-verifiable except the two deploy notes above.

---

## Stage 2 — move the remaining *sends* into the Lambda (needs dev deploy)

This is the consolidation. Each sub-step moves one more post off an SFN
`http:invoke` (EventBridge) onto a `lambda:invoke` against the notify Lambda, and
adds the matching "action" to the Lambda. The SFNs keep Wait + token; only the
posting moves.

### 2a. Interactive Slack post → notify Lambda
- Add `InteractiveSlackNotifier` to the notify Lambda: builds the blocks+buttons
  payload (~2.6 KB), resolves the per-pipeline webhook from SSM. The buttons still
  point at `${console_api_endpoint}/api/action/*` — **button click handling
  (slack.py) is untouched**; only who *posts* the message changes.
- `interactive_choice_slack`'s two `http:invoke` states → `lambda:invoke`. Pass
  `pipeline_name` so the Lambda picks the channel from the registry/SSM →
  per-pipeline buttons (the thing that's global today).
- run_task still holds the token + Wait_For_Decision. Unchanged.
- Risk: 🔴 high. Interactive path is the critical waitForTaskToken flow. Dev
  deploy + a real Slack click test required before prod.

### 2b. run_task live PagerDuty alert → notify Lambda (+ wait timeout from config)
- `Send_PagerDuty_Alert` (fires while the task is live, in the 5h window) →
  `lambda:invoke` with a "live alert" action. (First resolve the open question
  below — is this a duplicate of the terminal alert or a distinct signal?)
- **Wait timeout (the deferred 1b) lands here.** Since run_task is already being
  edited and gains a registry read, add reading `alert_config.decision_timeout_seconds`
  and feed it to `Wait_For_Decision`'s `Seconds`, falling back to the 5h default.
  Plus the UI field (default 5h, editable) + endpoint round-trip + the
  hours→seconds conversion.
- Risk: 🔴 high. Critical path. Dev deploy + test.

### 2c. dependency_wrapper PagerDuty resolve → notify Lambda
- `Resolve_PagerDuty` (closes the incident on success/skip) → `lambda:invoke` with
  a "resolve" action.
- Risk: 🟡 medium. Not the token path, but still a real PD-resolve test on dev.

**End of Stage 2:** every Slack/PD *send* (terminal alert, live alert,
interactive post, resolve) goes through the notify Lambda. SFNs hold only
orchestration.

---

## Stage 3 — delete the now-dead machinery (after Stage 2 lands)

- Delete `pagerduty_alerter`, `pagerduty_resolver`, `interactive_choice_slack`
  helper SFNs — their only job was the HTTP post the Lambda now owns.
- Delete the `SlackConnection` EventBridge Connection + its template wiring —
  nothing uses `http:invoke` anymore.
- Remove the now-unused template substitutions (`slack_webhook_endpoint`,
  `connection_arn`, `decision_timeout_seconds`, etc.).
- Tests: route-table / template completeness updated; full suite green.
- Risk: 🟡 medium. Deletion only, but a dev deploy confirms nothing references the
  removed resources.

---

## Stage 4 — final cleanup + docs (the original Phase 5)

- Remove any dead per-task `slack_channel` storage left in registration.
- Docs: DSL reference (alerts removed), Settings → Alerts how-to (channel mode,
  timeout, test button), CHANGELOG entry for ADR #103 + this consolidation.
- QA pass: stale-doc sweep, version drift, broken links.

---

## Open question to resolve (not code) — before 2b

**PagerDuty alert appears at two moments:** run_task (task failed but live, 5h
window) and failure_handler (terminal failure). Is that:
- **intended** — two distinct signals ("intervene now" vs "it's dead"), on-call
  reads them differently? Then 2b keeps both, just moves the post into the Lambda.
- **a duplicate** — on-call gets paged twice for one failure? Then one of them

**RESOLVED (2026-06-24): not a duplicate — on-call is paged once.** Traced both
payloads. Both send `event_action: trigger` with the **same** `dedup_key`
(`pipeline/task/date`):
- `pagerduty_alerter` (run_task, live failure, 5h window): trigger, dedup
  `pipeline/task/date`.
- `PagerDutyNotifier` (failure_handler, terminal): trigger, dedup
  `pipeline/task/date`.

PagerDuty Events API v2 deduplicates `trigger` events by `dedup_key`: a second
trigger with the same key **updates the existing incident**, it does not open a
second one. So the run_task alert opens the incident ("failed, you have 5h to
intervene") and the failure_handler alert folds into that same incident
("terminal now") — one incident, paged once. This is correct as two signals
collapsed into one incident, not a double-page.

**Implication for Stage 2b:** keep both fire points. When `Send_PagerDuty_Alert`
(run_task) moves to the notify Lambda, it must keep emitting `event_action:
trigger` with the **same** `dedup_key` formula (`pipeline/task/date`) so the
dedup contract is preserved. Do NOT change the dedup_key shape in either path —
that is what keeps it one incident. (A future "resolve on success" — Stage 2c —
also keys off the same dedup_key to close that incident.)

The old open question, kept for history:
- **a duplicate** — on-call gets paged twice for one failure? Then one of them
  goes during 2b.

This decision only affects 2b's shape; it blocks nothing earlier.

---

## What never moves (the honest boundary, restated)

- `Wait_For_Decision` (the 5h timer) — SFN. A Lambda can't sleep 5h.
- `waitForTaskToken` (holding the token across the pause) — SFN orchestration.
- Button-click handling (`slack.py` `/api/action/*`) — console_api. The buttons
  are about *controlling the task*, same as the UI; that logic stays where task
  control lives.

Everything else — every *post* — ends up in the notify Lambda.

---

## Ordering rationale

```
Stage 1  container-safe (1a) + light deploy (1b)   ← do now
Stage 2  consolidation, each needs a dev deploy    ← when ready to test on dev
Stage 3  delete dead SFNs + EventBridge            ← after 2 proves out
Stage 4  cleanup + docs                            ← last
```

Stage 1 gives immediate, low-risk wins (config-read into Lambda, per-pipeline
timeout). Stage 2 is the real consolidation and is gated on your ability to deploy
+ click-test on dev, because it touches the live token path and can't be verified
in-container. Stage 3 is pure deletion once Stage 2 holds. Stage 4 closes the
books.

Each step is independently shippable — we can stop after any stage with a
consistent, working system.

---

## РОЗЧЕПЛЕННЯ cleanup debt (track — do NOT forget)

When alerting was split across the two repos (РОЗЧЕПЛЕННЯ), the transitional
Slack/PagerDuty SFN helpers were **left in public** deliberately, because
run_task (public) references their ARNs and splitting them now would break the
public build. They are temporary — Stage 2–3 deletes them entirely.

**Left in public for now (must be cleaned in Stage 3):**
- `sam/sfn_templates/helpers/pagerduty_alerter/` — paid PD alert poster
- `sam/sfn_templates/helpers/pagerduty_resolver/` — paid PD resolve poster
- `sam/sfn_templates/helpers/interactive_choice_slack/` — paid Slack buttons poster
- the EventBridge `SlackConnection` + the substitutions wiring these into run_task

**Why deferred, not split into ee:** these are not the long-term home of the
logic — Stage 2 moves their posting into the notify Lambda (already split:
framework public, Slack/PD in notify/ee/), and Stage 3 deletes the helpers + the
EventBridge Connection outright. Splitting transitional code into ee just to
delete it next would be wasted churn. The *real* paid delivery code (Slack/PD
notifiers) is already correctly in notify/ee/.

**Cleanup trigger:** during Stage 3 (delete dead machinery), confirm these three
helpers + SlackConnection are gone from public, and that run_task no longer
references their ARNs. After that, public has zero paid alerting SFN code.

Also still owed from the split itself:
- `tests/backend/test_alerting.py` — currently public; the one ee-only class
  (`TestPagerDutyAlerterLinks`, exercises pagerduty_alerter) rides along in public
  for now since the helper is still there. When the helper is deleted (Stage 3),
  delete that test class too.

### template.yaml split debt (track — do NOT forget)

`template.yaml` lives only in public (ee has no template — it is a code overlay).
Confirmed: **public template = monorepo template MINUS exactly 3 resources**
(NotifyLogGroup, NotifyFunction, NotifyRole). Nothing else diverges (public ⊆
monorepo). So the notify Lambda infra (those 3 resources) is added to the public
template as free infrastructure (the Lambda *framework* is free; Slack/PD code is
the ee overlay shipped into the notify CodeUri at deploy).

**Debt to clean later (ConsoleApiRole crossing the tier line):**
- The monorepo ConsoleApiRole gained `lambda:InvokeFunction` + a `NotifyFunction`
  ref for the **test-webhook button** (follow-up C), which is an *ee* feature
  (Settings → Alerts → Test). It now sits in the public template's ConsoleApiRole.
  This is a (small, harmless) tier smear: a free-tier role holding an invoke perm
  for a paid feature. The Lambda is still only *invoked* when the ee test route is
  present, so it is inert in OSS — but the perm shouldn't be in the public role.
- **Cleanup option (Stage 3/4):** either (a) move that invoke perm to a separate
  policy attached only in the paid deploy, or (b) accept it as a no-op in OSS and
  document it. Decide during the Stage 3 machinery cleanup. Low priority — inert,
  not a leak (no secret, no paid code; just an unused IAM allow in OSS).

When cleaning: re-confirm public template still == monorepo minus the 3 notify
resources, and that ConsoleApiRole carries no paid-only perms in the public tree.

---

## Stage 2 BLOCKER discovered (2026-06-24) — alert_config not in SFN input

While drafting Stage 2 I assumed `input.alerts` carried the per-pipeline
alert_config shape (`{slack: {webhook_param}, pagerduty: {routing_key_param,
severity}}`). **It does not.** Traced the actual runtime shape:

- `input.alerts.pagerduty` is the **severity string** (e.g. `"critical"`), not an
  object. `input.alerts.slack` is similarly a value, not `{webhook_param: …}`.
- The old helper SFNs got the PagerDuty routing key from the **deploy-level
  substitution** `${pagerduty_routing_key}` — one key for the whole deployment,
  NOT a per-pipeline SSM parameter.
- So the per-pipeline SSM params (`webhook_param`, `routing_key_param`) that live
  in `alert_config` in the registry **never reach the SFN input today**. The
  interactive Slack / live PD / resolve helpers run on the legacy `input.alerts`
  (values) + the single deploy routing key.

**Consequence:** Stage 2 cannot just "switch the SFN Task from helper-SFN to
lambda:invoke". The notify Lambda actions (interactive_slack, live_pagerduty,
resolve_pagerduty) are written and tested and expect the alert_config shape — but
nothing feeds that shape into the SFN. The first SFN draft was reverted (run_task,
dependency_wrapper restored to their helper-SFN form) because it read fields that
don't exist at runtime.

**What Stage 2 actually needs first (the real 2a-prerequisite):**
1. **Get alert_config into the flow.** Either (a) run_task/wrapper reads
   `alert_config` from the registry (a DynamoDB GetItem — the same read the wait
   timeout needs, so they share it), or (b) the trigger that starts the wrapper
   builds the input from `alert_config` and passes the structured shape in. Decide
   which — (a) keeps the SFN self-contained but adds a critical-path read; (b)
   moves the coupling to the launch point.
2. **Only then** point the SFN Tasks at the notify Lambda actions, reading the
   now-present `alert_config` shape. The Lambda side (notify/ee/actions_ee_impl.py)
   is already correct for that shape.
3. The **wait timeout (deferred 1b)** rides on the same registry read from step 1
   — once alert_config is in the flow, `decision_timeout_seconds` comes with it.

**Status of Stage 2 code:**
- ✅ notify Lambda action framework (`actions.py`) + ee actions
  (`actions_ee_impl.py`) + tests — DONE, correct, green. They are the *target*;
  they wait for the input plumbing.
- ❌ SFN re-pointing (2a/2b/2c) — reverted. Blocked on step 1 above.
- This step-1 plumbing is itself a real-deploy item (the input shape can only be
  confirmed against a live execution), so it belongs in the same dev-deploy
  session as the rest of Stage 2.

---

## Stage 2 wiring DONE (2026-06-24) + timeout 1b note

**Stage 2 SFN wiring is now done** (container-validated; live click still needs
dev). The fix that unblocked it: the notify Lambda actions read alert_config from
the registry **themselves** by pipeline_name (via registry.get_alert_config,
boto3 resource → plain dict), exactly like the batch fan-out. So the SFN no longer
needs the alert_config shape in its input — it passes only pipeline_name +
failure + the deploy-level console_api_endpoint. This sidesteps the legacy
input.alerts shape entirely (that was the BLOCKER above).

Wired:
- run_task `Interactive_Slack` → `lambda:invoke` notify (action interactive_slack)
- run_task `Send_PagerDuty_Alert` → `lambda:invoke` notify (action live_pagerduty)
- dependency_wrapper `Resolve_PagerDuty` → `lambda:invoke` notify (action resolve_pagerduty)
- template: run_task gains `notify_function_arn` + `console_api_endpoint` subs;
  wrapper gains `notify_function_arn`. IAM already allows it (OrchestrationRole
  has lambda:InvokeFunction "*"; NotifyRole already reads registry + SSM).
- Flow integrity verified: run_task 42 states, wrapper 22 states, 0 broken
  transitions. JSON + YAML valid.

**Still deploy-gated (cannot be container-tested):** a live Slack button click
resolving the waitForTaskToken, a real PD trigger/resolve cycle, and confirming
the live execution input actually carries pipeline_name where the new payloads
read it. The helper SFNs (pagerduty_alerter, interactive_choice_slack,
pagerduty_resolver) are now UNUSED by run_task/wrapper but still defined — Stage 3
deletes them after a dev deploy confirms nothing references them.

**Wait timeout (1b) — intentionally NOT done here.** `decision_timeout_seconds`
stays the deploy-level 5h default. Making it per-pipeline needs a *new field in
alert_config + the UI* (it is not in alert_config today) AND a way for the SFN
`Wait` to read it — and a `Wait` cannot read from a Lambda, so it needs a small
registry GetItem state before `Wait_For_Decision`. That is a separate slice
touching UI + backend + SFN; tracked for its own change, not folded into the
Stage 2 send-migration. Current 5h default is unchanged and correct.

---

## Stage 2 — manual-resolve path also migrated (2026-06-24)

While checking Stage 3 deletion safety, found a **fourth send path** that the
Stage 2 SFN wiring had missed: `console_api/utils.py:resolve_pagerduty`. This
fires when a human resolves a task **manually** (UI button or Slack button →
ee/team/tasks.py, ee/team/slack.py) — distinct from the wrapper's automatic
resolve on task success. It was still calling the `pagerduty_resolver` helper SFN
via `sfn.start_execution`.

Migrated it to the notify Lambda too (action `resolve_pagerduty`):
- `config.py`: added a lazy `lambda_client` (mirrors the `sfn` lazy client).
- `utils.resolve_pagerduty`: now `lambda_client.invoke` (InvocationType 'Event',
  fire-and-forget) with `{action: resolve_pagerduty, pipeline_name, failure}`.
  Reads nothing from `alerts_json` anymore — the Lambda reads the routing key from
  alert_config by pipeline_name, same dedup_key (pipeline/task/date).
- `NOTIFY_FUNCTION_ARN` is already in the ConsoleApi env; ConsoleApiRole already
  has lambda:InvokeFunction + a NotifyFunction ref (was there for the test button).
- Tests (`test_utils.py::TestResolvePagerduty`) rewritten for the Lambda path —
  4 tests green. Full console_api suite green (290).

**Result: all three helper SFNs now have zero live callers** — pagerduty_alerter
(run_task), pagerduty_resolver (wrapper + utils), interactive_choice_slack
(run_task) are all bypassed. Stage 3 deletion is now unblocked for the
container-checkable part (removing the resources + dead substitutions + the
now-unused `PAGERDUTY_RESOLVER_ARN` env). A dev deploy still confirms the live
system before the resources are actually torn down.

---

## Stage 3 DONE (2026-06-24) — dead machinery removed

All three helper SFNs and the EventBridge Slack plumbing are deleted now that
every send path goes through the notify Lambda (Stage 2 + manual-resolve).

Removed from template.yaml (7 resources):
- `PagerDutyAlerterSfn` + `PagerDutyAlerterLogGroup`
- `PagerDutyResolverSfn` + `PagerDutyResolverLogGroup`
- `SlackInteractiveSfn` + `SlackInteractiveLogGroup`
- `SlackConnection` (EventBridge Connection) + `SlackApiDestination`

Removed parameters/conditions (deploy-level PagerDuty model, now per-pipeline via
alert_config/SSM): `PagerDutyRoutingKey` param, `SlackWebhookEndpoint` param,
`HasPagerDuty` condition.

Removed dead substitutions/env: `pagerduty_alerter_arn`, `pagerduty_resolver_arn`,
`interactive_slack_sfn_arn`, `slack_alerter_arn`, `connection_arn`,
`slack_webhook_endpoint`, `PAGERDUTY_RESOLVER_ARN`, `ConnectionArn`.

Deleted SFN template files: `sam/sfn_templates/helpers/{pagerduty_alerter,
pagerduty_resolver,interactive_choice_slack}/`.

Tests: removed the stale classes/methods that exercised the deleted machinery
(`TestPagerDutyAlerterLinks`, the interactive_choice_slack mention tests),
inverted `TestMainTFWiring` (now asserts the alerter ref is *gone* and
notify_function_arn is present). SDK parametrized tests that globbed the deleted
.tpl.json dropped with them. All green: SDK 661, backend 112 (test_alerting 75),
console 264, notify 16; ee notify+actions 22, console alerts 16; ruff clean both.

**This is still container-validation only.** A dev deploy must confirm CloudFormation
cleanly removes these resources (no lingering references in a live stack) before
this is considered fully shipped. The OrchestrationRole `lambda:InvokeFunction "*"`
and NotifyRole registry/SSM perms remain (used by the live wiring).

**Remaining: Stage 4** (final cleanup + docs) and the **deploy-gated confirmations**
for Stage 2/3 (live Slack click, real PD cycle, CloudFormation teardown).

---

## Stage 4 — split into (a) docs now, (b) legacy-field cleanup deferred

### Deferred (own careful change): legacy alerts_json / slack_channel teardown

The old per-execution alert fields are now **inert for alerting** (the notify
Lambda reads everything from alert_config in the registry — Stage 2), but they are
still woven through SFN records and the API contract, so removing them is a
separate, careful change, not folded into Stage 4 docs:

- `alerts_json`: written by run_task (DDB task record), read by restart_task which
  forwards it as `alerts` — but run_task no longer reads `input.alerts` (Stage 2
  reads alert_config), so the forward is a dead flow. Inert: it writes an unused
  attribute, nothing breaks.
- `slack_channel`: written by registration, read by restart_task / wrapper, and
  **surfaced in API responses** (`routes/tasks.py`, `routes/pipelines_list.py`
  default `#alerts`). The Slack action reads channel from `alert_config.slack`, not
  this — but the API field may be consumed by the frontend, so dropping it is an
  API-contract change.

**Why deferred:** removing these touches registration + restart + wrapper SFNs and
two API routes; low individual risk but multi-file, and the API field could break
the frontend if something reads it. Worth doing, but as its own change with a
frontend check — not blind, and not bundled with the doc sweep. (Two prior
hasty SFN edits this cycle were reverted; this one gets the same caution.)

### Done now: docs

### Done now: docs (continued)

Stage 4 docs are done:
- **DSL.md**: the `alerts=` argument section rewritten as deprecated (ADR #103) —
  it is accepted one release then ignored; alerts move to Settings → Alerts. All
  `alerts={...}` examples removed from the DSL doc (kept only in the deprecation
  note). Removed the dead `slack_channel` from the default_args example.
- **New `docs/features/alerts.md`**: the user-facing Settings → Alerts how-to —
  browser notifications (free), Slack (webhook/channel-mode/mentions + the
  Skip/Success/Fail/Restart buttons), PagerDuty (routing key + severity + the
  one-incident dedup behavior), the Test button, the failure flow, and where
  secrets live (SSM, only the param name in the registry).
- **README.md**: the "Alerts Configuration (required)" section replaced with the
  two-layer model + a link to alerts.md; all `alerts={...}` examples removed.
- **getting-started (TUTORIAL, PROJECT_STRUCTURE) + reference (AIRFLOW_MIGRATION,
  DESIGN_DECISIONS) + operations (TROUBLESHOOTING)**: `alerts={...}` /
  `alerts=None` examples removed so no doc still teaches the deprecated argument.
- Suites green after the doc sweep (SDK 661, backend 112, console 264).

**Stage 4 status:** docs done. The legacy-field teardown (alerts_json,
slack_channel) is the one remaining piece, deferred above as its own careful
change (multi-file, touches the API contract). Everything else in the alerting
redesign (Stages 1–3 + Stage 2 manual-resolve + Stage 4 docs) is complete and
container-validated; only the live-deploy confirmations remain.

---

## Legacy-field teardown DONE (2026-06-24) + mentions gap fixed

Removed the dead old-model alert fields now that everything reads from
alert_config. **Frontend was grep-checked first** (per the decision): zero `.ts`/
`.tsx` references to `slack_channel` in either repo, so dropping the API field is
safe — no prod UI consumes it.

**Gap found and fixed first:** the new `interactive_slack` action read channel and
webhook from alert_config but **not mentions** — so removing the legacy
`slack_mentions_formatted` would have silently dropped @-mentions. Added
`_format_mentions` to the action (user ID → `<@ID>`, group → `<!subteam^ID>`,
here/channel specials) reading `alert_config.slack.mentions`, appended to the
Slack message header. 2 new tests. This had to land before the teardown.

Removed:
- `alerts_json`: run_task write (UpdateExpression + value), restart_task
  forward. `slack_mentions_formatted` likewise (now sourced from alert_config).
- `slack_channel`: registration write, restart_task read, dependency_wrapper
  (both the big Output JSONata and the inner pass-through), the API responses in
  `routes/tasks.py` and `routes/pipelines_list.py`, and the update field in
  `ee/team/tasks.py`. Template `DefaultSlackChannel` param + its two
  substitutions removed (no SFN reads it anymore).

Kept on purpose: `pipelines_repo.migrate_slack_channel` — a one-shot helper that
seeds alert_config from a legacy `slack_channel` on old DDB records. It is
idempotent, not called by live code, and harmless; useful for migrating
pre-ADR-103 pipelines whose records may still carry the old attribute.

Tests: removed the 8 stale alerts_json tests (`TestRunTaskAlertsInDDB`,
`TestRestartTaskAlerts`, `test_alerts_json_roundtrip`). All green: SDK 661,
backend 104, console 264/271, notify 16, ee notify+actions 24. Frontend untouched.

(Pre-existing ee whitespace lint noise — W293 in unrelated files — is its own
cleanup, not part of this change; the files touched here are ruff-clean.)

---

## Timeout 1b DONE (2026-06-24) — global decision-wait timeout, Team-editable

The deferred 1b is done, with the clarified shape: ONE global timeout for the
whole deployment, editable by Team (read-only on free) — not per-pipeline.

Storage (variant B, registry): a reserved `__global_settings__` item in the
PipelineRegistryTable holds `decision_timeout_seconds`. `pipelines_repo` gains
`get_global_settings()` (degrades to 18000s) and `set_decision_timeout()`.

SFN: run_task gains `Get_Decision_Timeout` — a getItem on `__global_settings__`
inserted between `Save_Error_Waiting` and `Check_Is_Backfill`, assigning
`decision_timeout_seconds` (default 18000 on any miss). `Wait_For_Decision` now
reads the assigned variable instead of the `${decision_timeout_seconds}`
substitution (removed from the template). Verified all three paths into the wait
pass through Get_Decision_Timeout, so the variable is always set. A Wait cannot
read a Lambda, which is why this is a registry getItem in the SFN itself.

Backend: GET `/api/settings/decision-timeout` is free (routes/settings.py) so the
value is always visible; PUT is Team-tier (ee/team/settings.py) with clamping
(60s–14d). The OSS build strips ee/, exposing the read but not the write.

UI: `DecisionTimeoutSection` (free component) shows the value to everyone and
enables editing only on Team builds (detected via a non-empty paid surface).
Free renders read-only with an "Editable on Team" hint; Team renders an editable
hours field + Save. Wired into SettingsModal as a free section.

Tests green: storage 4, GET 3, PUT 5, SFN structure +4 (Get_Decision_Timeout +
Wait-reads-variable + the two updated save_error tests), UI 4 (free + Team). All
suites green: public SDK 661 / backend 106 / console 281; ee console 276; UI.

(Note: monorepo ee/team has a pre-existing circular-import that prevents isolated
collection — unrelated to this change; ee is validated through the split/merged
tree, which is green.)
