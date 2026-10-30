---
name: domain-modeling
description: Build and sharpen the project's domain model. Use when discussing codebase terminology, writing or editing GLOSSARY.md, or recording an ADR.
---

# Domain Modeling

Actively build and sharpen the project's domain model as you design. This is the *active* discipline: challenging terms, inventing edge-case scenarios, and writing the glossary and decisions down the moment they crystallise.

## File structure

```
/
├── GLOSSARY.md                          ← project-wide terms
├── docs/
│   └── reference/
│       ├── DESIGN_DECISIONS.md          ← inline ADRs (numbered sections)
│       ├── adr-index.md                 ← ADR index (Principle #29)
│       └── adr-NNN-slug.md             ← standalone ADRs for larger decisions
└── polyris/CLAUDE.md                    ← SDK-specific rules
```

Create files lazily: only when you have something to write. `GLOSSARY.md` is created when the first term resolves. A standalone ADR file is created only when the decision is too large for an inline entry.

## During the session

### Challenge against existing docs first

Before asking any question, read `CLAUDE.md`, `polyris/CLAUDE.md`, and `docs/reference/DESIGN_DECISIONS.md`. A decision already settled there is a constraint, not a question.

### Challenge against the glossary

When the user uses a term that conflicts with existing language in `GLOSSARY.md`, call it out immediately: "Your glossary defines 'cancellation' as X, but you seem to mean Y. Which is it?"

### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical term. "You're saying 'execution': do you mean `pipeline_execution` (the run of a whole pipeline) or `wrapper_execution_arn` (one task's SFN execution)?"

### Discuss concrete scenarios

Stress-test domain relationships with specific scenarios. Invent edge cases that force precision about boundaries between concepts.

### Cross-reference with code

When the user states how something works, check whether the code agrees. Surface contradictions: "Your code does X, but you just said Y. Which is right?"

### Update GLOSSARY.md inline

When a term resolves, update `GLOSSARY.md` right there — don't batch. Format:

```md
# Polyris

One or two sentence description of the project context.

## Language

**pipeline_execution**:
A single dated run of a pipeline, keyed as `{pipeline_name}-run-{date}-{hex8}`.
_Avoid_: run, execution, job

**task_execution**:
The Step Functions execution of a single task within a pipeline_execution.
_Avoid_: task run, step execution
```

`GLOSSARY.md` is vocabulary only — no implementation details, no spec prose, no scratch notes.

### Offer ADRs sparingly

Only offer an ADR when **all three** are true:

1. **Hard to reverse** — meaningful cost to change your mind later
2. **Surprising without context** — a future reader will wonder "why did they do it this way?"
3. **A real trade-off** — genuine alternatives existed and you picked one for specific reasons

If any of the three is missing, skip it.

## ADR format

**Inline** (small decision, add a numbered section to `docs/reference/DESIGN_DECISIONS.md`):

```md
### ADR #N — Short title

One to three sentences: context, decision, why.
```

After adding: update the count line in `docs/reference/adr-index.md` and add a row to the table (Principle #29).

**Standalone** (large decision with rejected alternatives, consequences, or constraints worth preserving):

File: `docs/reference/adr-NNN-slug.md`

```md
# ADR #NNN — Short title of the decision

{1-3 sentences: context, what was decided, why.}

## Considered options

- Option A — chosen because X
- Option B — rejected because Y

## Consequences

{Non-obvious downstream effects, if any.}
```

After adding: update `docs/reference/adr-index.md` count and table (Principle #29).
