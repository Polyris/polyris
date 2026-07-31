# ADR #102 — Repo split (Dagster model): public core repo + private paid repo

> **Status:** ACCEPTED — aligned. Timing: split **now**, before first public
> release (so public history is live from commit one). Supersedes the
> single-repo + snapshot-export approach. Amends ADR #98 (split *structure*)
> and ADR #99 (UI exclusion); the registration seam from ADR #97 and the
> entitlement model from ADR #100/101 are unchanged.

## Context

ADR #98 established open-core as **one private monorepo** with an export script
(`oss-export.sh`) that strips proprietary roots and pushes a **snapshot** to a
public mirror. That model trades away two things the team now considers
important:

1. **Live per-commit history** in the public repo (snapshot produces squashed
   "Sync" commits, not a real commit history).
2. **Seamless external contributions** (a PR opened against the snapshot mirror
   is overwritten by the next sync; accepting it requires manual cherry-pick
   back into the private source).

A snapshot/`filter-repo` model **cannot** provide both at once, because the
public repo is *generated from* the private one — any PR landing in the public
repo conflicts with the next regeneration. The only model that delivers live
history **and** direct contributions **and** keeps paid code private is the
**Dagster model**: develop the open-source core **in a public repo** (the source
of truth), and keep paid code in a **separate private repo** that depends on the
public core as an installed package.

The team has chosen the Dagster model. Hiding paid code matters (a visible `ee/`
would let anyone self-host the paid features), so a single public repo with `ee/`
under a commercial license (the GitLab model) was rejected.

## Decision

Split the monorepo into **two repositories**. The boundary is **what extracts
cleanly**, verified by spikes. The key fact a spike established: the
free-backend ↔ ee dependency is **one-way (ee → free)**, and `main.py` already
makes `ee` optional (`try: import ee … except ImportError: pass`). So the
free-backend extracts cleanly as a standalone unit, and **only `ee` moves to the
private repo**.

- **`polyris` (public)** — a **fully working open-core product** the user can
  self-host (SDK + backend + frontend). Contains: the `polyris` Python **SDK
  core** (DSL→ASL, partitions, granularity, resolver, codegen); the **free UI**;
  the **free backend** (`console_api/` **without** `ee/` — `routes/`, `dal`,
  `auth`, `main`, etc.; `main.py` starts and serves the 16 free routes with no
  `ee` present); and the SSoT (`constants.py` + `polyris.codegen`). Published as
  `polyris` (git tag) + npm `@polyris/ui`.
- **`polyris-ee` (private)** — the paid layer only: the **`ee/` backend plugin**
  (`console_api/ee/` — paid routes that import the free backend), the **paid UI**
  (`ui/src/ee/` → `@polyris/ui-ee`), and the **SDK AI** (`polyris/_ee/`). Depends
  on the public repo. The paid product is built and deployed here, on our infra
  only.

**Why this boundary works for self-host (spike-driven; corrects two earlier
drafts).** This ADR went through two wrong boundaries before this one. Draft A
tried "extract the free backend, have ee pull it from git at build" — needlessly
complex. Draft B over-corrected to "whole backend private" — which **breaks free
self-host**, since the free UI would have no backend to talk to. A spike settled
it: free-backend modules (`routes`, `dal`, `auth`, …) import **nothing** from
`ee` (one-way ee → free), and `main.py` already guards `import ee` with
try/except. Verified by building `console_api` **with `ee/` removed** + core via
pip: `main.ROUTES` = 16, free routes (`pipelines`, `assets`, `tasks`) all live,
starts cleanly with no `ee`. So the free backend is genuinely standalone — the
open-core user gets a working backend **and** frontend, and `ee` is a plugin that
only loads on our SaaS. This is exactly the ADR #98 model, now expressed as a
repo boundary: free console works on its own; `ee` appends paid routes when
present.

