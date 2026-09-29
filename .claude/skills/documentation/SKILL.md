---
name: documentation
description: Write or edit docs so a reader can succeed in under a minute — plain prose, one topic per doc, honest gaps. Use when writing/editing anything under docs/, READMEs, docstrings, error messages, or user-facing text. MANDATORY first action on invocation is to Read `docs/CLAUDE.md` + root `CLAUDE.md` — this skill is a thin operational wrapper over those content-policy files and refuses to write docs from memory.
---

# Documentation — write for the reader who lands cold

## Step 0 — Load the rules (MANDATORY, no exceptions)

Before typing a single line of doc, run these reads. Skip them and the doc will violate rules you didn't know existed.

1. `Read('docs/CLAUDE.md')` — Diátaxis application, terminology, voice, structure, historical marking, dead-doc removal, cross-reference format.
2. `Read('CLAUDE.md')` — root principles, especially: **#9** (docs same commit), **#10** (English), **#24** (OSS silence), **#25** (Clear/Concise/Structured + Diátaxis).

If editing an existing doc, ALSO:
3. `Read('<path to the doc>')` — the whole file, not just the section you're changing.
4. `Bash('ls docs/<sibling-directory>/')` and read at least one sibling — does your change contradict a neighbour?

If you cannot Read these files, STOP and report. Never write docs from memory of the policy.

## Before you type a word

Answer these in order:
1. **Who is the reader?** Newcomer, migrator, on-call operator, integrator?
2. **What Diátaxis category?** Tutorial / How-to / Reference / Explanation — only one. Can't pick? The doc is trying to be two — split.
3. **Where does it live?** `docs/getting-started/` (learn), `docs/features/` (reference), `docs/deployment/` (how-to), `docs/architecture/` + ADR (explain).
4. **What existing doc covers this?** `grep -r "<key term>" docs/` — if anything exists, extend it, don't fork.
5. **Would a reader in 12 months find this?** Fix the navigation in the parent index in the same commit if needed.

## Hard rules

