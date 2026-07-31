# Spike: Do the 11 trigger rules actually work?

**Question.** We advertise 11 `trigger_rule`s. Do they work end-to-end
with the status model and the SFN execution engine, or is the taxonomy partly aspirational?

**Bottom line.** No — not all 11 work today. The **failure-reactive** rules cannot fire in
the current architecture, because a genuine task failure tears down the whole `Parallel`
before the reactive task can run. Only the **success/skip-reactive** rules work reliably.
Two independent defects are involved:

1. **Skip-bug (cosmetic + status):** a rule that legitimately does *not* fire is marked
   `upstream_failed` (red, run → `aborted`) instead of `skipped` (run → `success`).
2. **Parallel-abort (architectural):** any real task failure calls `SendTaskFailure`, which
   the `Run_All_Tasks` `Parallel` catches → `Pipeline_Failed` (Fail) → **all sibling
   branches are cancelled**, including the very tasks whose rule reacts to failure.

## Method

A faithful discrete simulator encodes the real semantics pulled from code:

- status sets (`constants_generated`): `SUCCESS_OK = {success, succeeded, skipped}`,
  `FAILURE = {failed, upstream_failed, aborted}` — note `upstream_failed` counts as a
  failure for rule evaluation, and `skipped` counts as OK (`ok = success + skipped`).
- `satisfied()` mirrors `evaluate_deps._check_trigger_rule` (terminal case).
- blocked terminal: **current** = always `upstream_failed`; **fix** = `skip` vs
  `upstream_failed` per rule.
- **Parallel-abort**: one `Parallel` (`Run_All_Tasks`) with `Catch: States.ALL →
  Pipeline_Failed (Fail)`; a genuine body-failure emits `SendTaskFailure`, which cancels
  every task that has not already `SendTaskSuccess`'d.
- run status via the real `derive_execution_status`.

Validated against two observed runs (both reproduced exactly): `trigger-rules-reference`
(9 success / 3 `upstream_failed` / run `aborted`, both extractors succeeded) and
`branching-demo` (`alert_on_failure` red, run `aborted`).

## Verdict — can each rule fire when it should?

Scenario: two upstreams → one target with the rule. "Fires" = target actually runs to
completion.

| rule | fire needs a failure? | today | after skip-fix | after full-fix |
|------|:--:|------|------|------|
| `all_success` | no | ✅ works | ✅ | ✅ |
| `none_failed` | no | ✅ works | ✅ | ✅ |
| `none_failed_min_one_success` | no | ✅ works | ✅ | ✅ |
| `none_skipped` | no | ✅ works | ✅ | ✅ |
| `all_skipped` | no | ✅ works | ✅ | ✅ |
| `one_success` | only in mixed | 🟡 racy w/ failures | 🟡 | ✅ |
| `one_done` | only in mixed | 🟡 racy w/ failures | 🟡 | ✅ |
| `one_failed` | **yes** | ❌ cancelled by abort | ❌ | ✅ |
| `all_failed` | **yes** | ❌ cancelled by abort | ❌ | ✅ |
| `all_done` | **yes** (all terminal) | ❌ cancelled by abort | ❌ | ✅ |
| `all_done_min_one_success` | **yes** (all terminal) | ❌ cancelled by abort | ❌ | ✅ |

- **5 reliable** (`all_success`, `none_failed`, `none_failed_min_one_success`,
  `none_skipped`, `all_skipped`).
- **2 racy** (`one_success`, `one_done`) — fire fine in success-only runs; when a failure is
  in the mix they may or may not fire depending on which branch terminates first.
- **4 broken** (`one_failed`, `all_failed`, `all_done`, `all_done_min_one_success`) — their
  trigger *requires* a terminal failure, and that failure aborts the `Parallel` before they
  run.

**`all_done` being broken is the sharpest edge**: it is the "always run cleanup/teardown
regardless of outcome" primitive. Today, if any task fails, cleanup does **not** run.

## Verdict — is the blocked (no-op) case labelled correctly?

Scenario: the rule legitimately does not fire (no triggering failure occurred).

| rule | deps | today | after skip-fix |
|------|------|------|------|
| `one_failed` | success, success | `upstream_failed` → run `aborted` | `skipped` → run `success` |
| `all_skipped` | success, success | `upstream_failed` → `aborted` | `skipped` → `success` |
| `none_skipped` | skipped, success | `upstream_failed` → `aborted` | `skipped` → `success` |
| `none_failed_min_one_success` | skipped, skipped | `upstream_failed` → `aborted` | `skipped` → `success` |
| `all_failed` | fail, success | `upstream_failed` | `skipped` (run stays `failed` — a dep failed) |
| `all_success` | fail, success | `upstream_failed` (correct-ish) | `upstream_failed` |
| `none_failed` | fail, success | `upstream_failed` (correct-ish) | `upstream_failed` |
| `one_success` | fail, fail | `upstream_failed` (correct-ish) | `upstream_failed` |

The skip-fix converts the wrongly-red no-op tasks to `skipped` and turns their run status
from `aborted` to `success`. The AVERSE rules (`all_success`/`none_failed`/`one_success`)
blocked by a real failure stay `upstream_failed`, which is correct.

## Root causes and what each fix buys

**Fix 1 — skip-fix (small, self-contained).**
`evaluate_deps` returns a `verdict` (`ready | wait | skip | upstream_failed`); the poller
(`notify_dependents`) and `registration` send a new `deps_skip` signal when blocked-as-skip;
the `dependency_wrapper` routes `deps_skip` into the existing `Auto_Skip_*` path (writes
`skipped`, notifies dependents, `SendTaskSuccess`). `upstream_failed` path is untouched.
→ Fixes the wrongly-red no-op tasks and the false `aborted` run status. **Does not make any
failure-reactive rule fire.** Default `all_success` is unaffected (it only blocks on
`failed > 0`, which stays `upstream_failed`).

**Fix 2 — de-abort (architectural, unlocks the failure-reactive rules).**
Stop failing the `Parallel` on a task failure. On a *logical* failure, `SendTaskSuccess`
(branch survives), record `failed` in the tokens table, notify dependents, and let
`derive_execution_status` compute the run status from the DDB records (a `failed` task →
run `failed`) instead of from the SFN execution result. With the `Parallel` no longer
aborting, `one_failed` / `all_failed` / `all_done` / `all_done_min_one_success` run to
completion, and `one_success` / `one_done` stop being racy.
Touches: the failure callback in `failure_handler` (`SendTaskFailure` → `SendTaskSuccess`
for logical failures), the `Run_All_Tasks` `Catch` (remove or repoint so a branch no longer
aborts siblings), and confirmation that run-status derivation already keys off DDB (it does —
ADR #112).

## Recommendation for the "11 rules" claim

Shipping "11 trigger rules" while the failure-reactive half cannot fire is
a false claim — the whole point of failure-reactive rules is reacting to upstream *failure* states.

- **Minimum honest OSS:** ship Fix 1, and mark `one_failed`, `all_failed`, `all_done`,
  `all_done_min_one_success` (and note `one_success`/`one_done` timing) as **experimental /
  not-yet-supported** in docs + `EDITIONS`. Advertise the 5 reliable rules as stable.
- **Correct:** do Fix 2 so all 11 work, then advertise all 11.

Fix 1 is a prerequisite for Fix 2 (skip semantics must exist before de-abort makes reactive
rules reachable). Recommend Fix 1 now (self-contained, fixes what users already see), Fix 2
as a scoped follow-up epic "failure propagation without Parallel abort".
