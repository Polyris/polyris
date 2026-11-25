---
area: docs
accepted: 2026-11-25
---
# ADR #141 — Decision records: one file per ADR, in force only, each section citable

## Context

Code, tests, CLAUDE.md files and skills cite ADRs as rules, by number and section (`ADR #137 §1` alone more than a hundred times). The records had grown to about 960 KB in two forms: a 480 KB monolith of 120 inline ADRs and 28 standalone files, with a hand-kept index that missed about 30 numbers. Superseded decisions, change records, revision logs, review transcripts and probe logs sat beside the rules in force, six numbers named two decisions, ADR #103 was cited 71 times and had no text, and no check read the citations outside `docs/`. An agent could not read the monolith in one call, and a grep landed in superseded text with no marker.

## Decision

### 1. One file per ADR in force

Each ADR is `docs/decisions/adr-N-<slug>.md`, with YAML front matter (`area`, `accepted`, and `revised`, `supersedes`, `amends` when they apply) and the H1 `# ADR #N — <the decision>`. `docs/decisions/README.md` is the index: its table is generated from the files (`scripts/adr_index.py`), and its *Retired* table is kept by hand. `docs/CLAUDE.md`, *Design decisions*, is the writing rule.

### 2. Retired ADRs leave the tree

A superseded ADR, or a change record filed as one, has no file. Its row under *Retired* names the successor or the reason, and a `git show v1.3.2:<path> | sed -n 'a,bp'` command that prints exactly its old text from an immutable release tag. A replaced section that something cites keeps its heading as a one-line pointer to a live section; one that nothing cites is deleted.

### 3. A decision lives in the repository that ships what it governs

An ADR that governs only the overlay in `polyris-ee` is written in `polyris-ee/docs/decisions/`, numbered from 1001, and is neither stored, indexed nor cited here (root `CLAUDE.md` Principle #24). One that governs code here stays here, even when only the overlay cites it, and the code it governs cites it. The overlay's `scripts/check_adrs.py` reuses this repository's resolver to check its citations and that no number has a file in both.

### 4. Every citation resolves

Tests in `tests/docs/` read every tracked text file (in `CHANGELOG.md`, its *Unreleased* section): every cited number names a file or a retired row (`test_adr_refs.py`); every cited section, list, range, `§Name` and `*Name*` names a heading of that ADR, a bare `§k` one of an ADR the same file cites, and a pointer a live section (`test_adr_sections.py`); the index matches the files and every retired command prints its ADR's heading (`test_adr_index.py`); CLAUDE.md files, skills and the CI reviewer prompt cite no retired ADR; the numbers 65, 94, 106, 107 and 108, which name two decisions each, are cited with their slug word ("ADR #65, view sync"), and no other number may collide.

### 5. Limits

20 KB per file, 1,000 characters per line, 300 per front-matter field, 120 per title (`test_adr_limits.py`). The files over 20 KB are listed with a cap at their size, which goes down with the file and up only by the maintainer's decision.

## Consequences

- The records went from about 960 KB to about 750 KB, and the largest file from 480 KB to 128 KB (ADR #137). The era specs of ADRs 42–106 lost only their process sections; their prose is the next thing to cut.
- A citation reaches its section in two calls: a grep for the heading, a read of that section.
- Nothing in `docs/reference/` is an ADR any more; user docs explain a reason in a sentence instead of linking an ADR.

## Alternatives

- **One monolith, slimmed:** it keeps one hot file, invisible number collisions and a read an agent cannot make in one call.
- **An archive folder of retired ADRs:** the same weight and grep noise under another name; the release tag keeps the text.
- **Stub files for retired numbers:** `docs/CLAUDE.md` forbids stubs, and the index row with its command covers the lookup.
- **Renumbering the colliding ADRs:** thousands of citations across two repositories would need checking; the slug word disambiguates.
- **A separate numbering for the overlay, such as `adr-ee-N`:** a second citation form; numbers from 1001 keep one form and never meet this repository's.
- **A size cap per section:** it is met by adding headings, so it measures heading density; the failure to stop was a file growing by history.
