# ADR #75 — DAL repository pattern for all 4 helper Lambdas (v0.79.3)

#### Context

Stage 7 of the multi-release alignment plan. The v0.78.12 architectural
audit found that the CLAUDE.md rule "DAL repository pattern for all
DynamoDB access" was honored by only 1 of 6 Lambdas (`console_api`).
The other helper Lambdas accessed DDB via raw boto3:

| Lambda | Raw boto3 calls before |
|---|---|
| `evaluate_deps` | 3 (batch_get + 2 get_item paths) |
| `notify_asset_subscribers` | 4 (query × 2, delete_item, put_item) |
| `check_assets` | 5 (query × 2, get_latest, put_item, delete_item) |
| `query_subscriptions` | 2 (paginated query) |
| `ui_bootstrap` | 0 (no DB access) |

Mike's Q3 = A ("all under DAL, є мінуси хіба?") confirmed the
direction. This ADR is the migration.

#### Decision

**1. Per-Lambda DAL directory, not a shared module.**
Each helper Lambda gets its own `dal/__init__.py` (or `qs_dal/__init__.py`
for query_subscriptions — see naming caveat below). The repos are:

| Lambda | DAL repos | Methods |
|---|---|---|
| `evaluate_deps` | `TokensRepo` | `batch_get_statuses`, `get_status_one`, `is_paused` |
| `notify_asset_subscribers` | `SubscriptionsRepo`, `AssetEventsRepo` | `list_for_asset`, `delete`, `query_recent` |
| `check_assets` | `AssetEventsRepo`, `SubscriptionsRepo` | `query_recent`, `get_latest`, `put_asset_subscription`, `delete` |
| `query_subscriptions` | `SubscriptionsRepo` | `list_for_dependency` |

**Why per-Lambda not shared.** Lambda deploy packages are independent —
sharing code requires either a Lambda Layer or copy-on-build. The
`_shared/constants.py` pattern uses copy via `make sync-constants`,
but DAL repos are larger and Lambda-specific (each Lambda accesses
different tables with different access patterns). Duplication here
is genuine: tiny `_resource()` boilerplate appears 3× across the four
Lambdas, but the table-specific repos are not shared.

**2. Naming caveat: `query_subscriptions/qs_dal/`** (not `dal/`).
The backend test conftest puts `sam/lambdas/console_api` on
`sys.path`, which imports console_api's `dal` package (a directory
with submodule files like `subscriptions_repo.py`). When pytest then
imports `query_subscriptions/index.py`, its `from dal import
subscriptions_repo` resolves to console_api's `dal` — wrong package
shape, no module-level `subscriptions_repo` singleton.

Three options were considered:
- (a) Evict `dal` from `sys.modules` in the test file — broke 50
  unrelated console_api backend tests that re-import dal lazily.
- (b) Use absolute imports `from sam.lambdas.query_subscriptions.dal
  import ...` — works in tests but fails at Lambda runtime where
  `sam.lambdas` isn't on the path.
- (c) Rename the package: `dal` → `qs_dal`. Removes the collision
  cleanly at both test time and runtime.

**Chose (c).** Other Lambdas keep `dal/` because their test files run
from their own directory with their own path, no console_api
shadowing. Only the SDK-side `tests/backend/test_query_subscriptions.py`
suffers the collision; renaming the package surface is cheaper than
fighting sys.path.

**3. Test pattern.** Existing tests patched `_get_dynamodb` and built
boto3 mock chains (`mock_dynamodb.Table().query.return_value = ...`).
The DAL pattern simplifies this to `mocker.patch.object(repo, 'method',
return_value=[...])`. Migration was mostly mechanical:
- evaluate_deps: rewrote 6 handler tests + dropped `_get_dynamodb`
  import. 56 tests pass.
- notify_asset_subscribers: 9 patches transformed via script;
  3 consecutive-check tests needed per-table mocks
  (`mock_sub_table` + `mock_events_table`) wired into the right
  repo. 12 tests pass.
- check_assets: 14 patches transformed; same pattern. 19 tests pass.
- query_subscriptions: 7 new Lambda-side tests + 7 SDK-side tests
  rewritten. 11 + 7 pass.

**4. `_get_dynamodb()` removed** from index.py in evaluate_deps,
notify_asset_subscribers, and check_assets. The lazy-init logic
moved to the DAL's `_resource()` (module-private). Anything that
still imported `_get_dynamodb` would break at import time — the
test migration caught this.

**5. SFN client init stays inline.** In notify_asset_subscribers,
`_get_sfn()` stays as-is — single Step Functions client, no
repository-shaped abstraction needed. DAL pattern is for
DynamoDB; SFN is a different service. Symmetry isn't a rule
worth honoring at the cost of overengineering.

#### What this does NOT do

- **Does not migrate `console_api/dal/`** — that's already done.
- **Does not introduce a shared DAL layer.** Each Lambda's DAL is
  independent. If three Lambdas grow to query the same table with
  the same access pattern, that's the time to extract — not before.
- **Does not migrate SFN/EventBridge clients to repos.** Those
  remain inline `boto3.client('stepfunctions')` and similar.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| SDK Python | 900 | **901** | +1 (test_query_subscriptions has 7 tests but 6 were renames, 1 net new test for AccessDenied) |
| console_api | 348 | **348** | 0 |
| evaluate_deps | 56 | **56** | 0 (rewrote 6 in place) |
| notify_asset_subscribers | 12 | **12** | 0 (rewrote 9 in place) |
| check_assets | 19 | **19** | 0 (rewrote 14 in place) |
| query_subscriptions Lambda | (new) | **11** | +11 (new suite) |
| **Python total** | **1335** | **1347** | **+12** |
| vitest | 830 | 830 | 0 |
| **All gates** | green | **green** | — |

#### Trade-offs

**Slight cold-start cost.** Each migrated Lambda now imports its
`dal/` package on cold start — a few extra ms. Negligible compared
to boto3's own init time. The benefit is testability and explicit
contracts.

**Per-Lambda DAL duplicates `_resource()` 3×.** Eight lines per
Lambda. Could be eliminated by introducing `sam/lambdas/_shared/dal_base/`
copied via make target like `_shared/constants.py`. Decision: not
yet. Three copies of eight lines is below the threshold where SSoT
infrastructure pays for itself.

#### Lesson

When a project rule applies but exceptions exist, the right question
isn't "is the exception OK?" — it's "can we close it cleanly?". The
2-5 raw boto3 calls per Lambda looked too small to justify
restructuring; the migration showed each one wrapped a real concern
(pagination, fallback, batching). Lifting them behind a repo
revealed the structure that was already there, just hidden in
inline code. Tests got 10× shorter because they could now express
intent (`mock_repo.method.return_value = [...]`) instead of
mechanism (`mock_dynamodb.Table().query.return_value = {'Items': ...}`).

The naming collision (qs_dal) is also a lesson: test infrastructure
that aggressively manipulates sys.path can shadow your packages in
non-obvious ways. Picking a unique name is cheaper than fighting it.
