---
name: break
description: Attack a change until it breaks. Use when the maintainer asks to "check everything", "prove it's good", "try to break it", or before declaring any work done. Reads every touched line, hunts for the failure the change enables, and reports findings — never reassurance.
---

# Break — attack the change until it yields

You are not reviewing your own work. You are trying to **break** it, on behalf of
someone who will be embarrassed in public if you miss something.

The maintainer has had to type "check everything, read every line, try to break
it" repeatedly. That request was never noise — every single time it was made, a
real bug was found. This skill exists so it does not have to be typed again.

## The rule that governs everything below

**Reassurance is a failure.** "Looks good", "should be fine", "I'm confident" are
not review outputs. If you finish and have found nothing, you have not looked
hard enough — go to §5 and pick a technique you skipped. A review that finds
nothing is reported as *"searched X, Y, Z; here is what I could not rule out"*,
never as *"it's fine"*.

## 0. Reproduce before you claim

Never report a bug you have not made happen, and never ask the maintainer to try
something you could run yourself. If you believe a bug exists: write the failing
test first, watch it fail, then fix it, then watch it pass. If you cannot
reproduce it, say so explicitly — "read the code, did not run it" — and label the
finding as unconfirmed.

Symmetrically: never report a fix you have not mutation-tested. Re-introduce the
bug, confirm the new test catches it, restore. A test that cannot fail proves
nothing.

## 1. Read every changed line

Not the diff summary. The actual lines, plus the surrounding function. For each:

- What did this line do before? What does it do now?
- What input makes the new behaviour wrong?
- Is any variable, import, branch, or CSS rule now dead? (Remove it.)
- Is any comment or docstring now false? (A stale comment is a bug — #9.)

## 2. Sweep the blast radius (Principle #23, both repos)

For every symbol touched — function, constant, enum *member*, field, CSS class,
component, prop, route — `grep -rn` across **CE and EE**, production and tests.

Then ask the three questions that have actually caught things here:

- **Who else writes this shape?** Migration needed for existing rows?
- **Who else reads it?** Does the consumer accept the new value?
- **Is there a second site of the same pattern** that was not migrated? (Array
  form *and* OR-comparison form. Backend *and* frontend. Both repos.)

## 3. Check the claims, not just the code

This is where the real bugs have been. Documentation and tests are **suspects**,
not evidence.

- Does `CLAUDE.md` assert something about this area? **Verify it against the
  code.** Four of its claims were false and had been for months — "DAL 100%",
  "no unittest.mock", the coverage-omit justification, an ADR #99 example.
- Does a test assert current behaviour, or *correct* behaviour? Three assertions
  in `HelpModal.test.tsx` were pinning a bug in place while the same file
  documented the opposite. A green suite can encode the defect.
- Does a guard actually run? `check-no-paid.sh` and `check-no-leak.sh` both
  reported success while doing nothing, and neither was wired into CI. **Make
  every guard fail on purpose once** and confirm it fails.
- Does a justification still hold? Three modules sat in the coverage omit list
  under an "AWS/CLI" rationale with zero boto3 references.

## 4. Use it as a user, then as an attacker

**As a user:** run the flow the README documents, end to end, from a clean
directory. Every command, every flag. Check exit codes are non-zero on failure —
and check them *directly*, not through a pipe (`$?` after `cmd | tail` is
`tail`'s status; this produced a false finding once).

Then the paths a real user hits that docs never show: empty input, missing file,
malformed file, wrong flag, two conflicting flags, the operation run twice, the
operation interrupted halfway.

**As an attacker:** what is the worst input this accepts? What is the state you
can get it into and not get out of? A persisted value with no UI to reset it is
the shape of the `viewMode='calendar'` trap — look for that shape specifically.

## 5. Techniques, when the obvious pass finds nothing

- **Boundaries.** Empty, one, exactly the limit, limit+1, zero, negative,
  null/None/undefined/"".
- **Ordering.** Two writers to the same key: which lands last? Does the test read
  `call_args` (most recent) when it means a specific call?
- **Edition matrix.** Every UI change: does it hold with `paidSurface = {}`?
  Every route: does it 404 correctly in OSS? Run `check-oss-build.sh`.
- **Type honesty.** Does an annotation claim something the code cannot deliver —
  or *forbid* something the code deliberately does? Both directions have bitten
  here. `mypy polyris/` is a real gate; the wheel ships `py.typed`.
- **Dead-on-arrival.** After the change, can any branch still be reached? An
  unreachable defensive guard is dead code, not safety.
- **Silent failure.** Does anything `except: pass`, swallow a non-zero exit, or
  `|| true` a command whose failure matters?

## 6. Report

End with the completeness report from Principle #23 — and lead with the bad news:

> **Found:** N issues — [each: what, where, reproduced yes/no, severity]
> **Changed:** A, B, C.
> **Blast radius:** swept N consumers (CE + EE + tests); M sites of the pattern —
> migrated P, left Q because R.
> **Contract:** both sides checked.
> **Dead code:** none / removed X.
> **Suites:** [list each, with counts].
> **Not covered:** Z — because R. ← never omit this section

If a finding needs a decision rather than a fix (contract change, tier boundary,
UX trade-off), **do not fix it silently**. Report it, state the options and your
recommendation, and stop — Principle #6.

## What this skill must never produce

- "All tests pass" as the headline. The suites passing is table stakes, not a
  finding.
- A clean report on a change you did not run.
- A fix bundled with a contract change the maintainer did not approve.
- Silence about what you chose not to check.
