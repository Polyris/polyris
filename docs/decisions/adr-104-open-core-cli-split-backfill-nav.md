---
area: open-core
accepted: by 2026-08-17
---
# ADR #104 — Open-core CLI split, backfill nav-tab slot, and contract-drift cleanup

**Context.** The open-source build advertised and partially wired Team-tier
features the open-core backend no longer registers (ADR #99/#100) — "contract
drift." Concretely: the bare `polyris` CLI was a backfill client hitting
`/api/backfill*` (404 in OSS); `Header.tsx` polled `/api/backfills` every 5s
via an inline `useBackfillsListQuery('active')` (a live 404 in OSS); a dead
`usePipelineMetricsQuery` hit a non-existent `/api/pipeline-metrics`; and the
README, `docs/operations/API.md`, and `HelpModal` promised backfill, Gantt,
calendar, Slack/PagerDuty, and API tokens as generally available. The boundary
is intentional (backfill/ops/governance are paid; engine + assets + lineage are
free) — the drift was that the public surface still claimed the paid half.

**Decision.**
1. **CLI split** (supersedes the backfill-dispatch half of ADR #51). The bare
   `polyris` command is now a pure command index — it prints the available
   `polyris-*` scripts and exits 0, performing no work. Backfill moves to a
   dedicated `polyris-backfill` console script shipped from `polyris-ee`
   (`polyris._ee.backfill_cli`), with a flattened parser
   (`polyris-backfill pipeline|asset|list|show|cancel|retry-failed`). The OSS
   wheel carries neither `_ee` nor any backfill code.
2. **Backfill nav tab → `BackfillNavTab` paid-surface slot** (ADR #99). The
   Header's Backfills tab and its active-count badge now live in the Team build
   behind the `BackfillNavTab` slot; the free `ViewTab` helper is exported for
   reuse. In OSS the slot is absent (empty `paidSurface` stub), so no tab is
   rendered and the `/api/backfills` poll never runs.
3. **Removed the dead `usePipelineMetricsQuery`** (no consumer; targeted a
   non-existent endpoint) and its query key.
4. **Team backfill e2e tests** (`tests/e2e/test_backfill.py`) moved to
   `polyris-ee`; the two repos are independent (РОЗЧЕПЛЕННЯ), so the e2e harness
   travels with the tests.
5. **Docs** (README, API.md, HelpModal) now mark backfill, Gantt/calendar view
   modes, and Slack/PagerDuty alerts as Team-tier.

**Consequences.** The OSS build is self-contained and honest: no live 404s, no
free-tier promises the backend won't serve. Team features remain fully available
through the overlay, validated via the merged tree (CE base + `src/ee`). The
`BackfillNavTab` slot follows the same eager-component pattern as the other
in-Header surfaces (e.g. `GanttChart`, `PipelineActionsProvider`).
