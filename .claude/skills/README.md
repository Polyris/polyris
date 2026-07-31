# Working skills

Three, invoked by hand. Copies of what lives in `~/.claude/skills/` — they
describe how one person works, not what Polyris is, so the home directory is
their real home and these are here for reference.

```
/shape   →  (new session)  →  /implement  →  (new session)  →  /break
   ▲                                                              │
 you answer                                                   you decide
```

For a small change, skip `/shape`.

The new session between stages is the mechanism, not ceremony: a reviewer that
remembers why the code was written that way reviews the intention rather than
the code. In the sessions this came from, existing tests caught three wrong
turns and agent self-review caught none.

| Skill | Does | Ends by |
|---|---|---|
| `/shape` | one question at a time, each with a recommendation; never asks what the repo can answer | writing `docs/work/<slug>/DECISIONS.md`, then stopping |
| `/implement` | one change: read first, failing test first for a bug, mutation-test the fix, sweep the blast radius | reporting what the gates said, not how it feels |
| `/break` | attacks the change in a session that has not seen its reasoning; treats CLAUDE.md and the suite as suspects | a findings list — reassurance counts as failure |

Hooks (`.claude/settings.json`) run regardless of which skill is active, so the
gates are not something a skill can forget.

Full guide: `docs/tools/AGENTIC_WORKFLOW.md`.

**Note on the name:** this skill is `implement`, not `build`, because
`.gitignore` line 29 (`build/`, for Python artefacts) has no leading slash and
therefore matches at any depth — a skill called `build` is invisible to git.
