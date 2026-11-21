"""The order in which the code paths of ADR #130 and ADR #137 arrive on the branches that build them.

A scenario marked `needs_binding(stage)` runs once its stage is in
BOUND_STAGES and is skipped until then. The stages of ADR #130's engine
(core, task-deps, assets, push, operations, sweeper) retired with it (D115):
TR-4 generates pipelines with Start_Run, so every scenario of
test_scenarios.py waits for the stage that ports it to the new engine. test_meta.py rejects a marker that
names a bound stage, so markers are removed as their code lands.

ADR #137's stages follow the phases of 1.3: `rows-core` with Start_Run, the
dispatcher, the tasks table's stream and the sweeper (TR-4); `rows-run-task`
with the new run_task (TR-5); `rows-ops` with the operator actions and the
console (TR-6); `rows-migration` with the removals and the upgrade from 1.2
(TR-8). row_invariants.py runs each of its checks from one of them.
"""

STAGES = ("rows-core", "rows-run-task", "rows-ops", "rows-migration")
BOUND_STAGES: frozenset = frozenset({"rows-core", "rows-run-task"})
