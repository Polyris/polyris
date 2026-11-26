# STATE.md — where things stand

A short page, not a changelog: what a session needs to know before touching anything. `CHANGELOG.md` is the history; this is the present.

Update it when something moves. Paths here are checked by
`tests/sdk/test_context_terms.py`.

---

**Version:** 1.3.0, tasks as rows (`docs/decisions/adr-137-tasks-as-rows.md`), released 2026-11-24

## In flight

- Nothing recorded since the v1.3.0 release.

## Decided, not done

- **Two query hooks have no caller in this repository.** `useBackfillsListQuery` (`ui/src/hooks/queries/useBackfillQueries.ts`) and `useCalendarExecutionsQuery` (`ui/src/hooks/queries/usePipelineQueries.ts`) are exported but used by no component here, and the first calls a route this console does not serve. Moving them out changes a contract (#6), so it waits for the maintainer.
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
