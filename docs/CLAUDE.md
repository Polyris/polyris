# docs/CLAUDE.md — how to write polyris documentation

Read this before editing anything under `docs/`. These rules apply on top of the
root `CLAUDE.md`; they don't replace it. They exist so every doc reads as if
one person wrote it — consistency saves the reader's time.

## The one ironclad content rule

**OSS docs describe what OSS ships. Silence about everything else.** Full
statement lives in root `CLAUDE.md` Principle #24 — read it before you edit.
Everything below is *style*; #24 is *content*.

Two consequences of #24 that catch people out most often:

- **Comparison phrases are the frequent violation**, not specific feature
  names. Anything of the shape "X is not yet in OSS", "OSS build has no Y",
  "compared to the paid version", "coming to OSS later" — the comparison
  itself is the violation, even if X and Y are OSS-shipped concepts.
- **Experimental OSS features are OSS.** Document them normally. Say
  "experimental, API may change" and stop there — do not add a "the full
  version will ship a UI" tease.

## Three writing tenets (root Principle #25) — Clear, Concise, Structured

Every doc, docstring, code comment, error message, and UI string in this repo
ships against these three tenets. Named, so a review can point at "not
concise enough" instead of arguing style from scratch. The three tenets are
GitHub's, from [Documentation done right](https://github.blog/developer-skills/documentation-done-right-a-developers-guide/).

### Clear

- Plain language for the intended audience (see Diátaxis below — the
  audience differs per category).
- **Define or replace every acronym / jargon term the first time it appears
  in a given doc**, unless the doc's category is Reference for experts and
  the vocabulary is table-stakes for that audience.
- Second person, present tense. `you deploy the pipeline`, not `the pipeline
  will be deployed by the user`.
- No filler (`simply`, `just`, `easy`, `obviously`, `of course`). If it's
  easy, the reader will notice.

### Concise

- **Document what a reader needs to succeed, not every edge case.** Edge
  cases live in a separate "gotchas" or "troubleshooting" section —
  cross-linked, not inlined.
- **One topic per document.** If two topics share a doc, the reader has to
  read past the one they don't care about. Split.
- Prefer showing over telling: one runnable snippet beats three paragraphs
  of prose. If a claim has no example, ask whether you actually need the
  claim.
- Delete anything you can delete without losing information.
- **Docs are bonsai, not statues.** Trimming isn't a one-off act at
  write-time; it's ongoing. Every time you edit an existing doc, ask
  whether some part of the surrounding content is now stale, redundant, or
  outdated — and cut it in the same commit. A collection of small, fresh,
  accurate docs beats a large collection in disrepair.

### Structured

- **Most important information first.** The reader lands, gets their answer
  in the first paragraph / first table / first code block, and reads on
  only if they want detail. Anti-example: a "Cheat sheet" section that
  lives at the *bottom* of the doc.
- **Headings that a scanner can navigate.** Section titles say what's in
  the section, not `Introduction` / `Details` / `More info`.
- **Consistent styling.** Sentence case for headings. `code` for
  identifiers. `**bold**` only for the single most important term in a
  paragraph.
- **Emphasis is scarce.** Aim for < 10% of the text being bold or in a
  list. If everything is emphasised, nothing is.
- Longer docs (say, > 200 lines) get a table of contents at the top — a
  `##` list of sections the reader can jump to.

## Diátaxis: pick one category per document

Every doc fits **exactly one** of these four purposes. A doc that tries to
be two becomes bad at both. When you land on a `docs/` file to edit it,
first name its category; if you catch yourself drifting into another,
that's the signal to split.

| Category         | Purpose                                                              | This repo lives in                                        |
| ---------------- | -------------------------------------------------------------------- | --------------------------------------------------------- |
| **Tutorial**     | Learning-oriented — walks a reader through *doing* something for the first time. Assumes zero context. | `docs/getting-started/TUTORIAL.md`                        |
| **How-to guide** | Goal-oriented — "how do I do X", assumes the reader knows what X is and why they want it. | `docs/getting-started/QUICKSTART.md`, `docs/deployment/*` |
| **Reference**    | Lookup — technical specification, tables of parameters, exact contracts. Assumes the reader knows the concepts. | `docs/features/DSL.md`, `docs/features/DATA_PASSING.md`, `docs/reference/*` |
| **Explanation**  | Understanding-oriented — "why this design", background, rationale. Not step-by-step; not lookup. | `docs/architecture/*`, ADRs (`docs/reference/adr-*.md`)   |

**Common mixing anti-patterns to catch on review:**

- A **how-to** that grows an "and here's the theory" section → move the
  theory into an Explanation doc, link from the how-to.
- A **reference** that starts with a tutorial-style walkthrough → the
  walkthrough belongs in `getting-started/`, the reference stays cold.
- A **tutorial** that reference-dumps every option along the way → the
  reader loses the flow. Pick one path, defer the options to Reference.
