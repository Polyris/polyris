# STATE.md — where things stand

Twenty lines, not a changelog. What a session needs to know before touching
anything. `CHANGELOG.md` is the history; this is the present.

Update it when something moves. Paths here are checked by
`tests/sdk/test_context_terms.py`.

---

**Version:** 1.2.3 · **Next milestone:** v1.3.0, tasks as rows (ADR #137)

## In flight

- **Tasks as rows** (`docs/reference/adr-137-tasks-as-rows.md`), on `feat/tasks-as-rows`,
  ships as v1.3.0. Done: `Start_Run` writes a run's tasks as rows. The dispatcher, woken by
  the tasks table's stream, decides each row from the rows and the facts and starts
  `run_task`, and the sweeper checks every due run. The console's actions write rows.
  `polyris-up` upgrades from 1.2 and stops what 1.2 left running, and 1.2's engine is gone.
  Reviews of each part and of the whole branch are done, and their blockers fixed. The live
  run (Principle #15) is under way: a dev stack upgraded from 1.2.1 runs the branch, and the
  coordination smoke, its task scenarios included, passes on it. Scale and cost are estimated
  on the harness, not run live: 2,000 tasks hold once four limits were fixed. Left: the
  release rehearsal, then the release.

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
- **Coordination follow-ups after v1.3.0.** Listed with their reasons in ADR #137,
  *Out of scope*. 1.4 removes the move from 1.2: a test fails from 1.4.0 until it goes
  (`tests/sdk/test_d129_upgrade_from_1_2_guard.py`).

## Known debt, measured

- **npm audit: 3 high, 0 critical** — `next`, `postcss`, `sharp`. All unreachable
  under `output: 'export'`; reasoning recorded in `SECURITY.md`. No patched
  `next` 16.2.x exists.

## Before tagging

Principle #15 is not optional: deploy to dev and run a live smoke. The last passes
touched the console's actions (`sam/lambdas/console_api/routes/tasks.py`,
`polyris/coordination/actions.py`), the sweeper and `polyris-destroy` — write paths where
a mock can lie. Run `make e2e-coordination`, and destroy a pipeline with a run in flight.
