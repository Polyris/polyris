---
name: pr-description
description: Write a PR description that reads like a human wrote it — plain prose, honest about scope and gaps, no marketing verbs, no hard line-wraps. Use when the user asks to draft a pull-request description, PR body, or "опис для ПР".
---

# PR description — plain and honest

Draft a pull-request body that a reviewer can skim in 30 seconds and understand:
what shipped, why, and what's still hanging. Nothing else.

## Hard rules

1. **Never hard-wrap paragraphs.** GitHub renders soft line-wrap on its own.
   Manual `\n` at ~80 characters shows up as forced breaks in the rendered PR.
   One paragraph = one line in the source. Bullets get one line each too.
2. **No marketing verbs.** Ban: *hardening, comprehensive, seamless, closes,
   robust, empowers, elegant, thoughtful*. Say what changed and stop.
3. **Honest about gaps.** If something was deferred, say so and where it's
   tracked. "Not done: X (v1.0.2)" beats silence every time.
4. **No self-praise.** Never call your own work "clean", "well-designed",
   "solid". The reviewer decides that. State facts.
5. **First person plural, past tense.** *"We added the guard because…"* —
   this is a report, not a spec. Not *"This PR introduces…"* corporate voice.
6. **No copy from commit messages.** Commit history already exists. The PR
   body summarizes above the commit level: motivation, top-level changes,
   test evidence, migration notes.
7. **Length: 150–400 words in the body.** If it's shorter, the PR is either
   trivial (fine, skip half the sections) or under-explained. If longer, the
   PR is probably too big to review — split it.

## Structure

Use these sections in order. Omit any that don't apply — an empty section is
worse than a missing one.

```markdown
## Why

<1–3 sentences on the problem. What broke, what was missing, what someone
asked for. No paragraph wrap — one sentence per line only if that helps
scanning, otherwise flow.>

## What changed

<Bullet list of concrete changes at the file-set level, not commit-by-commit.
Each bullet is one line. Reference paths but not line numbers (they rot).>

- Some/file.py — added X because Y
- other/module/ — extracted the Z helper, three callers migrated

## Testing

<How you verified. Prefer terse: "1956 pytest / 100% coverage / cfn-lint
clean / live smoke against dev account: 8/8 tasks green". Not a checklist
of every tool unless a reviewer would specifically ask.>

## Deploy notes

<Only if the reviewer/operator needs to do something non-obvious. If it's
just merge-and-forget, omit this section entirely.>

## Not in this PR

<Explicit list of things considered but deferred, with a pointer to where
they're tracked (issue #, backlog file, v1.0.2 milestone). Silence here reads
as "author forgot to consider it".>
```

## Anti-patterns from real PRs

The following are things I've written that came back as feedback. Don't repeat:

- **Hard-wrapping** every paragraph at 80 chars → renders as broken lines in
  GitHub. One paragraph is ONE line in Markdown source.
- **Marketing verbs**: "This PR closes onboarding traps", "Comprehensive
  hardening of the ETL surface" — write "We added X. It fixes Y". Nothing else.
- **Restating the commits**: PR body ≠ commit history. Body is above-the-line
  synthesis. If a reviewer wants commits, they scroll.
- **"Test plan" as tool checkboxes**: `- [x] mypy`, `- [x] ruff`, `- [x] tsc`
  — reviewer doesn't care that each tool passed individually. Say "all gates
  green" and give the number that matters (tests passed, coverage %).
- **Motivation as a story**: don't narrate the debugging journey. Just state
  the problem in one sentence and move on.
- **Corporate voice**: "This PR introduces…" or "The changes herein…" —
  write like you're telling a colleague at their desk.

## Workflow

1. Read the branch: `git log --oneline base..HEAD` — get the commits.
2. `git diff --stat base..HEAD` — get the file-set surface.
3. Draft in a scratch file first, following the structure above, with NO
   hard wrapping.
4. Read aloud (mentally). If any sentence sounds like a press release, cut
   it and rewrite in plain English.
5. Present to the maintainer for review before calling `gh pr create`.
6. Never `gh pr create` on your own initiative — the maintainer decides when.

## Done when

- Body reads like one engineer telling another what shipped.
- No hard line-wraps in the markdown source.
- Any deferred work is listed under "Not in this PR" with where it's tracked.
- Under 400 words in the body.
