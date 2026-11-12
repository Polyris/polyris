# STATE.md — where things stand

Twenty lines, not a changelog. What a session needs to know before touching
anything. `CHANGELOG.md` is the history; this is the present.

Update it when something moves. Paths here are checked by
`tests/sdk/test_context_terms.py`.

---

**Version:** 1.1.4 · **Next milestone:** v1.2.0, the coordination reconciler (ADR #130)

## In flight

- **Coordination reconciler** (`docs/reference/adr-130-coordination-reconciler.md`), on
  `feat/coordination-reconciler`, ships as v1.2.0 in one delivery. Done: `reconcile` and
  `asset-fanout` decide readiness from facts; registration, `notify_dependents`, asset facts
  and asset-scheduled pipelines are wired to them; the old coordination Lambdas, the push
  state machine and the AND-queue table are gone. Next: pause, resume and restart from
  facts, the sweeper and the engine-health banner; then docs, reviews and a live deploy.

## Decided, not done

- **`useBackfillsListQuery` belongs in EE.** Its last free caller became the paid
  `BackfillNavTab`, so by ADR #99's own rule it should live under
  `ui/src/ee/team/hooks/queries/`. Left in the free tree as dead code pointing at
  a route that 404s there. Moving it is a cross-repo contract change (#6).
- **The init wizard ignores an explicit "none"** for dependencies.
  `_build_custom_pipeline` cannot tell "never asked" from "declined", so the
  sequential-chain fallback overrides the answer and a fan-out of independent
  tasks is unreachable. Pinned by a test that documents current behaviour;
  fixing it changes that function's contract.
## Known debt, measured

- **npm audit: 3 high, 0 critical** — `next`, `postcss`, `sharp`. All unreachable
  under `output: 'export'`; reasoning recorded in `SECURITY.md`. No patched
  `next` 16.2.x exists.

## Before tagging

Principle #15 is not optional: deploy to dev and run a live smoke. The last pass
touched `sam/lambdas/console_api/routes/tasks.py` and
`sam/lambdas/console_api/task_actions.py` — DDB write paths where a mock can lie.
Exercise mark_success on a task with an outlet and confirm the asset event lands.