- An **explanation** doc that includes step-by-step deploy commands →
  those commands are how-to; the explanation just describes the why.

### Diátaxis is the "why" axis — placement is a separate axis

Every doc has two orthogonal attributes:

- **Where it lives** — inline code comments, docstrings, `README.md`,
  `docs/`, ADRs, external systems. This is a placement continuum, not a
  rule — the filesystem declares the answer.
- **What purpose it serves** — Tutorial / How-to / Reference / Explanation
  (Diátaxis, above). This IS a rule: one purpose per document.

A docstring on `@task.sfn` and `docs/features/DSL.md` both serve **Reference**
purpose — they live in different places on the placement axis. That's fine.
The rule is on the purpose axis: don't make either of them try to be a
tutorial *and* a reference.

The sections below are the concrete rules the three tenets and Diátaxis
imply for this repo.

## Structure of a doc

Every doc under `docs/` should have this shape:

1. **Title** — one H1, matches the filename.
2. **Overview** (2–4 sentences) — why this doc exists, who it's for, what
   they'll be able to do after reading it.
3. **Body** — organized by concept, in the order a reader encounters them.
   Simple case first, edge cases last.
4. **Examples** — runnable, realistic. Not `foo`/`bar`.
5. **Reference** — tables of params/options for scan-back. Optional but
   preferred at the bottom of user-facing docs.

Docs under `getting-started/` assume zero context (fresh reader, blank AWS
account). Docs under `reference/` assume the reader knows the concepts and
needs the exact contract. Do not mix — a reference doc that reads like a
tutorial is neither.

## Voice

- **Second person, present tense.** "You define a DAG with `DAG(...)`."
  Not "The user may define a DAG..." or "A DAG will be defined...".
- **Direct.** State what the tool does. `polyris deploys the pipeline`,
  not `polyris will attempt to deploy the pipeline`.
- **No filler.** Drop "simply", "just", "easy", "obviously", "of course".
  If a step is easy, it's easy; the reader will notice.
