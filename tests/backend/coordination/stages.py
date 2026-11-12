"""The order in which ADR #130's code paths arrive on the branch that implements it.

A scenario marked `needs_binding(stage)` runs once its stage is in
BOUND_STAGES and is skipped until then. test_meta.py rejects a marker that
names a bound stage, so markers are removed as their code lands.
"""

STAGES = ("core", "task-deps", "assets", "push", "operations", "sweeper")
BOUND_STAGES = frozenset({"core", "task-deps", "assets"})
