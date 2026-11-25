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
│   └── decisions/
│       ├── README.md                    ← ADR index (Principle #29)
│       └── adr-N-slug.md                ← one file per ADR
└── polyris/CLAUDE.md                    ← SDK-specific rules
```

Create files lazily: only when you have something to write. `GLOSSARY.md` is created when the first term resolves. An ADR file is created when a decision is recorded.

## During the session

### Challenge against existing docs first

Before asking any question, read `CLAUDE.md`, `polyris/CLAUDE.md` and `docs/decisions/README.md`, the ADR index; then the ADRs of the area under discussion, section by section (find one with `grep -n '^#\+ k\.'`). A decision already settled there is a constraint, not a question.

### Challenge against the glossary

When the user uses a term that conflicts with existing language in `GLOSSARY.md`, call it out immediately: "Your glossary defines 'cancellation' as X, but you seem to mean Y. Which is it?"

### Sharpen fuzzy language

When the user uses vague or overloaded terms, propose a precise canonical term. "You're saying 'execution': do you mean `pipeline_execution` (the run of a whole pipeline) or `run_task_arn` (one task's `run_task` execution)?"

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

The rules are in `docs/CLAUDE.md`, *Design decisions*: one file, `docs/decisions/adr-N-<slug>.md`, N the highest number + 1. A decision that governs only the paid overlay is written in `polyris-ee/docs/decisions/`, numbered from 1001, never here.

```md
---
area: engine
accepted: 2026-11-25
---
# ADR #N — The decision, stated in at most 120 characters

## Context

{Short: what forced the decision.}

## Decision

### 1. {A rule}

{The rule as it holds, with the test that pins it.}

## Consequences

{Non-obvious downstream effects.}

## Alternatives

- Option B — rejected because Y
```

After adding: run `python scripts/adr_index.py` (Principle #29).