- **English only** (root Principle #10).
- **Sentence case for headings** (`## Task parameters`, not
  `## Task Parameters`), consistent with the sections that already exist.

## Terminology

Pick one term per concept and use it everywhere. When two words compete:

- "task" (not "step", "operation", "activity") — the polyris DSL primitive
- "pipeline" or "DAG" (interchangeable in this codebase; prefer "pipeline"
  in user-facing prose, "DAG" when referring to `DAG(...)` the DSL object)
- "orchestrate" (not "provision") — polyris runs existing AWS resources; it
  does not create them
- "namespace" (not "prefix", "org") — the deployment-time resource-name
  prefix
- "stage" (not "environment") — the deployment tier (dev / prod / …)
- "run" or "execution" (interchangeable; use whichever the surrounding UI
  uses)

Define a term the first time it appears in a given doc, then use it
consistently. Don't switch styles mid-doc.

## Code examples

- **Runnable.** If a reader copies the block, it should parse and (where
  applicable) execute. No pseudo-code without a `# pseudo-code` label.
- **Realistic values, not placeholders.** `"my-etl-job"`, not `"foo"`.
  `arn:aws:states:us-east-1:123456789012:stateMachine:my-workflow`, not
  `arn:...:my-stack`.
- **Explicit imports** on any block that stands on its own.
- **Match the surrounding style** — 4-space indent for Python, 2-space for
  YAML/JSON, LF line endings.
- **No `# TODO`** in shipped examples. Remove or replace before commit.

Prefer showing the **minimal** version first, then adding parameters
incrementally in follow-up examples.

## Tables vs prose

- **Prose** for narrative — the "why", "how", "when to use this".
- **Tables** for reference — parameters, options, comparisons, decision
  matrices. Anything a reader will scan back to later.
- Don't write a paragraph that's really a table. If the content is
  parallel structure with the same shape per item, use a table.

## Callouts

Use markdown blockquotes for callouts, and use them sparingly:

- `> **Warning:**` — irreversible or destructive; the reader must pay
  attention or something bad happens.
- `> **Note:**` — important context that isn't obvious from the surrounding
  prose (edge case, hidden coupling, known limitation).
- `> **Tip:**` — optional performance / ergonomics improvement.

If every third paragraph is a callout, they stop working. Delete the ones
that are just prose in disguise.

## Cross-references

- **Link, don't restate.** If a concept is defined in `CONFIGURATION.md`,
  link to it from `QUICKSTART.md` rather than duplicating the definition.
  When the definition changes, one edit — not five.
- Use relative paths from the repo root:
  `[CONFIGURATION.md](../reference/CONFIGURATION.md)`. Don't hardcode
  `https://github.com/...` URLs to files in this same repo.
- Every `[text](path)` must resolve. Dead links are user-facing bugs.

## Delete dead documentation

A doc that's silently wrong is worse than no doc — it misinforms, slows people
down, and demoralises the next engineer who has to reconcile it with reality.
Deleting dead docs is a duty, not an optional cleanup.

**When to delete:**

- Content describes a code path that no longer exists, or a behaviour that
  changed and the doc didn't.
- Content advertises a feature that never shipped, was removed, or moved to
  a different tier (this last case is a #24 violation too).
- A tutorial or how-to references commands / files / URLs that don't
  resolve.
- A design doc / spike / plan is being *read as current reference* by
  people who don't know it's frozen at some past state (see next section).

**How to delete:**

- **Default to deletion when migrating.** If a doc might still be useful
  and you're not sure, delete it — git preserves it. If it turns out to
  matter, it's one `git revert` away.
- **Incremental scan.** Delete what's clearly wrong first; skip anything
  ambiguous rather than agonising over borderline cases. The borderline
  cases can wait for their next natural edit.
- Removing a doc doesn't need its own PR — fold the delete into whichever
  code change made it stale, so the code and its docs stay in step
  (root Principle #9).

**What NOT to do:**

- Don't leave a stub that says "see the old version of this doc for
  details". If the details still matter, they belong in the current doc;
  if they don't, the pointer just tells readers to go read out-of-date
  material.
- Don't add a `Deprecated:` banner in place of deleting. Banners rot too.
  Either the content earns its place or it goes.

## Historical vs current docs — mark them, or delete them

`docs/reference/` in this repo contains a mix of **current reference**
(`CLI.md`, `CONFIGURATION.md`, ADRs that are load-bearing) and **historical
dev notes** (`SPIKE_*.md`, `PLAN_*.md`, `COMPLETENESS_REPORT_*.md`) that
were written to capture the state of things at a specific point in time.

A new engineer landing on a `SPIKE_TRIGGER_RULES.md` should be able to tell
in the first line whether they're reading the current contract or a
historical analysis. Otherwise a five-month-old spike that noted "we
haven't shipped X yet" becomes a citation for "polyris doesn't ship X" —
long after we did ship X.

**Rule:** every historical/frozen doc must open with an explicit status
line as its first paragraph. Two forms are allowed:

- **Frozen-and-current** — the analysis reflected reality at the time and
  is still useful as history:
  > **Historical analysis @ v0.94. Behaviour changed in ADR #117 (see
  > `DSL.md` for the current trigger rules).**

- **Frozen-and-superseded** — the analysis is now wrong. Prefer deleting
  the doc; if you keep it for reasons, mark it aggressively:
  > **Superseded — this analysis pre-dates ADR #117. Do not use as
  > reference. Kept for git-history readability only.**

Design docs / spikes / plans / completeness reports without one of these
headers are treated as current reference by the next reader, which is how
"half-correct design docs misused as current reference material" happens.

## What's a "doc" for these rules

Everything under `docs/`, plus:

- `README.md` at the repo root
- `CHANGELOG.md`
- Docstrings on public Python API (`polyris/`) — the reader sees these
  through `polyris-init` help text, IDE hover, `pydoc`, and any
  documentation generator
- User-facing strings in the UI (`ui/src/`) — these are docs that ship
  as runtime text
- Comments in `polyris-init`-generated templates (`polyris/init.py`)
- The pipeline template scaffolded by `scripts/setup-polyris.sh`
- Error messages a user could see (Python exceptions, Lambda logs, CLI
  output)

If it's read by a user, it's a doc, and every rule here applies.

## Before you commit

- **Run the examples.** If a doc shows a `polyris-deploy` command, a
  `sam deploy` command, or a Python snippet — the exact form must work
  against the current codebase. Broken docs are Principle #9 violations.
- **Check links.** `[text](path)` — does `path` resolve?
- **Grep for stale terminology.** If you renamed a function or a
  parameter, sweep every doc that mentioned the old name.
- **Sweep for #24 violations you introduced.** Re-read your edits with
  the "does OSS ship this?" test in mind.
- **Sweep for comparison phrases** ("not yet in OSS", "coming later",
  "available in", "the full version") — these bypass a features-list
  check but still violate #24.

## Common mistakes to avoid

- **"Let me note that Y will be a paid feature."** No. Delete the note.
  The paid overlay's docs introduce Y when the user gets there.
- **"The docstring mentions a parameter that only paid uses — let me
  leave it, the code still accepts it."** No. OSS docstring describes
  what OSS users can actually do.
- **"I'll write a stub `docs/features/Z.md` that says 'planned'."** No.
  A stub is an advertisement.
- **"The Help modal shows entries that don't exist in OSS; let me grey
  them out."** No. The Help modal shouldn't produce the entry at all —
  it derives from the active feature surface (ADR #99). Fix the source,
  not the display.
- **"I'll leave `foo`/`bar` in the example; the reader will substitute."**
  No. The reader will copy verbatim, get an unhelpful error, and lose
  faith.
- **"I'll use 'workflow' here and 'pipeline' there, they mean the same
  thing."** No. Pick one. Two words = two concepts, to the reader.
