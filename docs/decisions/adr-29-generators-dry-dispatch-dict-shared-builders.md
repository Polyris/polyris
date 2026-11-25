# ADR #29 — Generators DRY: Dispatch Dict, Shared Builders, Centralized Constants

**Context:** `generators.py` grew to 1545 lines with several DRY violations: `_generate_step_state` was a 238-line if/elif chain across 14 step types, wrapper input dicts were duplicated between Task and Step branches (15 identical fields), asset serialization used 3 different inline patterns (7 occurrences), and the same JSONata expressions and type lists appeared 2-5 times each.

**Decision:** Four targeted extractions, all verified by 60 ASL snapshot tests:

1. **Dispatch dict** — `_STEP_STATE_BUILDERS: Dict[str, callable]` maps `step_type → builder_function`. New step types need one function + one dict entry. Each builder is independently testable.

2. **Shared wrapper input** — `_build_wrapper_input()` returns the base dict with 15 common fields. `_build_task_branch` adds Task-specific extras (alerts, outlets, wait_for, trigger_rule, pipeline_execution_short, slack_mentions_formatted). `_build_step_branch` uses the base dict as-is. Wrapper SFN protocol changes now require one edit.

3. **Asset serialization helpers** — `_serialize_outlet(asset)` and `_serialize_inlet(asset)` replace 7 inline patterns. `_iter_dag_assets(dag)` yields `(task_id, asset, role)` tuples, shared between single-DAG and multi-DAG generators.

4. **Module constants** — `WRAPPER_STEP_TYPES`, `TRACKED_STEP_TYPES`, 8 `JSONATA_*` expressions. Inline definitions removed.

**Alternatives rejected:**
- Full module split (`generators/asl.py`, `generators/validation.py`, etc.) — premature at 1572 lines, would complicate imports and test paths
- `_build_task_config()` unification — Task and Step dataclasses have different field names (`glue_arguments` vs `arguments`, `container_overrides` vs `overrides`), a unified builder would need a mapping layer that adds complexity without reducing total lines
- `validate_asl` decomposition — pure function, rarely modified, 127 lines is acceptable for a validator with 5 checks

**Result:** Largest function reduced from 238 → 155 lines. 100% type annotation coverage. Zero business logic changes — 60 snapshot tests + 10-point business logic verification confirm identical ASL output.
