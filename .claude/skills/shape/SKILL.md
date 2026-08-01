---
name: shape
description: Interrogate a fuzzy idea until it is a set of settled decisions. Use at the start of any backlog item or feature, before a spec exists. Produces docs/work/<slug>/DECISIONS.md. Human answers; agent never decides scope.
---

# Shape — settle the decisions before any code

Turn a fuzzy intention into settled decisions. **You do not write code here.** You interrogate, then record.

## Method

**One question at a time.** Ask, wait, record, ask the next. Never dump a
questionnaire — the maintainer has said plainly that walls of text are useless.

**Answer your own question first.** Before asking anything, check whether the
codebase answers it. `grep`, read the ADR index, read the tests. Only ask what
the repo genuinely cannot tell you. A question you could have answered yourself
is a waste of the one resource this stage spends: the maintainer's attention.

**Always offer a recommendation.** Never "what would you like?" — always "I'd do
X, because Y. Agree?" The maintainer wants direct recommendations, not option
menus. A bare menu is a failure of this stage.

**Resolve dependencies in order.** If decision B only makes sense once A is
settled, ask A first. Say so when you skip ahead.

**Stop when the decisions are settled**, not when you have a design. The design
belongs to stage 2.

## What to interrogate

- **Boundary.** Free or paid? (See CONTRIBUTING.md open-core section.) This decides half the
  design and is the single most expensive thing to get wrong late.
- **Scope.** What is explicitly *not* in this change?
- **Contract.** New/changed DDB fields, API routes, SFN states, enum members?
  Anything crossing a producer↔consumer boundary needs naming now.
- **Vocabulary.** Any term used loosely? Pin it. `run` vs `execution` vs
  `backfill`; `partition` vs `granularity`. Ambiguity here becomes bugs later.
- **Reversibility.** Which decisions are one-way? Those become ADRs.
- **Failure shape.** What does this do when the input is empty, the AWS call
  fails, the row is missing?

## Output

Write `docs/work/<slug>/DECISIONS.md` as you go — not batched at the end:

```markdown
# <feature> — decisions
Status: shaping | settled
## Settled
- **<question>** — <decision>. Rationale: <why>. (one-way: yes/no)
## Vocabulary
- **<term>** — <definition in this project's own words>
## Open
- <question> — blocked on <what>
## Explicitly out of scope
- <thing> — because <reason>
```

For genuinely one-way decisions, draft the ADR now under
`docs/reference/` and reference it. **Sparingly** — an ADR is for a decision that
is hard to reverse, surprising without context, and the result of a real
trade-off. A convention is not an ADR. There are already 81+; most shaping
sessions should add zero.

## Done when

Every question in **Open** is either answered or explicitly deferred with a
reason, and the maintainer says so. Not when you think it is enough.

## Then

Stop. The decisions document is the deliverable. Implementation happens in a
**new session** — say so, and do not start coding here.
