# ADR #60 — Runtime enforcement of `skip_on_backfill` task flag (v0.78.1)

#### Context

Tasks have a `skip_on_backfill: bool = False` parameter in the DSL.
It's documented as "Skip this task during backfill runs" and used in
real pipelines (`pipelines/acme/daily/dag.py:292,297,302` for three
scraper tasks). The flag is stored in pipeline-registry's
`tasks_metadata.skip_on_backfill` field, displayed in UI types, and
covered by an SDK smoke test that verifies DSL-level set/get.

External code review (2026-05-22) and direct verification revealed
that **the flag was declared, stored, and never enforced at runtime**.
A grep across the entire codebase for any path that reads
`skip_on_backfill` outside of declaration/storage/test sites returned
zero results. Backfill runs were executing every task in the
pipeline regardless of the flag.

The bug was hidden because:
- DSL test verifies `task.skip_on_backfill is True` (declaration), not
  runtime behavior.
- Real pipelines using the flag tolerated the bug since scrapers also
  succeed on re-run (just wasteful API calls).
- No backfill snapshot or contract test asserted the flag's effect.

#### Decision

Enforce `skip_on_backfill` at the backfill API boundary by computing
`skip_task_ids` as a union of two sources:

  (a) **Task-subset complement** — when the user supplies a positive
      `tasks: [...]` list, skip everything NOT in the subset.
  (b) **`skip_on_backfill=True` flag** — read from
      `pipeline_registry.tasks[*].skip_on_backfill` and added to
      `skip_task_ids` regardless of whether a user subset is provided.

User-explicit subset takes precedence over the flag. If a developer
passes `tasks=['scraper_t1']` and `scraper_t1.skip_on_backfill=True`,
the task runs — the user said so explicitly. The flag is a safety
default for "I just clicked Backfill", not an unconditional ban.

#### Contract

| Scenario | task_subset | skip_on_backfill tasks | skip_task_ids |
|---|---|---|---|
| Default backfill, no flags | None | none | `[]` |
| Default backfill, flags set | None | t1, t3 | `[t1, t3]` |
| Subset, no flags | `[t1, t2]` | none | complement |
| Subset includes flagged | `[t1]` (t1 flagged) | t1, t3 | `[t2, t3, t4]` |
| Subset excludes flagged | `[t2, t4]` | t1, t3 | `[t1, t3]` |

Order in `skip_task_ids` matches order in the registry's task list
(stable, deterministic).

#### Implementation

Helper `_compute_skip_task_ids(target_pipeline, task_subset)` in
`sam/lambdas/console_api/routes/backfill.py`. Returns list of task
IDs to skip. Called from `start_backfill` after task_subset
validation, before building the SFN input. The pipeline SFN (built by
`polyris.generators`) already consumes `$states.input.skip_tasks` via
`JSONATA_SKIP_TASKS` — the Choice state in each task short-circuits
if its task_id appears in the list. No SFN-template change needed.

#### Backwards compatibility

Pipelines without `skip_on_backfill=True` on any task: unchanged
(empty skip list, same as before). Pipelines that already use the
flag: scrapers now correctly skip — this is the intended behavior
the DSL promised since v0.71. Counts as a **bug fix, not a breaking
change** because no production behavior depended on the flag being
silently ignored.

#### Cost

Zero. Same SFN structure, same Lambda invocations. The flag merely
adds task IDs to an existing list field.

#### Tests

Four contract tests in
`sam/lambdas/console_api/tests/routes/test_backfill.py::TestSkipOnBackfillFlag`:

- `test_flag_skips_scrapers_when_no_task_subset` — canonical case.
- `test_flag_ignored_for_tasks_in_explicit_subset` — explicit override.
- `test_flag_with_subset_unions_correctly` — union semantics.
- `test_no_flagged_tasks_yields_empty_skip_list` — guards against
  spurious flag enforcement when no task is flagged.

#### Alternatives considered

1. **Enforce in generated SFN ASL via a new Choice state per task** —
   would couple every task generator to backfill semantics. Rejected:
   API boundary is the simpler injection point and re-uses the
   existing `skip_tasks` plumbing.
2. **Enforce in bulk-backfill SFN via JSONata over Map input** —
   would need DDB lookup per partition iteration, costly.
3. **Strip the flag entirely** — silently breaks the existing DSL
   contract and forces every user to migrate to subsets. Rejected.
