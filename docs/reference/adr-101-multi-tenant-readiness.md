# ADR #101 — Multi-tenant readiness (entitlement is tenant-source-agnostic; tenancy deferred)

> **Status:** Accepted — direction recorded, mechanism proven by spike. **No code
> changes.** The entitlement foundation (ADR #98–100) is already the right base for
> the future multi-tenant SaaS; building tenancy now would be premature structure.
> A spike showed that in a single deployment with `POLYRIS_TIER` unset, two tenants
> on different plans gate correctly (enterprise tenant → 200, team tenant → 403,
> `feature_not_in_plan`) **reusing the same registry, `capabilities_for()`, and
> `ee/team` vs `ee/enterprise` split** — only the tier *source* changed
> (`deployment_tier()` env → a `tenant_tier(event)` resolver), and the single-tenant
> env path still works (the swap is additive).

## Context

Today Polyris is **single-tenant**: one deployment serves one customer, the tier
comes from `POLYRIS_TIER` (one value per stack, ADR #100), and data is keyed by
domain (`pipeline_name`, `execution_name`, `asset_name`) with **no tenant
partition** in any of the seven tables.

The intended paid product is different: a **multi-tenant SaaS platform we host**.
Users register, subscribe, and deploy their pipelines into our platform; each
user's **subscription** decides team vs enterprise **per request**. The question
this ADR settles: is the open-core + entitlement architecture (ADR #98–100) the
right foundation for that, or does it need rework now?

## Decision

1. **The entitlement / gating layer stays as-is — it is tenant-source-agnostic.**
   The only thing coupled to single-tenancy is `deployment_tier()` (reads the env),
   and it is isolated to one function. Everything downstream (`capabilities_for`,
   `has_capability`, `requires`, the UI `can()`) operates on an abstract
   tier/capability set. Going multi-tenant swaps that one source for a
   `tenant_tier(event)` resolver (the tenant's plan, from their subscription) and
   threads the request `event` through `has_capability`/`requires`. The gates, the
   `FEATURES` registry, and the `ee/team` vs `ee/enterprise` code split are
   **unchanged**.

2. **team/enterprise (which features) is orthogonal to tenancy (who gets them).**
   They compose cleanly; keep both. The directory split and the gating mechanism
   serve both single- and multi-tenant deployments.

3. **No code is written now.** Threading a `tenant_id` through the DAL and handlers
   "in advance" (always defaulted) is the same premature-structure anti-pattern we
   avoid with the `generators.py` registry (deferred until the first paid service).
   It is multi-tenant work and is done **additively when the platform is built**,
   not speculatively against a model that does not exist yet.

## The multi-tenant work, when it happens (NOT now)

- **Tenant identity** — add a tenant/org claim to the authenticated principal (the
  Cognito `sub` is already on every request, so the hook exists) and a
  subscription/plan store keyed by tenant.
- **Tier resolution** — `tenant_tier(event)` reads the tenant's plan and replaces
  `deployment_tier()`; `has_capability` / `requires` / `can()` thread the event.
- **Data isolation (the large piece).** Add `tenant_id` to the partition key of
  every table and thread it through every DAL call and handler so tenant A cannot
  read tenant B. Built into the platform's tables **from day one** — this is not a
  retrofit or migration of the single-tenant tables, so there is nothing to rework
  in the current schema.
- **Execution isolation** — decide where tenant pipelines deploy and run (a shared
  account with per-tenant IAM scoping, or per-tenant sub-accounts). A major,
  separate architectural decision.
- **Billing** — a real subscription model. The current `subscriptions_repo` is
  *asset* subscriptions (dependency notifications), not billing.

## Invariant to hold meanwhile

New paid features gate on the **caller's** capability — already the rule (ADR
#100). Never bake a cross-tenant global assumption (e.g. "all rows belong to one
tenant") into a handler or a DAL method. Honouring this keeps the additive path to
multi-tenancy open.

## Consequences

- The open-source release needs **none** of this — only the free↔paid strip (ADR
  #98/#99) matters for publishing the public repo.
- Multi-tenancy is a substantial **separate** project (identity, data isolation,
  execution isolation, billing), but the foundation plugs into it cleanly with no
  rework — the entitlement layer is forward-plumbed exactly for it, as the spike
  demonstrates.