**Dependency direction (one-way, enforced):** `polyris-ee` depends on `polyris`;
the public repo never imports proprietary code. The private `ee` backend, when
built into the SaaS Lambda, sits alongside the free backend and pulls the SDK
core via `pip install polyris@tag` (replacing the `console_api/polyris` symlink —
spike-verified: full 58-route assembly imports cleanly).



### Distribution: git tags, not PyPI

The paid repo pulls the core directly from the public GitHub repo by tag — **no
PyPI publish**:

```toml
# polyris-ee/pyproject.toml
dependencies = ["polyris @ git+https://github.com/Polyris/polyris.git@v0.93.0"]
```

Because the core repo is **public**, this needs no token or deploy key. A "core
release" is a `git tag`, not a package upload. Local cross-cutting development
uses `pip install -e ../polyris` (editable) so paid code builds against the live,
unreleased core (this is the primary anti-drift mechanism — see below).

### How the two backends combine at build time

Two build targets, one shared free backend (now living in the public repo):

- **Open-core self-host (the user):** deploys the **public** repo as-is.
  `console_api/` has no `ee/`; `main.py` registers 16 free routes; the free UI
  talks to it. A complete, working product on the user's own AWS. Paid routes
  simply do not exist in this build.
- **SaaS (us):** the private `polyris-ee` build assembles the Lambda from **free
  backend (public repo) + `ee/` (private repo) + SDK core (pip)**. `main.py`'s
  `try: import ee` picks up the paid routes → 58 routes. Because free-backend
  lives in the public repo, the SaaS build pulls it in (git dependency / vendored
  checkout) and overlays `ee/` on top. This is the one place both repos are
  combined, and it only happens on our infra — never shipped to a customer.

The free backend is the shared base; `ee/` is an overlay that only our SaaS build
applies. The earlier worry ("if backend is private, how does the user self-host")
is resolved: the backend the user needs is **public**; only the paid overlay is
private.

## Spike results (verified, not assumed)

Run against the current tree before writing this ADR:

1. **Core is pip-installable and self-contained.** `python -m build --wheel`
   succeeds and produces `polyris-0.91.0-py3-none-any.whl` **even with `ee/` and
   `ui/` removed**. The build-backend is standard setuptools; `git+https://`
   installs will work. ✓
2. **Core boundary is clean.** `import polyris` works with no backend/`ee`
   present; core imports nothing from `sam/`, `console_api`, or `ee`. ✓
3. **Core does not depend on `polyris/_ee`.** Grep confirms no core module
   imports `_ee`; it lifts out cleanly. ✓
4. **Backend→core surface is tiny.** `ee/team/` imports exactly **three** core
   modules: `polyris.partitions`, `polyris.granularity`,
   `polyris.upstream_resolver`. Small contract surface = low drift risk. ✓
5. **Block 5 (the high-risk one) works.** Built the core wheel, packaged
   `console_api` **with the `polyris` symlink removed**, then
   `pip install core-wheel rsa -t lambda-pkg/` — exactly what `sam build` does
   from `requirements.txt`. `import polyris` and `from polyris.partitions import
   PartitionRange` both resolve from the vendored package. The symlink→pip
   replacement is real, not theoretical. ✓ **Bonus:** this *removes* the fragile
   `console_api/polyris` symlink ADR #98 nurses (the one `unzip`/Dolphin keep
   breaking) — core now arrives via pip instead.
6. **Backend dependency is one-way (ee → free), so free-backend is standalone.**
   Free-backend modules (`routes`, `dal`, `auth`, `utils`, …) import **nothing**
   from `ee`; only `ee` imports the free backend (a plugin depending on the core —
   the correct direction). `main.py` already guards `import ee` with
   `try/except ImportError`. Verified by building `console_api` **with `ee/`
   removed** + core via pip: `main.ROUTES` = 16, free routes live, starts cleanly
   with no `ee`. So the free backend ships public (working self-host), and only
   `ee` is private. (Earlier drafts mis-read the one-way ee→free imports as
   "fused" and wrongly proposed a whole-backend-private split; this spike
   corrected that.) The full assembly (free + ee + core pip) also works:
   `main.ROUTES` = 58, `ee.MODULES` = 12.

