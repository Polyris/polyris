# ADR #100 — Tier entitlement (team↔enterprise gated at runtime, not by strip)

> **Status:** Accepted — mechanism proven by spike against the project's own
> routing contract. A signature-agnostic `@requires(cap)` decorator composes with
> both real handler shapes (`handler(event)` and `handler(param_value, event)`);
> on a `team` deployment a team route returns 200 and an enterprise route returns
> 403 (`feature_not_in_plan`), on an `enterprise` deployment both return 200, and
> `enterprise ⊃ team` holds by construction. The registry lives under `ee/`, so
> the OSS build is unaffected. Supersedes the *implicit* extrapolation that the
> team↔enterprise boundary would also be a physical strip; the **free↔paid**
> strip from ADR #98/#99 is unchanged. Extended by **ADR #101**, which records that
> this entitlement layer is tenant-source-agnostic and the multi-tenant path is
> additive (no rework).

## Context

ADR #98 made the **free↔paid** boundary physical: proprietary code lives under a
distinct root (`console_api/ee/`, `polyris/_ee/`, `ui/src/ee/`) and "what is in
the build" decides the tier. ADR #99 extended the same physical mechanism to the
UI. Both also **pre-declared** the directory invariant `ee/team/ never imports
ee/enterprise/` but left the team↔enterprise *enforcement* deferred — at the time
`ee/enterprise/` held **0 routes**, so there was nothing to enforce, and the
unstated assumption was that the same per-tier strip would extend down a level.

Two facts now settle that deferred decision:

- **The paid tiers are hosted by us, not the customer (SaaS).** Team and
  Enterprise both run inside our own deployment. A self-hosted/on-prem tier where
  a customer holds the artifact does not exist.
- **The deployment is single-tenant.** Handlers take `event: Dict` and carry no
  org / tenant / account identity (`grep` for `tenant|org_id|workspace` in
  `console_api/` is empty). One deployment serves one customer; multi-tenancy is
  itself a future Enterprise feature.

Given those, a **physical per-tier strip** for team↔enterprise would mean: a
separate build artifact per tier, a tier upgrade becomes a redeploy of a
*different* artifact, and the test/CI matrix doubles — all for **no security
benefit**, because we host both tiers and the Enterprise source never leaves our
infrastructure. The boundary that physical strip exists to protect (proprietary
code must not enter the **public repo**) is the free↔paid boundary, and that
stays physical. Within `ee/`, both paid tiers are already private.

This is the standard open-core arrangement (e.g. GitLab): one private build
contains all paid code; what a given install may *use* is decided at runtime by
its plan, not by shipping it a different binary.

## Decision

**Two boundaries, two mechanisms.**

1. **free ↔ paid — physical strip (unchanged).** `ee/` is stripped from the OSS
   build (ADR #98 backend/SDK, ADR #99 UI). OSS never contains paid code, so the
   entitlement layer below is irrelevant to it.

2. **team ↔ enterprise — runtime entitlement, inside `ee/`.** Both paid tiers
   ship in one build; a **deployment-level tier** decides what is enabled.

**Registry — single source of truth.** `console_api/ee/entitlements.py` maps each
paid capability to the tier that owns it, and derives each tier's capability set
so that `enterprise = team ∪ {enterprise-only}` **by construction** (the superset
is not maintained by hand):

```python
FEATURES = { "execution.controls": "team", "asset.lineage": "team",
             "cost.reporting": "enterprise", "rbac": "enterprise", ... }
# tier -> caps, with enterprise inheriting team
```

**Deployment tier.** A single value per stack: `POLYRIS_TIER` (env var, declared
in the console_api Lambda's `Environment.Variables` in `sam/template.yaml`, with a
matching template parameter). It is read once via `config`; there is no per-org
lookup.

**Enforcement lives on the API — this is the real boundary.** A signature-agnostic
decorator gates a route:

```python
@requires("cost.reporting")
def get_cost(pipeline_id, event): ...
```

The wrapper takes `*args` (so it works for both `handler(event)` and
`handler(param_value, event)` as dispatched by `routing.py`), and returns the
existing `cors_response(403, {...})` when the deployment's tier does not include
the capability. It is applied to **enterprise** routes (0 today). **Team** routes
need no decorator: `team ⊂ enterprise`, and the OSS build does not contain them at
all, so they cannot be reached on a free install and always pass on a paid one.

**UI gating is UX only, never security.** A `/capabilities` route (in `ee/`)
returns the deployment's capability set; the UI fetches it once after login and a
`can(key)` helper hides controls the plan does not include. A hidden control is a
convenience, not a protection — the API still enforces, so a hand-crafted request
to an enterprise endpoint on a team deployment is rejected. (Pure-UI features with
no privileged data or action may rely on `can()` alone.)

**Directory structure mirrors the registry.** Code is organised as `ee/team/` and
`ee/enterprise/` on **both** runtimes (`console_api/ee/team`, `console_api/ee/enterprise`,
`ui/src/ee/team`, `ui/src/ee/enterprise`). The backend composes route modules from
the present tier packages (`ee/__init__.py` builds `MODULES` from team and, when
present, enterprise); the UI generator (ADR #99) scans `ee/*/index.ts` and merges
the present tier surfaces. The invariant `team` never imports `enterprise` holds
(enterprise may import team). The directories are **organisation, not gating** —
the registry + decorator do the gating.

**On-prem escape hatch, kept open at zero cost.** Because the tier directories are
physical, if a self-hosted paid tier is ever required, the *same* parameterised
strip used for OSS (ADR #99) layers on one level deeper — a `team` artifact strips
`enterprise/` — with no rework, because the seam is already a directory boundary.
Until a real on-prem customer exists, we do not build per-tier artifacts.

## Consequences

- **Adding a paid feature:** add its key to the registry under a tier; if it is
  enterprise, decorate its route with `@requires`; gate its UI with `can()`. No
  new build, no new pipeline.
- **Adding a tier:** add one tier→capabilities entry in the registry. No new
  build.
- **Upgrading a deployment Team→Enterprise:** change `POLYRIS_TIER`. No redeploy
  of a different artifact.
- **Current state:** per ADR #98 all paid code is **Team**; `ee/enterprise/` is
  empty on both runtimes. So `@requires` and `can()` apply to **0** features
  today — this ADR delivers the *mechanism and structure* so the first Enterprise
  feature is a trivial, additive change rather than a re-architecture.
- **Security boundary is the API.** Enterprise JavaScript sits in the shared
  static bundle and is therefore visible in browser devtools to a Team user. For
  a paid-vs-paid boundary this is acceptable (it is not an OSS source disclosure)
  because the API enforces entitlement regardless. If hiding Enterprise *client
  code* ever becomes a requirement, the options are per-tier static builds (the
  on-prem escape hatch above) or moving the logic behind an API call — neither is
  needed for correctness.
- **Supersedes** the implicit reading of ADR #98 that team↔enterprise would be a
  physical strip. ADR #98's free↔paid strip, and ADR #99's UI exclusion, stand
  exactly as written.
