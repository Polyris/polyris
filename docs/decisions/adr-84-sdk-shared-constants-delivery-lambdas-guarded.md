---
area: deploy
accepted: 2026-05-28
---
# ADR #84 — SDK / shared-constants delivery to Lambdas — guarded copy now, PyPI later

#### Status

Accepted. Records a deliberate decision so the question isn't re-litigated
each time the constants duplication is noticed.

#### Context

AWS Lambda functions cannot import each other's code. Canonical definitions
in `polyris/constants.py` must therefore be *delivered* into each Lambda
artifact. Today this happens two different ways:

- **console_api** bundles the whole `polyris` package via a committed
  symlink (`sam/lambdas/console_api/polyris` → repo-root `polyris/`); SAM's
  Python builder follows it into the artifact.
- **helper Lambdas** (evaluate_deps via the `_shared` copy) carry a
  codegen-generated `constants_generated.py` plus, in `_shared/constants.py`,
  a few hand-written status classes with an ImportError fallback so the file
  imports standalone in unit tests.

After the v0.80.0 enum-SSoT consolidation (ADR #83), console_api re-exports
all status classes from the generated module, and `_shared`'s remaining
manual classes are guarded against drift by `check_shared_constants`. What
remains is the question of whether to eliminate the duplication *structurally*
rather than guard it — i.e. how shared code reaches each Lambda.

#### Options considered

**A — Fold `_shared` into the generated module (re-export classes).**
Mirror what console_api does. *Rejected for `_shared`:* it removes the
ImportError fallback that lets `_shared/constants.py` import standalone in
direct unit tests, trading real test resilience for cosmetic tidiness, and
hard-couples `_shared` to `constants_generated` at import time.

**B — Keep the guarded copy (status quo after v0.80.0).** `_shared` keeps
its manual classes + fallback; `check_shared_constants` (run by
`make sync-constants`) fails CI if those classes ever define a value absent
from canonical. Duplication remains but **cannot silently drift**.

**C — Lambda Layer.** Publish `polyris` (or shared constants) as a layer,
attach to each Lambda; code lands on `/opt/python` at runtime, so every
Lambda imports canonical `polyris` directly — collapsing the symlink,
`constants_generated` copies, and `_shared` manual classes together.
AWS-native and the cleanest runtime model, but: separate layer deploy cycle
+ extra CloudFormation resource + layer-version ARN pinning; and it does
**not** fully remove the standalone-test fallback, because a layer is a
runtime concept absent under local/CI `pytest` (tests would still need the
code pathed). Per the packaging tech-debt note, "not recommended unless
multiple Lambdas need polyris and PyPI publish is far off."

**D — PyPI (or private CodeArtifact) + `requirements.txt`.** Publish
`polyris`, pin it in each Lambda's `requirements.txt`; `pip install` vendors
it at build. This is the documented **target end state** for the symlink
migration. Removes the symlink, and would let helper Lambdas import
`polyris` directly (retiring `constants_generated` + `_shared` classes).
Gated on the OSS/PyPI launch.

#### Decision

- **Now: Option B.** The only real risk in the duplication — silent drift —
  is already closed by the `check_shared_constants` guard. B preserves the
  standalone-test resilience of `_shared` at zero ongoing cost.
- **End state: Option D (PyPI).** When `polyris` is published, the symlink
  *and* the per-Lambda constants copies *and* the `_shared` manual classes
  collapse together: every Lambda declares `polyris==X.Y.Z` and imports the
  canonical module directly. Codegen mirrors (`constants_generated`) become
  unnecessary.
- **Option C (Layer) is the fallback** if a second Lambda needs `polyris.*`
  before PyPI is ready — that event is itself the migration trigger.

Crucially, this is **one packaging migration done as a single move**, not a
piecemeal change for constants alone. The constants duplication and the
polyris symlink are the same underlying problem ("Lambdas can't share
code"); they should be resolved together, under the existing
"Lambda packaging of polyris SDK" tech-debt item, when its trigger fires
(PyPI release, or a second `polyris` consumer).

#### Consequences

- No code change from this ADR; it ratifies the v0.80.0 state (guarded copy)
  as intentional, not unfinished.
- The `_shared` manual classes are permitted to remain *only while* the
  `check_shared_constants` guard exists; removing that guard without doing
  the PyPI/Layer migration is forbidden.
- Revisit when any migration trigger from the packaging tech-debt item
  fires; at that point prefer D, fall back to C, never A.

#### What must NOT come back (cross-ref packaging tech-debt)

The Lambda-local `Makefile` + `BuildMethod: makefile` + `cp ../../../polyris`
pattern and the top-level `make sam-build` vendor-copy both failed live and
are blocked by regression tests in
`tests/sdk/test_reviewer_regressions_v078.py`. A future packaging migration
must use PyPI/CodeArtifact or a Layer, not a build-time copy hack.