### Problem the spike surfaced (must be handled)

`polyris/_ee/` (the AI assistant, 9 files) lives **inside the `polyris` package**,
and `pyproject.toml` couples to it in two places that **break a public core**:

```toml
polyris-ai = "polyris._ee.ai.cli:main"   # entry point → private module
[tool.setuptools.packages.find]
include = ["polyris*"]                     # glob would package polyris._ee
```

So the split is **three** proprietary roots, not two: backend `ee/`, UI `ee/`,
**and SDK `polyris/_ee/`**. The SDK one is the trickiest because it is nested
inside the core package. It must be extracted to `polyris-ee`, the `polyris-ai`
entry point removed from the public `pyproject.toml`, and the `include` glob
narrowed so a stray `_ee` can never leak into the public wheel.

## Scope of work (from spikes, not estimation-by-feel)

| # | Block | Risk | Notes |
|---|---|---|---|
| 1 | Move **`console_api/ee/`** (paid routes only) → private repo; free backend (`routes`, `dal`, `auth`, `main`) stays public | low | one-way ee→free; `main.py` already makes `ee` optional (try/except), so free backend serves 16 routes without it |
| 2 | Make SDK core a tagged, installable package | low | build already works; `polyris-ai` entry point + `include` glob already fixed (Step 1 done) |
| 3 | **Extract SDK `polyris/_ee/` → private repo** | med | nested in core package; surfaced by spike |
| 4 | Split UI: free `@polyris/ui` (public) + private `@polyris/ui-ee` | med | one-line change in `gen-ee-active.mjs`; `ee-contract.ts` stays public |
| 5 | **Lambda build: symlink → vendored core** | **verified** | spike passed: on the SaaS build, free backend + `ee` + `pip install core-wheel -t pkg/` → 58 routes assemble and import cleanly, no symlink; free-only build → 16 routes |
| 6 | Two CI pipelines + drift gate in private CI | med | split `ci.yml`; delete `oss-export.sh`, `dco.yml`, secret-scan |
| 7 | Version sync: core tag ↔ `polyris-ee` dependency | low | `check-versions` adapts; drift test catches desync |

Highest risk is **Block 5**: today the Lambda gets the core via the
`console_api/polyris` symlink ADR #98 deliberately nurses. The split replaces it
with a git-tag dependency (or vendored `pip install polyris@tag -t ./package`).
This must be spiked on a real `sam build` before committing.

## Drift control (between repos)

The existing SSoT machinery moves across the boundary rather than being rebuilt:

- **SSoT stays in public core** (`constants.py`); `polyris-ee` imports it, never
  copies it.
- **Codegen output ships inside the package** — `polyris.codegen.sync_enums`
  generates `_shared/`, `console_api/constants_generated.py`,
  `ui/generated/enums.ts` in the public repo; the paid repo receives them via the
  installed package.
- **Drift/parity tests** (`test_enum_drift`, `test_drift`, the 8 parity tests)
  move into private CI and assert "`ee` is compatible with the installed core".
- **`pip install -e`** during development makes drift visible in the same run.

Residual risk: drift is **caught at CI/release**, not impossible by construction
(there is a window between a core change and the paid repo bumping its tag). The
git-tag pin + drift gate cover it.

## Hard prerequisite

**Do not split before the SSoT consolidation (v0.79.0, the planned full enum
consolidation) is complete in the monorepo.** Splitting while shared constants
still exist as multiple hand-maintained copies would scatter those copies across
the repo boundary, where codegen can no longer sync them — building drift in from
day one. Consolidate to a single `constants.py` → codegen source first, **then**
split, so it migrates cleanly.

## Consequences

**Gained:** live public history; direct PRs (no cherry-pick); paid code invisible
(self-host of paid impossible); private development space for paid work.
**Removed:** `oss-export.sh`, snapshot mirror, secret-scan-on-export, DCO sync
friction.

