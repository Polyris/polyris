# ADR #70 — "Active" filter renamed to "Running"; narrowed to running-only (v0.78.13)

#### Context

The Backfills list page had filter chips: All / Active / Completed / Failed
/ Partial / Canceled. "Active" was a synthetic bucket meaning
`{running, pending}`.

User feedback: "active = running?" — Mike observed the two are effectively
synonymous from a user perspective, and asked to rename. Investigation
confirmed: `BackfillStatus.PENDING` is defined in backend constants but
the `bulk_backfill` SFN writes `status='running'` immediately on start,
so `pending` is theoretically possible but operationally never seen by
end users.

#### Decision

Rename the filter chip from "Active" to "Running", and narrow its
semantics to derived-running only (no longer matches `pending`).

Implementation:
- `STATUS_OPTIONS` now has `{ value: 'running', label: 'Running' }`
  instead of `{ value: 'active', label: 'Active' }`.
- `statusFilter` state type narrows from `BackfillStatus | 'active' | 'all'`
  to `BackfillStatus | 'all'` — no synthetic value.
- Filter logic simplifies to `computeDerivedBackfillStatus(b) === statusFilter`
  with no special case for active.

#### What about `pending`?

The backend constant `BackfillStatus.PENDING` stays — removing it would
be a larger backend change and the value MIGHT still be written in some
edge case (manual DDB update, future SFN restructure). The constant
remains for forward compatibility; the UI filter just doesn't show a
chip for it. If a `pending` backfill ever appears, it shows under "All"
but not under "Running" — the user sees an explicit pending status pill.

This trade-off mirrors a known frontend cleanup pattern: don't break
data contracts when removing UI affordances. Definition stays; surface
changes.

#### Why not include `pending` under "Running"

Mike's question implied "active = running" — that they're the same
thing. Users think binary: "is it running or not?". Hiding `pending`
under "Running" would re-introduce the same ambiguity in code (filter
matches two values, label says one). Cleaner: one chip = one status.
If `pending` becomes operationally meaningful later (e.g. SFN
restructure that genuinely lingers in pending), add a chip for it
then.

#### Tests

- `BackfillsListPage.test.tsx` — 2 new tests: "Running" chip present
  + "Active" chip gone; "Running" filter narrows to derived-running
  only (excluding stuck-running with derived=completed).

#### Lesson

When a synthetic filter bucket like "Active" gets questioned by users,
it's usually because the bucket abstracts over a distinction the user
doesn't care about. The fix isn't always "explain the abstraction
better" — it's "remove the abstraction." Direct status-to-chip mapping
is simpler to reason about, both for users and for the code.
