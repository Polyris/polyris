---
name: documentation
description: Write or edit docs so a reader can succeed in under a minute — plain prose, one topic per doc, honest gaps. Use when writing/editing anything under docs/, READMEs, docstrings, error messages, or user-facing text. MANDATORY first action on invocation is to Read `docs/CLAUDE.md` + root `CLAUDE.md` — this skill is a thin operational wrapper over those content-policy files and refuses to write docs from memory.
---

# Documentation — write for the reader who lands cold

The rules below are **operational** — how to apply the repo's existing doc
policy to a specific doc-writing task. The **content** rules live elsewhere
and this skill deliberately does not duplicate them.

## Step 0 — Load the rules (MANDATORY, no exceptions)

Before typing a single line of doc, run these Read calls. This is not a
suggestion; it is the first action of the skill. Skip it and the doc will
violate rules you didn't know existed.

1. `Read('docs/CLAUDE.md')` — full content policy: Diátaxis application,
   the four categories, terminology, voice, structure, historical-vs-current
   marking, dead-doc removal, cross-reference format, code-example rules.
2. `Read('CLAUDE.md')` — root principles. The four that apply to every doc:
   - **#9** — docs ship in the same commit as the code they describe.
   - **#10** — English only.
   - **#24** — OSS docs describe what OSS ships. Silence about the rest.
   - **#25** — Clear, Concise, Structured + Diátaxis category per doc.

If you're editing a doc that already exists, ALSO:
3. `Read('<path to the doc you're editing>')` — the whole file, not just the
   section. You need to see what else in the doc your change contradicts.
4. `Bash('ls docs/<sibling-directory>/')` and open at least one sibling doc
   in the same directory — you need to see whether your edit contradicts a
   neighbour.

If you cannot Read these files (offline, sandbox restrictions, etc.), STOP
and report that to the maintainer. Do not attempt to write docs from memory
of the policy.

## Before you type a word

Answer these five questions in your head, in order. Skip any and the doc
will drift:

1. **Who is the reader?** Newcomer, migrator, on-call operator, integrator?
   The audience decides vocabulary, prerequisites, length.
2. **What Diátaxis category?** Tutorial / How-to / Reference / Explanation.
   Only one. If you can't pick, the doc is trying to be two — split.
3. **Where does it live?** `docs/getting-started/` (learn), `docs/features/`
   (reference), `docs/deployment/` (how-to), `docs/architecture/` +
   ADR (explain), `docs/reference/` (lookup). Filename says the topic, not
   the category.
4. **What existing doc covers this?** `grep -r "<key term>" docs/` — if any
   result exists, extend it, don't fork.
5. **Would a reader in 12 months find this?** If the current stale-doc
   collection makes it hard to find, that's your fault too: fix the
   navigation in the parent index in the same commit.

## Hard rules

