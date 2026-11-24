# STATE.md — where things stand

Twenty lines, not a changelog. What a session needs to know before touching
anything. `CHANGELOG.md` is the history; this is the present.

Update it when something moves. Paths here are checked by
`tests/sdk/test_context_terms.py`.

---

**Version:** 1.3.0, tasks as rows (`docs/reference/adr-137-tasks-as-rows.md`), released 2026-11-24

## In flight

- Nothing recorded since the v1.3.0 release.

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

Principle #15 is not optional: deploy to dev, run `make e2e-coordination`, and exercise one happy path per new feature in the console.
