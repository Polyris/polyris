# Agentic development workflow

How this repo is developed with a coding agent, and what stops the agent cutting
corners. Everything here is optional tooling — the project builds and tests
without it — but it exists because specific failures kept recurring, and each
piece names the failure it prevents.

Related: `CLAUDE.md` (the rules themselves), `.claude/CONTEXT.md` (vocabulary),
`docs/reference/STATE.md` (current state).

---

## The problem this solves

Across several sessions of agent-written work, the failures were consistent, and
none of them were bad code generation:

| Failure | Times |
|---|---|
| Agent reported "done" without running the gates | 3 |
| Maintainer had to ask "check everything" before a real bug surfaced | 2 |
| Agent's own review missed what existing tests caught | 3 |
| `CLAUDE.md` asserted something that was no longer true | 5 |
| Agent destroyed an uncommitted working tree | 1 |

Three of the five are mechanical: a script can prevent them entirely. One is
judgement, and needs a review that does not share the implementer's assumptions.
One — the drifting document — needs the document to be executable.

---

## The loop

**Small change**

```
/implement →   (new session)   →   /break   →   you decide what to fix
```

**Backlog item, or anything where the plan is still fuzzy**

```
/shape   →   (new session)   →   /implement →   (new session)   →   /break
```

The new session between stages is the point, not ceremony. An agent reviewing its
own work carries the assumptions that produced it. A session that has not seen
the reasoning reads the code the way a reviewer would.

### `/shape`

For a fuzzy idea, before any code. Asks **one question at a time**, each with a
recommended answer rather than a menu, and never asks what it could read from the
repo. Settles the scope, the tier (`CLAUDE.md`, *Repository context*), the contracts, and the vocabulary. Writes
`docs/work/<slug>/DECISIONS.md` as terms resolve. Drafts an ADR only for a
genuinely one-way decision — most sessions should add none.

Ends by telling you to open a new session. It does not implement.

### `/implement`

One change, start to finish. Reads before writing. For a bug
fix: failing test first, then the fix, then mutation-test it — re-introduce the
bug and confirm the test catches it. Runs the gates. Sweeps the blast radius
(CLAUDE.md #23). Reports what it found separately from what it fixed.

Refuses to expand scope or change a contract on its own (#6).

### `/break`

The review, in a session that has not seen the implementation. Its governing rule is that
**reassurance counts as a failure**: a clean pass is reported as "searched X, Y,
Z; here is what I could not rule out", never as "looks good".

It treats `CLAUDE.md` and the test suite as *suspects*, not evidence — both had
been lying. Five claims in the document were false. Three assertions in
`ui/src/components/HelpModal.test.tsx` were pinning a bug in place while the same
file documented the opposite behaviour.

The skills are in `.claude/skills/`. Copy them to `~/.claude/skills/` to use them in other repositories too ([Setup](#setup)).

---

## What runs without being asked

### `scripts/verify-changed.sh`

Runs the gates the current `git status` says are relevant.

| You touched | It runs |
|---|---|
| `polyris/`, `tests/` | ruff, mypy, pytest + the 100% coverage floor |
| `sam/` | cfn-lint, constants/logger sync, four codegen drift gates, every Lambda suite, console_api |
| `ui/` | typecheck, eslint, vitest, build, `check-oss-build.sh` |
| anything | the open-core guard, version consistency |

A full run costs about four minutes, most of it the UI build. Scoping keeps the
common path at seconds, which is the difference between a gate that runs and one
that gets skipped "this once". `--full` forces everything; outside a git
checkout it runs full, because it cannot scope safely.

`--hook` is the light mode the Stop hook uses, because every session in the
checkout runs it at every stop:

- It does nothing when the checkout is unchanged since the last green run, from
  any session or mode. Without that, a second session re-ran the whole gate on
  the first session's unchanged work at each of its stops.
- It runs pytest without the coverage floor, and leaves out the UI build. Run
  the script without flags (or `make test-cov`) before you commit; CI runs both.

Every mode waits for a run already going on the same checkout instead of
starting a second one (where `flock` exists), then usually finds that run's
green result.

### `.claude/settings.json`

- **Stop** — runs `verify-changed.sh --hook` when the agent finishes a turn.
  Reporting work as done on red gates stops being possible rather than
  discouraged. This is CLAUDE.md #21 and #23 enforced instead of trusted.
- **PreToolUse (Bash)** — runs `scripts/guard-destructive.sh` before every shell
  command.

Each hook names its script through `$CLAUDE_PROJECT_DIR`, so it runs whatever
directory the shell stands in: a relative path failed with exit 127 from `ui/`
or a Lambda's folder, and the guard did not run (`tests/sdk/test_agentic_hooks.py`).

### `scripts/guard-destructive.sh`

Blocks `rm -rf` outside cache and scratch paths, `git reset --hard`,
`git checkout .`, `git clean -fd`, `git restore .`, and force-push. Exit 2 with a
reason; `/tmp`, `node_modules`, `__pycache__` and friends pass through.

It exists because a working tree with uncommitted fixes was destroyed by
`rm -rf <tree> && unzip <archive>` mid-review. Reading a rejection is cheap.

---

## Documents that cannot drift

### `tests/sdk/test_claude_md_claims.py`

`CLAUDE.md` is what every session reads and trusts, so a false claim there is
worse than a bug — it is a bug the next session will build on. This proves each
checkable claim against the tree:

- no wildcard imports
- no raw `.Table()` outside `console_api/dal/`
- no `unittest.mock` anywhere (ADR #26)
- no `.module.css`
- components never import `api` directly
- no competing UI component libraries
- TypeScript strict is actually on
- public type-hint coverage is at least the advertised percentage
- the leak guard fails closed outside a git work tree
- every target `make check` depends on exists

Claims needing judgement ("follow existing patterns", "don't build for
hypothetical use cases") are deliberately absent — they belong to review.

**Add a checkable claim to CLAUDE.md, add its assertion here.**

### `tests/sdk/test_context_terms.py`

Every term in `.claude/CONTEXT.md` must still appear in the code; every path `docs/reference/STATE.md`
references must exist. Neither proves a definition is *right*, only that the
vocabulary has not rotted. Terms that are deliberately prose-only carry a written
exemption.

### `.claude/CONTEXT.md` and `docs/reference/STATE.md`

`.claude/CONTEXT.md` is vocabulary only — the terms here that are near-synonyms and not
interchangeable (`run` vs `execution` vs `pipeline_execution_short`; `partition`
vs `granularity`; `measured core` vs total coverage). Confusing them has produced
real bugs.

`docs/reference/STATE.md` is a short page of where things stand: in flight, decided-but-not-
done, measured debt, and the pre-tag smoke reminder. `CHANGELOG.md` is history;
this is the present.

---

## Setup

```bash
chmod +x scripts/verify-changed.sh scripts/guard-destructive.sh

# skills (optional, personal)
mkdir -p ~/.claude/skills
cp -r .claude/skills/shape .claude/skills/implement .claude/skills/break ~/.claude/skills/

# confirm
python3 -m pytest tests/sdk/test_claude_md_claims.py tests/sdk/test_context_terms.py -q
bash scripts/verify-changed.sh
```

---

## What this does not replace

A live smoke before tagging (Principle #15). Every gate here runs against mocks
or a static tree. The lesson from v0.78 stands: a fully green mock suite shipped
a `pipeline_status` vs `status` field-name bug that one live request would have
caught in thirty seconds.
