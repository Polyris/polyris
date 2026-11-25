# ADR #21 — Single Source of Truth for Date Variables

**Decision:** JSONata in `Prepare_Task_Input` is the single source for all date-derived variables. Python (backfill.py) only provides `current_date` and non-derivable extras.

**Variable ownership:**

| Source | Variables | Why |
|---|---|---|
| JSONata (always) | date_compact, date_slash, year, month, day, day_of_week, previous_date, next_date, minus_7/14/30_days (ALLOW_UNSUCCESSFUL_SPIDER_RUN until 1.3.0) | Derivable from current_date with simple arithmetic |
| Python (backfill only) | minus_1_month, minus_3_months, day_of_year, week_of_year, is_backfill, is_reprocess | Calendar math or special formats JSONata can't compute |

**Merge order:** `$merge([$dateVars, $vars])` — backfill/custom variables always override computed values.

**Rationale:** Before this, 15+ variables were duplicated between `backfill.py` (Python) and `Prepare_Task_Input` (JSONata). If logic changed in one place, the other would diverge silently. Now each variable is computed in exactly one place.

**Update:** the canonical source is now `polyris/variables.py` (name + JSONata expression); `Prepare_Task_Input`'s `$dateVars` block is **generated** from it by `polyris.codegen.sync_variables`, and `task_variables.py` re-exports it. The "one place" is now the registry, and drift is impossible (the template is generated, checked by `test_template_generated_from_registry`).