1. **English only** — no exceptions (root #10).
2. **Same commit as the code** — not "in a follow-up" (root #9).
3. **One topic per document** — two topics = two docs, cross-linked.
4. **Delete adjacent stale content** — every edit is a bonsai pass.
5. **Every claim has an example, or it comes out** — "Fast" without a number is filler.
6. **No hard-wrapping paragraphs in Markdown** — GitHub soft-wraps. One paragraph = one line in source.
7. **OSS silence** — no "coming in paid", no "compared to the full version" (root #24).

## Anti-patterns LLMs specifically fall into

Delete on sight:
- **Marketing verbs**: `hardening`, `comprehensive`, `robust`, `seamless`, `powerful`, `elegant`, `thoughtful`. Self-assessments the reader didn't ask for. State facts.
- **Adjective piling**: "a fast, simple, robust, comprehensive API". Cut every adjective — if the sentence still reads, they were all filler.
- **Filler adverbs**: `simply`, `just`, `easily`, `obviously`, `basically`, `essentially`, `of course`.
- **Corp voice**: "This document introduces…", "The following section covers…", "As mentioned above…". Talk to the reader like a colleague.
- **Restating structure**: "This section will cover A, B, C. First A: …" — cut the announcement; write A directly.
- **Apologetic TOCs**: a TOC lists sections; it doesn't tell readers what to skip. If sections need "skip" labels, the doc is mis-ordered — put the most-read section first.
- **False dichotomies for drama**: "Traditionally doing X was painful. Now it's easy." Nobody reads for the narrative arc.

## When editing an existing doc

1. Grep for every symbol the doc mentions — do they still exist in code?
2. Read the whole doc, not just your section — is anything else now inconsistent?
3. Look at sibling docs in the same directory — did you just contradict one?
4. Delete anything the edit made stale.

## Runnable examples

Every code block a reader might copy must run against current code:
- `grep -rn '<function name>' polyris/` — if a symbol no longer exists, fix or delete.
- Shell commands: run locally. Python snippets: `python -c '<snippet>'`.
- CHANGELOG entries and docstring examples count as "docs" for this rule.

## Semantic self-review (LLM-native check)

Mechanical gates (pytest / lychee / Vale) catch drift detectable by regex and file existence. They don't catch semantic problems. Before commit, read the whole doc top-to-bottom and check:

1. **Internal contradictions** — does any claim disagree with another in the same doc? ("Section 2 says async; Section 5 shows `sync_only=True`.")
2. **Doc↔doc contradictions** — read at least one sibling. Does your edit contradict it? One of you is wrong — fix in same commit.
3. **Behavior claims not covered by mechanical checks:**
   - Latency claims ("runs in <1s") — verify with a benchmark or delete.
   - Response-shape claims ("returns 200 on success") — grep the handler.
   - Timing/ordering ("this fires before X") — read the code, don't guess.
4. **Deprecated / removed features still cited** — search for symbols you know were removed.

## Before you commit

- [ ] Step 0 done: `docs/CLAUDE.md` + root `CLAUDE.md` actually Read this session.
- [ ] Diátaxis category is one of the four, not "mixed".
- [ ] First 10 lines answer purpose + prerequisites + the main thing.
- [ ] Every code block runs today, verbatim.
- [ ] Every symbol / path / version referenced actually exists.
- [ ] No hard-wrapping in Markdown source.
- [ ] No marketing verbs / adjective piling / filler adverbs.
- [ ] Adjacent stale docs: pruned or cross-linked.
- [ ] No non-OSS features mentioned.
- [ ] Same-commit as the code change (root #9).
- [ ] **`python3 -m pytest tests/docs/ -q` — 0 failures** (cli flags, entry points, ADR refs, resource counts, PyPI installs).
- [ ] **`lychee --config .config/lychee.toml --offline './**/*.md'` — 0 errors** (relative links + heading anchors).
- [ ] **`vale --config .config/vale.ini docs/ README.md polyris/ sam/lambdas/` — 0 warnings** (filler adverbs, marketing verbs, corp voice, sentences over 40 words).
- [ ] **Semantic self-review** — internal contradictions, behavior claims, removed features.

## The mechanical gates

### `tests/docs/` — pytest

- `test_cli_flags.py` — every `polyris-<cmd> --flag` in docs must exist as `add_argument("--flag")` in `polyris/*.py`.
- `test_entry_points.py` — every `polyris-<cmd>` in docs must be in `pyproject.toml [project.scripts]`.
- `test_adr_refs.py` — every `ADR #N` / `adr-N-slug` must resolve to a file or heading in `DESIGN_DECISIONS.md`.
- `test_resource_counts.py` — prose claims like "8 Lambda functions" must match `sam/template.yaml` counts.
- `test_pypi_installs.py` — bare `pip install polyris` rejected; use `polyris @ git+…@<VERSION>`.

Suppression: whole file → add glob to `DEFAULT_SKIP_GLOBS` in `tests/docs/_helpers.py`. Single line → append `<!-- audit-docs: skip-line -->`. Prefer fixing the doc over suppression.

### `lychee` — link checker

```bash
lychee --config .config/lychee.toml './**/*.md'           # online
lychee --config .config/lychee.toml --offline './**/*.md' # local only
```

### `vale` — prose style linter

Four rules: `MarketingVerbs.yml`, `FillerAdverbs.yml` (sentence-start only), `CorpVoice.yml`, `SentenceLength.yml` (>40 words). Scope: `docs/ README.md polyris/ sam/lambdas/`. Vale sees only docstrings and `#` comments in Python.

```bash
vale --config .config/vale.ini docs/ README.md polyris/ sam/lambdas/
vale --config .config/vale.ini docs/features/DSL.md   # single file
```

When adding a new rule: create `.config/vale/styles/PolyrisDocs/<Name>.yml`, run vale against all docs, fix or suppress every hit before merging.

## Done when

- A reader who hasn't seen this codebase can complete the doc's stated purpose in under a minute.
- Every claim has a runnable example or a specific number.
- The doc reads like one engineer talking to another.
