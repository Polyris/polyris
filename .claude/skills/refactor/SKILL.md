---
name: refactor
description: Restructure code without changing behaviour — rename, extract, move, reorganise. Use when the change is purely structural (no new features, no bug fixes). The invariant is: tests green before AND after, behaviour identical. If you find a bug while refactoring, stop and report it — do not fix it here.
---

# Refactor — restructure without changing behaviour

The single invariant: **behaviour is identical before and after.** Every test that passed before must pass after. No new features, no bug fixes bundled in.

## Before you touch anything

1. **Green baseline first.** Run `bash scripts/verify-changed.sh`. If the suite is not green before you start, stop — you cannot tell what you broke vs what was already broken.
2. **Name every symbol you will rename or move.** List them before touching a single file.

## Order of work

1. **Grep every symbol** you plan to rename across CE *and* EE (`grep -rn`), production and tests. Know the full blast radius before the first edit.
2. Make the change atomically where possible — rename in one pass, not file by file over multiple commits.
3. **Run the suite after each logical unit** (one rename, one extraction) — not at the end. A suite that was green, went red, and came back green tells you where you introduced and fixed a regression. A suite run only at the end tells you nothing.
4. Remove dead code that the refactor exposes — imports, branches, CSS rules, unused params.
5. Update every reference: type annotations, docstrings, CLAUDE.md rules that quote the old name, error messages, comments.

## What you must not do

- Fix a bug you find along the way — report it under "Found while working", keep going.
- Add new behaviour "since we're in here anyway".
- Leave a compatibility shim (`old_name = new_name`) unless there is an explicit external caller that cannot be updated in the same change.

## Blast radius is the whole checklist

For every renamed symbol, answer all four:
- **All callers updated?** (`grep -rn 'old_name'` returns zero results outside history)
- **Tests still test the right thing?** A test that imports the old name via a shim tests the shim, not the code.
- **Docs updated?** CLAUDE.md, docstrings, error messages, comments that quote the old name.
- **EE updated?** EE path: `../polyris-ee/` relative to this repo root — grep there too.

## Done when

Gates are green (`bash scripts/verify-changed.sh`), `grep -rn 'old_name'` returns zero results, blast radius reported. Then stop — run `/break` in a new session.

## Reporting

```
Renamed: A → B, C → D
Callers updated: N (CE), M (EE)
Dead code removed: X, Y
Tests: all green before and after
Found while working: [list or "none"]
```
