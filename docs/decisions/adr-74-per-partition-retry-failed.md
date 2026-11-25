# ADR #74 — Per-partition retry-failed (v0.79.2)

#### Context

Stage 6 of the multi-release alignment plan. The user-facing problem:
when a backfill ends in 'partial' (some partitions failed), the only
action is "retry all failed". For a 30-day backfill where 2 dates
failed, the user has to either retry all 28 untouched failures
together or manually start a new ad-hoc backfill with just those 2.

This ADR adds **per-partition retry** by extending the existing
`/api/backfills/{id}/retry-failed` endpoint with an optional
`partition_keys` filter.

#### Decision

**1. Extended endpoint, NOT a new one.** Per the plan: "Extension of
existing cancel + retry_failed endpoints, not new routes."

```http
POST /api/backfills/{id}/retry-failed
{
  "partition_keys": ["2024-01-15", "2024-01-16"]   // optional
}
```

- **Body omitted or empty array** → retry ALL currently-failed
  partitions (v0.78.11 behavior, backward-compatible).
- **`partition_keys` provided** → retry only the intersection of
  caller's keys and actually-failed partitions.
- **Caller-provided key NOT in failed set** → `422
  partition_keys_not_failed` with the actual failed list in the
  response (so the UI can surface stale state).

**2. Strict validation.** Silent intersection (just compute
`intersect(requested, failed)` and proceed) would hide client bugs
— stale UI showing failures that backend has since cleaned up,
or a typo in the partition key. The endpoint rejects with 422
when any requested key isn't actually failed.

**3. Frontend affordance: tiny ↻ button on failed cells.**
Appears at top-right of each `failed`-status partition cell in the
heatmap. Clicking:
- Stops event propagation (doesn't open DAG)
- Confirms via `window.confirm`
- POSTs with `partition_keys: [key]`
- On success: toast + `onRetryStarted(newBackfillId)` callback
- On 422 or other error: toast with error message

#### What this does NOT do (deferred)

- **Per-partition CANCEL.** Cancel mid-flight needs SFN template
  changes — the bulk_backfill SFN already checks a backfill-level
  cancel flag at each Map iteration, but adding per-partition skip
  flags requires additional DDB state and JSONata logic in the SFN
  template. Significant risk in a single release. BACKLOG.
- **Per-partition SKIP.** Same issue as cancel — needs SFN template
  changes. BACKLOG.
- **Multi-partition select in UI.** No checkbox column on the heatmap
  for batch select of "these 3 partitions". The single-partition
  button covers the most common ask (one specific failure that the
  user has a fix for). Batch select is a UI-design item separate
  from this ADR.

#### Why "strict" instead of silent intersect

Three reasons:

1. **Stale UI catch.** If the user's tab has been idle and the
   underlying backfill changed (someone else retried, or retry
   succeeded), the UI might offer "retry partition X" when X is no
   longer failed. Silent intersect would do nothing; strict 422
   tells the user what happened so they can refresh.

2. **Typo catch.** Per-partition retry might end up scriptable
   (e.g. a CLI command). Silent intersect would silently no-op on
   typo. 422 surfaces the mistake.

3. **Diagnostic in response body.** Returning the actual failed list
   alongside the error lets the client offer "retry these instead?"
   without a second roundtrip.

#### Tests

Backend (`tests/routes/test_backfill.py`, +7 in `TestRetryFailedWithPartitionKeys`):

- Empty body retries all failed (backward-compat)
- Subset partition_keys retries only those
- Unknown partition_key → 422 with `failed_partitions` in body
- Non-array `partition_keys` → 422 `invalid_partition_keys`
- Malformed JSON body → 422 `malformed_body`
- Empty array == no filter (treated as "retry all")
- Duplicate keys deduplicated (set semantics)

Frontend (`hooks/queries/useBackfillQueries.test.ts`, +3 in
`useRetryFailedBackfillMutation`):

- Backward-compat: bare `mutateAsync('bf-id')` works, sends empty body
- New shape `{backfillId, partitionKeys: [...]}` sends `partition_keys`
- Empty array → omitted from body (parallels backend treatment)

UI behavior tests for the ↻ button per-partition retry are covered
implicitly via existing BackfillDetailPage tests + the mutation
contract test. A dedicated click-through test would be valuable but
involves modal/confirm interception not yet abstracted in the test
harness — left as a follow-up.

#### Lesson

Adding an optional filter to an existing endpoint is almost always
cleaner than minting a new endpoint. New URLs need API versioning
thinking, client compat shims, and route discovery — none of which
helps the user. The trade-off is being strict about validation, so
the optional parameter doesn't quietly do the wrong thing.

Per CLAUDE.md #4 (visibility of errors): rejecting unknown partition
keys with a diagnostic 422 instead of silently intersecting is the
right default. Silent-by-default is what created the v0.78.12 stuck-
running bug — defensive code at one layer hiding what's actually
wrong upstream.
