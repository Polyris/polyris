---
area: sdk
accepted: by 2026-05-29
---
# ADR #91 — Rename backfill `cascade` → `downstream` (with deprecated alias)

#### Context

Phase 2 (ADR #88) added the top-level `upstream` option
(`off`/`smart`/`none`→`smart`/`force`) to the backfill request. The existing
opposite-direction option was named `cascade` (`auto`/`all`/`none`). So the
API ended up asymmetric: `upstream=…` walks producers, `cascade=…` fans out
to consumers. "Cascade" also overloads a word that means different things in
different tools. The asset-centric concept work (ADR #86) calls these two
directions plainly **upstream** and **downstream**; the API should match.

#### Decision

`downstream` is the canonical request/response field name for the
consumer-fan-out option; `cascade` is accepted as a **deprecated alias** on
input and mirrored on output for one transition window.

- **Input:** the request may send `downstream` (preferred) or `cascade`
  (deprecated). If only `cascade` is present, a `deprecated_field` warning is
  returned and its value is used. If both are present, `downstream` wins.
  Values are unchanged (`auto`/`all`/`none`); the `BackfillCascade` enum
  family is untouched.
- **Output:** preview, start, and list/detail responses return `downstream`
  (canonical) and continue to include `cascade` (a mirror) so existing
  readers don't break during the transition.
- **Internal:** the in-process variable, the DDB record field, the SFN input
  key, and the SFN template all keep the name `cascade`. It is invisible to
  users, and renaming it would force a second consecutive SFN-changing deploy
  (right after ADR #90) for zero user benefit. The boundary translation lives
  only at the API edge. Documented here so the internal/external name gap is
  intentional, not drift.

The deprecation window closes (alias removed) only when no client sends
`cascade` — tracked via the `deprecated_field` warning. The bundled UI keeps
working unchanged because the alias is accepted; migrating it to `downstream`
is cosmetic and independent.

#### Consequences

- The backfill API reads symmetrically: `upstream` + `downstream`.
- Zero behavior change, zero SFN change, no deploy disruption.
- One transition release where both names are accepted/returned.
