---
area: ui
accepted: 2026-05-27
---
# ADR #67 — Client-side derived status for backfills

#### Context

`bulk_backfill` SFN writes `status='running'` at start and writes a
terminal status (`completed`/`partial`/`failed`) in its `Finalize`
step at the very end (template: `sam/sfn_templates/bulk_backfill/sfn.tpl.json`).
If the Finalize step never runs — SFN aborted between counter updates
and finalize, manual intervention, eventual-consistency lag, IAM
revocation, etc — the DDB record stays at `status='running'` forever.

User-visible symptom (Mike, v0.78.7 deploy review): TOTAL=2,
COMPLETED=2, FAILED=0, and yet the UI showed the "Running. 2 of 2
partition(s) processed so far" banner with a Cancel button. Mike's
note: *"вони ж ніби компліт вже а не ранінг"*.

#### Decision

Introduce `derivedStatus` in `BackfillDetailPage` that overrides
backend-reported `running`/`pending` when partition counters indicate
the work is done. Mirror the SFN Finalize logic on the client:

```ts
if (raw !== 'running' && raw !== 'pending') return raw;  // terminal — respect backend
const processed = completed + failed + skipped;
if (total > 0 && processed >= total) {
    if (failed === 0) return 'completed';
    if (completed === 0) return 'failed';
    return 'partial';
}
return raw;
```

`derivedStatus` drives:
- Status pill modifier class (`bd-status-pill--${derivedStatus}`)
- Banner choice (partial / failed / canceled / running)
- `isActive` flag (gates the Cancel button)
- `canRetry` flag (gates the Retry failed button)

A `console.warn` is emitted whenever raw and derived disagree, so
future debugging can quickly spot the discrepancy in the browser
DevTools console.

#### Why client-side and not backend

The right fix is finding out why `Finalize` doesn't run in some cases
and ensuring it does. That requires SFN execution log inspection
across multiple stuck backfills to find the pattern. Until that
investigation produces a backend fix, the UI workaround is the
minimum-cost safety net — users see correct state, action buttons
match reality, no false alarms.

The workaround is conservative: it only re-interprets the
**non-terminal** raw values. If backend writes `completed` or
`failed`, the UI respects them. If backend writes `running` AND counts
agree (still in flight), the UI shows running. The override only fires
in the narrow `running` + `counts say done` case where the backend is
demonstrably wrong.

#### Recovery if backend is fixed later

When the SFN Finalize bug is identified and fixed, `derivedStatus`
becomes a no-op for new backfills (raw will be terminal). Existing
stuck backfills in DDB will still benefit until they're re-run or
manually fixed. The override can be removed in a future cleanup pass,
or kept indefinitely as defense-in-depth (recommended).

#### Pinned by tests

`BackfillDetailPage.test.tsx › derivedStatus (v0.78.8, ADR #67)`:
6 tests cover (a) terminal raw respected, (b) stuck-running →
completed, (c) stuck-running → partial, (d) stuck-running → failed,
(e) genuine in-flight remains running, (f) Cancel button hidden when
override fires.

#### Lesson

State derived from multiple fields (raw status + counters) should be
computed in one place at read time, not stored. Storing `status` in
DDB and updating it via a separate SFN step means two writes have to
both succeed for the data to be consistent. A computed view in the
read path eliminates that whole class of failure mode at the cost of
slightly more work per read — usually the right trade for UI-facing
state.