**New ongoing cost (the trade for the above):** release ordering (core tag →
then bump `polyris-ee`); the public contract (`register(router)`,
`@/ee-contract`, `constants.py` keys, the 3 core modules) becomes a versioned API
— breaking it is a deliberate major-version event, not a free edit; two repos and
two versions to keep in sync. This is the maintenance load the team accepts in
exchange for contributions + live history.

## Resolved: paid is SaaS-only

Paid features run **only on our infrastructure** — a customer never runs the paid
build themselves. This is the strongest case for the Dagster model: the private
`polyris-ee` repo is never distributed, so paid code **never leaves our infra**
and self-hosting the paid tier is physically impossible. No license key or code
obfuscation is needed — keeping the repo private *is* the enforcement. The public
`polyris` repo is the free product customers self-host; the private repo is our
SaaS backend.

## Execution sequence (aligned)

Ordered so each step is reversible and verifiable before the next. Per CLAUDE.md
#6/#7, each step lands with its own tests + docs before moving on.

**Step 0 — SSoT consolidation: VERIFIED ALREADY DONE (spike).**
Checked the tree: all 8 enum families (`TaskStatus`, `TriggerRule`,
`BackfillStatus`, `BackfillCascade`, `BackfillGranularity`, `ExecutionStatus`,
`PipelineStatus`, `StalenessStatus`) exist as a **single** definition in
`polyris/constants.py`; the backend/UI copies are codegen output
(`DO NOT EDIT ... generated from polyris/constants.py`), and
`sync-constants --check` reports **"Generated enums in sync"**. The one apparent
exception — `dal/ddb_schema.TaskStatus` — is **not** a duplicate: it is a
deliberate 8-value *subset* (the values valid for `PipelineTokens.STATUS` on
task-level DDB rows) vs the 14-value full enum, guarded by its own
`test_ddb_schema.py`. So the v0.79.0 consolidation is effectively complete for
split purposes; **no separate consolidation work is required before splitting** —
only re-confirm `sync-constants --check` is green at split time.

**Step 1 — Core packaging hygiene (still monorepo, no split yet).**
Remove the `polyris-ai` entry point from the public `pyproject.toml`; narrow the
`include` glob so `polyris._ee` can never land in the public wheel; confirm
`python -m build --wheel` is clean. Reversible, no repo move.

**Step 2 — Create `polyris-ee` private repo; move the three proprietary roots.**
`console_api/ee/` (backend Team routes), `ui/src/ee/` (paid UI), and
`polyris/_ee/` (SDK AI). The public repo keeps core, free routes, free UI, SSoT.

**Step 3 — Wire the dependency (git tag, no PyPI).**
`polyris-ee/pyproject.toml`: `polyris @ git+https://…/polyris.git@<tag>`. Lambda
`requirements.txt`: add the same line (replaces the `console_api/polyris`
symlink — spike-verified). Local dev uses `pip install -e ../polyris`.

**Step 4 — UI repoint.** One-line change in `gen-ee-active.mjs`:
`./ee/team` → `@polyris/ui-ee`. `ee-contract.ts` stays public. Free build → empty
surface stub (already works); paid build → `npm install @polyris/ui-ee`.

**Step 5 — Two CI pipelines.** Public CI: core/sdk/integration tests +
`sync-constants --check` + UI build. Private CI: `pip install polyris@tag` + ee
tests + drift gate (`test_drift`, parity tests asserting compatibility with the
installed core) + product build. Delete `oss-export.sh`, `dco.yml`,
secret-scan-on-export.

**Step 6 — Version sync.** Core tag ↔ `polyris-ee` dependency pin; adapt
`check-versions`; drift gate catches desync.

Highest remaining risk is now **Step 2's SDK extraction** (the `_ee` nested in the
core package) — Step 1 de-risks it by fixing the packaging coupling first.
