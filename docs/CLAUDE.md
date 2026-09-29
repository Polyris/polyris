# docs/CLAUDE.md — how to write polyris documentation

Read this before editing anything under `docs/`. These rules apply on top of root `CLAUDE.md`; they don't replace it.

## The one ironclad content rule

**OSS docs describe what OSS ships. Silence about everything else.** Full statement: root `CLAUDE.md` Principle #24.

Two violations that sneak in most often:
- **Comparison phrases** — "X is not yet in OSS", "OSS build has no Y", "compared to the paid version" — the comparison itself is the violation, even if X and Y are OSS concepts.
- **Experimental OSS features are OSS.** Say "experimental, API may change" and stop. Don't add "the full version will ship a UI".

## Writing tenets (root Principle #25) — Clear, Concise, Structured

- **Clear:** second person, present tense. "You deploy the pipeline" — not "the pipeline will be deployed". No filler (`simply`, `just`, `easy`, `obviously`).
- **Concise:** one topic per document. Delete anything you can delete without losing information. Every edit is also a bonsai pass — cut adjacent stale content in the same commit.
- **Structured:** most important information first. Headings say what's in the section. Sentence case. Emphasis sparingly (< 10% of text).

## Diátaxis: pick one category per document

| Category | Purpose | This repo lives in |
|---|---|---|
| Tutorial | Learning-oriented, zero context assumed | `README.md#try-it-now` |
| How-to | Goal-oriented, "how do I do X" | `docs/getting-started/QUICKSTART.md`, `docs/deployment/*` |
| Reference | Lookup, technical spec | `docs/features/DSL.md`, `docs/features/DATA_PASSING.md`, `docs/reference/*` |
| Explanation | Understanding-oriented, "why this design" | `docs/architecture/*`, ADRs |

A doc that tries to be two categories becomes bad at both. Common mixing anti-patterns:
- How-to that grows an "and here's the theory" section → move theory to an Explanation doc, link.
- Reference that starts with a tutorial-style walkthrough → walkthrough belongs in `getting-started/`.
- Explanation that includes step-by-step deploy commands → those are how-to, not explanation.

A docstring and `docs/features/DSL.md` both serve Reference — different placement, same category. That's fine. The rule is on purpose, not placement.

## Structure of a doc

1. **Title** — one H1, matches the filename.
2. **Overview** (2–4 sentences) — why this doc exists, who it's for, what they'll be able to do.
3. **Body** — organized by concept, simple case first, edge cases last.
4. **Examples** — runnable, realistic. Not `foo`/`bar`.
5. **Reference** — tables of params/options at the bottom. Optional but preferred.

Docs under `getting-started/` assume zero context. Docs under `reference/` assume the reader knows the concepts. Don't mix.

## Voice

Second person, present tense. `polyris deploys the pipeline`, not `the pipeline will be deployed`. Sentence case for headings. English only (root #10).

## Terminology

Pick one term per concept:
- "task" (not "step", "operation", "activity")
- "pipeline" or "DAG" (prefer "pipeline" in prose; "DAG" when referring to the `DAG(...)` object)
- "orchestrate" (not "provision") — polyris runs existing AWS resources, doesn't create them
- "namespace" (not "prefix", "org")
- "stage" (not "environment")
- "run" or "execution" (interchangeable; use whichever the surrounding UI uses)

## Code examples

- **Runnable** — if a reader copies the block, it should execute. No pseudo-code without a `# pseudo-code` label.
- **Realistic values** — `"my-etl-job"`, not `"foo"`. Real ARNs, not `arn:...:my-stack`.
- **Explicit imports** on any block that stands on its own.
- **No `# TODO`** in shipped examples.

Prefer the minimal version first, then add parameters incrementally.

## Callouts

Use markdown blockquotes sparingly:
- `> **Warning:**` — irreversible or destructive
- `> **Note:**` — important non-obvious context
- `> **Tip:**` — optional ergonomics improvement

## Cross-references

Link, don't restate. Use relative paths: `[CONFIGURATION.md](../reference/CONFIGURATION.md)`. Never hardcode `https://github.com/...` URLs to files in this repo. Every `[text](path)` must resolve.

## Delete dead documentation

A silently wrong doc is worse than no doc. Delete when:
- Content describes a code path that no longer exists
- Content advertises a feature that never shipped or was removed
- A tutorial or how-to references commands / files / URLs that don't resolve
- A design doc / spike / plan is being read as current reference

Default to deletion when migrating — git preserves it. Don't add a `Deprecated:` banner; delete. Don't leave stubs that point to old versions.

## Historical vs current docs — mark them, or delete them

`docs/reference/` contains both current reference and historical dev notes (`SPIKE_*.md`, `PLAN_*.md`, `COMPLETENESS_REPORT_*.md`). A reader can't tell from the filename.

**Rule:** every historical/frozen doc must open with a status line:
- **Frozen-and-current:** `**Historical analysis @ v0.94. Behaviour changed in ADR #117 (see \`DSL.md\` for the current trigger rules).**`
- **Frozen-and-superseded:** `**Superseded — this analysis pre-dates ADR #117. Do not use as reference. Kept for git-history readability only.**`

Docs without one of these headers are treated as current reference by the next reader.

## What's a "doc" for these rules

Everything under `docs/`, plus: `README.md`, `CHANGELOG.md`, docstrings on public Python API, user-facing strings in the UI (`ui/src/`), comments in `polyris-init`-generated templates, error messages. If it's read by a user, every rule here applies.

## Before you commit

- Run every example or command the doc shows against the current codebase.
- Check all links resolve (`[text](path)`).
- Grep for stale terminology if you renamed anything.
- Re-read your edits with "does OSS ship this?" in mind (Principle #24).
- Check for comparison phrases ("not yet in OSS", "coming later", "available in", "the full version").
- Render ASCII trees / diagrams / tables in a Markdown preview — broken box-drawing passes every lint but renders as garbage.

## Common mistakes

- **"Y will be a paid feature."** No. Delete it.
- **"The docstring mentions a parameter only paid uses."** No. OSS docstring describes what OSS users can do.
- **"I'll write a stub `docs/features/Z.md` that says 'planned'."** No. A stub is an advertisement.
- **"I'll leave `foo`/`bar` in the example."** No. The reader copies verbatim, gets an error, loses faith.
- **"I'll use 'workflow' here and 'pipeline' there."** No. Two words = two concepts to the reader.

## Back-compat aliases are documented once, at the point of the alias

When a symbol is renamed, document the alias in **exactly one place**: the back-compat paragraph next to the new symbol's reference entry. Every other doc uses the canonical name and doesn't re-explain the alias.

The alias docstring carries the deprecation note (visible in IDE autocomplete). Tutorials, how-tos, ADRs — none of them mention the alias unless showing a migration path in a dedicated Migration section.

When reviewing a doc PR: `grep` for the alias name across the docs tree — every hit outside the canonical reference entry and a Migration section is a candidate for deletion.
