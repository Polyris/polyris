---
area: ui
accepted: by 2026-08-17
---
# ADR #119 — Unified DAG visualization node/edge builder

**Context.** Two independent implementations built (nodes, edges) for DAG visualization:
`generate_dag_json` (the public `polyris-output`/`polyris-validate` surface) and
`_build_pipeline_metadata`'s `dag_metadata` construction (stored as the `dag` field in both
the pipeline registry and per-execution snapshots — what `/pipeline-dag`'s registry
fallback returns for "current structure" mode, ADR #118's neighbor feature). They looked
similar enough to read as "the same thing, written twice" but had quietly drifted:
`generate_dag_json` had a `type` marker, `trigger_rule` (non-default only), and tracked
Glue/ECS/Athena steps (`dag.steps`) that `_build_pipeline_metadata`'s version entirely
lacked; `_build_pipeline_metadata` had `wait_for` (asset pull-dependency metadata) that
`generate_dag_json` lacked. Neither author appears to have known the other existed —
each accreted fields independently as their own caller needed them.

Found while fixing a narrower bug (task-type badges missing on "current structure"
blueprint nodes, since that data only ever flowed through `_build_pipeline_metadata`'s
path) — patching both call sites in parallel would have re-created the exact condition
that caused the drift in the first place: two implementations, one obligation to keep
them in sync, no mechanism enforcing it.

**Decision.** One shared function, `_build_dag_visualization_nodes_edges(dag)`, returning
the canonical `(nodes, edges)` pair with the **union** of every field either caller had
accumulated — not the intersection, and not "whichever the newer caller happens to need."
Both `generate_dag_json` and `_build_pipeline_metadata` now call it and use its output
directly. A single regression guard test (`test_both_callers_produce_identical_nodes_and_edges`)
asserts their outputs are identical for the same DAG — if this class of drift recurs, it
fails there, immediately, rather than silently producing two different graphs depending
on which endpoint answered the request.

**Consequences.**
- Registry/snapshot-sourced DAG data (used by "current structure" mode, and by historical
  per-execution snapshots) now includes `type`, `trigger_rule`, and tracked Glue/ECS/Athena
  steps it never had before. A pipeline using a direct Glue/ECS/Athena step (not through
  `@task`) previously showed an **incomplete graph** in blueprint mode — that step's node
  was simply missing. Fixed as a consequence of the unification, not a separate patch.
- `generate_dag_json`'s output (`polyris-output --json`, `--graph`) now includes `wait_for`
  on nodes that have it — previously absent from that surface entirely.
- All 27 ASL snapshot tests were regenerated (`SNAPSHOT_UPDATE=1`) to reflect the new
  `type` field appearing in registry-stored `dag_metadata`; the diff in each was
  confirmed to be exactly that one field, nothing else, before regenerating.
- Frontend (`DAGGraphFlow.tsx`) was updated to thread `node.task_type` into the node's
  `data.task` in both the real-task and blueprint-fallback cases — the structural
  `task_type` field now always wins as a fallback when no execution-sourced value exists.
