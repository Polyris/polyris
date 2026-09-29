---
name: pr-description
description: Write a PR description that reads like a human wrote it — plain prose, honest about scope and gaps, no marketing verbs, no hard line-wraps. Use when the user asks to draft a pull-request description, PR body, or "опис для ПР".
---

# PR description — plain and honest

Draft a pull-request body that a reviewer can skim in 30 seconds and understand:
what shipped, why, and what's still hanging. Nothing else.

## Hard rules

1. **Never hard-wrap paragraphs.** GitHub renders soft line-wrap. One paragraph = one line in the source. Bullets get one line each too.
2. **No marketing verbs.** Ban: *hardening, comprehensive, seamless, closes, robust, empowers, elegant, thoughtful*. Say what changed and stop.
3. **Honest about gaps.** If something was deferred, say so and where it's tracked. "Not done: X (v1.0.2)" beats silence every time.
4. **No self-praise.** Never call your own work "clean", "well-designed", "solid". State facts.
5. **First person plural, past tense.** *"We added the guard because…"* — not *"This PR introduces…"* corporate voice.
6. **No copy from commit messages.** Body is above-the-line synthesis: motivation, top-level changes, test evidence, migration notes.
7. **Length: 150–400 words.** Shorter = trivial or under-explained. Longer = too big to review.

## Structure

Use these sections in order. Omit any that don't apply.

```markdown
## Why

<1–3 sentences on the problem.>

## What changed

- Some/file.py — added X because Y
- other/module/ — extracted the Z helper, three callers migrated

## Testing

<How you verified. E.g.: "1956 pytest / 100% coverage / cfn-lint clean / live smoke: 8/8 tasks green".>

## Deploy notes

<Only if the reviewer/operator needs to do something non-obvious. Omit if merge-and-forget.>

## Not in this PR

<Things considered but deferred, with where they're tracked (issue #, backlog file, milestone).>
```

## Anti-patterns from real PRs

- **Hard-wrapping** every paragraph at 80 chars → renders as broken lines in GitHub. One paragraph is ONE line in Markdown source.
- **Marketing verbs**: "This PR closes onboarding traps", "Comprehensive hardening of the ETL surface" — write "We added X. It fixes Y".
- **Restating the commits**: PR body ≠ commit history.
- **"Test plan" as tool checkboxes**: `- [x] mypy`, `- [x] ruff` — say "all gates green" and give the number that matters.
- **Motivation as a story**: don't narrate the debugging journey.
- **Corporate voice**: "This PR introduces…" or "The changes herein…"

## Project-specific workflow

- Always present to the maintainer for review **before** calling `gh pr create`.
- Never `gh pr create` on your own initiative — the maintainer decides when.
