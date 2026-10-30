# Working skills

Three, invoked by hand. Copies of what lives in `~/.claude/skills/` — they
describe how one person works, not what Polyris is, so the home directory is
their real home and these are here for reference.

```
                    ┌─ /grill-with-docs ──┐  terminology unclear, big design decision
                    │                     ↓
start ──────────────┤                  /shape  →  (new session)  →  /implement  →  (new session)  →  /break  →  /pr-description
                    │                                                     ↑
                    └─────────────────────────────────────────────────────┘  small/clear change
```

- **`/grill-with-docs`** — terminology unclear or architecture uncertain → writes `GLOSSARY.md` + ADRs. Batched question rounds, fast.
- **`/shape`** — scope and contracts unclear → writes `docs/work/<slug>/DECISIONS.md`. One question at a time, slower, permanent record.
- Both are optional; for a clear small change go straight to `/implement`
- **`/refactor`** replaces `/implement` when the change is purely structural (rename, extract, move)
- EE repo lives at `../polyris-ee/` — always sweep it during `/implement`, `/refactor`, and `/break`

The new session between stages is the mechanism, not ceremony: a reviewer that
remembers why the code was written that way reviews the intention rather than
the code. In the sessions this came from, existing tests caught three wrong
turns and agent self-review caught none.

| Skill | Does | Ends by |
|---|---|---|
| `/grill-with-docs` | relentless interview against CLAUDE.md + ADRs + codebase; writes new terms to `GLOSSARY.md` and hard decisions as ADRs inline | frontier empty, user confirms shared understanding |
| `/shape` | one question at a time, each with a recommendation; never asks what the repo can answer | writing `docs/work/<slug>/DECISIONS.md`, then stopping |
| `/implement` | one change: read first, failing test first for a bug, mutation-test the fix, sweep the blast radius | reporting what the gates said, not how it feels |
| `/refactor` | restructure without changing behaviour — green baseline first, full symbol sweep, no bug fixes bundled | all callers updated, grep returns zero old names, gates green |
| `/break` | attacks the change in a session that has not seen its reasoning; treats CLAUDE.md and the suite as suspects | a findings list — reassurance counts as failure |
| `/pr-description` | writes the PR body — plain prose, honest about scope, no marketing verbs | one markdown block ready to paste |

Hooks (`.claude/settings.json`) run regardless of which skill is active, so the
gates are not something a skill can forget.

Full guide: `docs/tools/AGENTIC_WORKFLOW.md`.

**Note on the name:** this skill is `implement`, not `build`, because
`.gitignore` line 29 (`build/`, for Python artefacts) has no leading slash and
therefore matches at any depth — a skill called `build` is invisible to git.
