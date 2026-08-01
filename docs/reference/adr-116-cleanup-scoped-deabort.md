# ADR #116 — Cleanup via scoped de-abort

> **Status:** ACCEPTED IN PRINCIPLE — implementation deferred to a live-SFN validation
> cycle (Phase 2, post-release). Not required for the OSS release gated by ADR #115.

## Context

ADR #114 keeps failure intervention-first; ADR #115 narrows the advertised
`trigger_rule` set to what that model can execute, dropping `one_failed`, `all_failed`,
`all_done_min_one_success`, and `one_done` as structurally unreachable. `all_done` is the
one rule in that dropped set with clear standalone product value: "always run this
regardless of outcome" is the standard cleanup/teardown primitive (temp tables,
releasing resources, final notification) present in every comparable orchestrator. It is
carved out here rather than silently lost.

`all_done` cannot fire today for the same reason as the others: a resolved failure calls
`SendTaskFailure`, which `Run_All_Tasks`'s `Parallel` (`Catch: States.ALL ->
Pipeline_Failed`) catches, cancelling sibling branches — including an `all_done` cleanup
task — before they complete.

## Decision

Scope a **narrow** de-abort limited to the `all_done` cleanup path — explicitly not the
general reversal of ADR #114 that would be needed to make the other dropped rules work:

- On a resolved failure, if any awaiting sibling branch has `trigger_rule = all_done`,
  the failure handler sends `SendTaskSuccess` (carrying the `failed` status in the
  payload/DDB record) instead of `SendTaskFailure`, allowing the `Parallel` to complete
  normally instead of aborting.
- Otherwise (no `all_done` sibling waiting), behavior is unchanged: genuine failure still
  aborts the `Parallel` as it does today.
- The `Run_All_Tasks` `Parallel`'s `Catch` is repointed accordingly so it no longer fires
  for this case.
- Run-status derivation is already DDB-driven and independent of the SFN execution result
  (ADR #112) and should be structurally unaffected — this must be **verified**, not
  assumed: a `Parallel` that completes "successfully" while a task item is `failed` must
  still derive execution status `failed`, and `reconcile_execution_status` must not
  override that from the (now-SUCCEEDED) SFN execution result.

## Consequences

- Requires live-SFN e2e validation before shipping — abort/race behavior between a
  cancelled branch and an in-flight `Auto_Skip`/cleanup branch is not observable at the
  unit-test level (per the discussion that produced ADR #114/#115: the simulator proves
  the logic, not the SFN runtime). This is explicitly the maintainer's part of the work,
  not something a sandboxed change can self-certify.
- Until implemented, `all_done` is documented (ADR #115, `docs/features/DSL.md`) as
  "runs on the success path; failure-path cleanup is experimental / not yet supported."
- Does not reopen ADR #114 for `one_failed`, `all_failed`, or `all_done_min_one_success` —
  those remain out of scope; only the cleanup case is exempted.

## Supersedes

None. Narrow, additive exception to ADR #114, scoped to one rule.
