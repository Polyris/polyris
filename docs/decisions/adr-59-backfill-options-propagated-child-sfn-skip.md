---
area: engine
accepted: 2026-05-20
---
# ADR #59 — Backfill options propagated to child SFN — skip_tasks, force, cascade_all, suppress_asset_event

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

Pinned by `tests/sdk/test_run_task_template.py`.

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
