---
name: implement
description: Implement one change with its gate. Use for any coding work. Reads the decisions doc if one exists, writes code and tests, sweeps the blast radius. Never expands scope.
---

# Build — implement one change, prove it

Implement **one** change. If `docs/work/<slug>/DECISIONS.md` exists for this
work, read it first — those decisions are settled and not yours to revisit.

## Is this a refactor?

If the change is **purely structural** — rename, extract, move, reorganise, no new behaviour, no bug fix — use `/refactor` instead. The refactor skill enforces a green baseline before you start and a full symbol sweep, which this skill does not. If you are unsure: if the tests would pass unchanged after your edit, it is a refactor.

## Boundaries

**Do not expand scope.** If you find something else broken, report it at the end
under "Found while working" and keep going. A bundled drive-by fix makes the
change unreviewable and un-revertible.

**Do not change a contract on your own.** DDB schema, API shape, SFN flow, the
free/paid boundary — if the work turns out to need one, stop and say so (#6).

## Order of work

1. **Read before writing.** The files, their tests, and every consumer of every
   symbol you will touch — CE *and* EE. Never guess a signature; open it.
   Answer questions from the codebase rather than asking the maintainer.
2. **Test first where it is a bug fix.** Write the failing test, watch it fail.
   A test that has never been red proves nothing.
3. Implement.
4. **Mutation-test the fix.** Re-introduce the bug, confirm the test catches it,
   restore. Say you did this.
5. Run `bash scripts/verify-changed.sh` (the Stop hook runs it too).
6. Sweep the blast radius (#23): consumers, pattern siblings, both sides of any
   contract, dead-after-change.
7. Report what you found along the way, separately from what you fixed.

## Reporting

Report what the gates said, not how it feels. Never "should work" — either it ran
and passed, or you did not run it. If you could not run something, name it.

If you had to reverse yourself mid-task, say so. That is signal, not failure.

## Adding a new API route?

Principle #16 applies — a new route requires in the same delivery:
1. New `tests/e2e/test_*.py` — happy path (200/202), response shape, 4xx on bad input, 404 for missing IDs.
2. Update the route-count guard: free-subset in `tests/sdk/test_templates.py`; full count in EE `tests/test_route_table_ee.py`.
3. Barrel export in `routes/__init__.py` (free routes only).

EE path: `../polyris-ee/` relative to this repo root — grep there too when sweeping blast radius.

## Done when

The gates are green, the blast radius is swept and reported — including EE (`../polyris-ee/`). Then stop. Run `/break` in a **new session** — one that has not seen this reasoning. Then `/pr-description`.