1. **English only** — code, comments, docs, CHANGELOG, ADRs, PR bodies.
   No exceptions (root #10).
2. **Docs ship in the same commit as the code they describe.** Not "in a
   follow-up PR", not "in the same PR but separate commit". Same commit
   (root #9).
3. **One topic per document.** Two topics = two docs, cross-linked.
4. **Delete adjacent stale content in the same commit.** Every doc edit is
   also a chance to prune neighbouring lies. `docs/CLAUDE.md` calls this
   the "bonsai" habit.
5. **Every claim has an example, or the claim comes out.** "Fast" without
   a number is filler. "Simple to configure" without a config example is
   sales copy.
6. **No hard-wrapping paragraphs in Markdown.** GitHub soft-wraps on its
   own — manual `\n` at 80 chars renders as forced breaks. One paragraph =
   one line in source. Same for PR bodies (see `pr-description` skill).
7. **OSS silence about non-OSS features** (root #24). No "coming in paid",
   no "compared to the full version". Silence.

## Anti-patterns LLMs specifically fall into

These are patterns I catch myself producing. Delete on sight:

- **Marketing verbs**: `hardening`, `comprehensive`, `robust`, `seamless`,
  `powerful`, `elegant`, `thoughtful`, `carefully-designed`. Every one is a
  self-assessment the reader didn't ask for. State facts.
- **Adjective piling**: "a fast, simple, robust, comprehensive API". Cut
  every adjective; if the sentence still reads, they were all filler.
- **Filler adverbs**: `simply`, `just`, `easily`, `obviously`, `basically`,
  `essentially`, `of course`. If it were simple, the reader would notice.
- **Corp voice**: "This document introduces…", "The following section
  covers…", "As mentioned above…". Talk to the reader like a colleague.
- **Restating structure**: "This section will cover A, B, C. First A: …"
  — cut the announcement; write A directly.
- **False dichotomies for drama**: "Traditionally, doing X was painful.
  Now, with polyris, it's easy." Nobody reads for the narrative arc.
- **Over-precise numbers where the range is what matters**: "This takes
  1.234 seconds" when what you mean is "under a second".

## Structure — a spine that survives 12 months

Adapt to the Diátaxis category, but every doc's first ~10 lines answer:

1. **Purpose** — one sentence: what this doc is for and who reads it.
2. **Prerequisites / Requires** — versions, IAM, adjacent knowledge.
3. **The single most important thing** — for a how-to, the actual
   commands. For a reference, the API signature. For a tutorial, the
   "you'll build X". Not a table of contents; the answer.

Only then: sections, tables, longer prose.

Long docs (> 200 lines) get a TOC under the purpose sentence.

## Runnable examples

Every code block that a reader might copy must actually run against the
current code. On every doc edit:

- `grep -rn '<function name>' polyris/` — if a symbol in the doc no longer
  exists in code, it's a lie. Fix or delete.
- If the doc shows a shell command, run it locally on a fresh checkout.
  If it shows a Python snippet, `python -c '<snippet>'` it or import into
  a scratch file.
- CHANGELOG entries and docstring examples count as "docs" for this rule.

## Cross-links

Prefer relative links from `docs/`. Never bare URLs to internal repo
files — those break on renames. Format: `[human label](../features/DSL.md#section)`.

## When editing an existing doc

The write-mostly path (new doc) is well-trodden. The edit path is where
most rot happens. Every time you edit an existing doc, run this pass:

1. **Grep for the symbols the doc mentions** — do they still exist under
   those names in code?
2. **Read the whole doc, not just the section you're editing** — is
   anything else in it now inconsistent with your change?
3. **Look at the sibling docs in the same directory** — did you just
   contradict one? If so, one of you is wrong.
4. **Delete anything the edit made stale** — comments about the "old"
   behavior, feature flags removed months ago, deprecation warnings for
   deleted features.

## Voice

- Second person, present tense. "You deploy the pipeline" — not "the
  pipeline will be deployed".
- Active. "Polyris compiles the DAG" — not "the DAG is compiled by polyris".
- One idea per sentence. If a sentence has two `and`s, split it.
- Contractions are fine ("don't", "you'll"). This is documentation, not a
  legal filing.

## Before you commit

- [ ] Step 0 done: `docs/CLAUDE.md` + root `CLAUDE.md` actually Read this
      session, not "remembered from a previous conversation".
- [ ] Doc's Diátaxis category is one of the four, not "mixed".
- [ ] First 10 lines answer purpose + prerequisites + the main thing.
- [ ] Every code block runs today, verbatim.
- [ ] Every symbol / path / version referenced actually exists.
- [ ] No hard-wrapping in Markdown source.
- [ ] No marketing verbs / adjective piling / filler adverbs.
- [ ] Adjacent stale docs in the same directory: pruned or cross-linked.
- [ ] English throughout.
- [ ] If the doc mentions a non-OSS feature by name — delete the mention.
- [ ] Same-commit as the code change that made it necessary (root #9).

## Done when

- A reader who hasn't seen this codebase can complete the doc's stated
  purpose in under a minute (Tutorial: read; Reference: find their answer;
  How-to: run the commands; Explanation: leave with the right mental model).
- Every claim has a runnable example or a specific number.
- The doc reads like one engineer talking to another — no press-release
  seams.

## Further reading

- `docs/CLAUDE.md` — the content policy for this repo (Diátaxis application,
  terminology, voice, structure, historical-vs-current, dead-doc removal).
- Root `CLAUDE.md` #9, #10, #24, #25 — the hard rules docs must obey.
- [GitHub: Documentation done right](https://github.blog/developer-skills/documentation-done-right-a-developers-guide/)
  — origin of Clear/Concise/Structured.
- [Diátaxis framework](https://diataxis.fr/) — origin of the four
  categories.
- [Google technical writing style guide](https://developers.google.com/tech-writing)
  — vocabulary, sentence-level tips.
