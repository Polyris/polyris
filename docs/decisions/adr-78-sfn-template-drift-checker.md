---
area: process
accepted: 2026-05-27
---
# ADR #78 — SFN template drift checker

#### Context

ADR #72 (v0.79.0) brought SSoT enum codegen for Python and TypeScript.
SFN template `.tpl.json` files were the remaining gap — they contain
status string literals like `"failed"`, `"upstream_failed"`,
`"deps_ready"` embedded in JSONata expressions and DDB
ExpressionAttributeValues. A rename of `TaskStatus.UPSTREAM_FAILED` in
canonical would break templates silently.

Mike's BACKLOG item: "SFN template literals → canonical via окремий
generator". On investigation, mechanical substitution into JSONata
expressions like `$status = "failed"` would either:
- break JSONata readability (`$status = "{{TASK_STATUS_FAILED}}"`),
- require a custom template processor in the SAM deploy pipeline,
- or both.

The cheaper intervention that achieves the same SSoT goal is
**validation, not generation**.

#### Decision

**Build a drift checker, not a substitution generator.**

`polyris/codegen/check_sfn_templates.py` scans all
`sam/sfn_templates/**/*.json` for status string literals in two
patterns and validates each against canonical:

1. **Pass-state status writes:**
   `"status": "<value>"`
2. **DDB status updates:**
   `":status": {"S": "<value>"}` (and common name variants like
   `:newstatus`, `:s`)

If any value isn't in `polyris.constants.TaskStatus`, the checker
fails and prints the file/line. Run via `make check-sfn-templates`
locally and in CI.

#### Why drift-check beats substitution

| Aspect | Substitution generator | Drift checker |
|---|---|---|
| **Template readability** | Loses (placeholders) | Keeps (real values) |
| **SAM/JSONata compatibility** | Complex (custom pre-processor) | None needed |
| **Catches typos** | ✅ | ✅ (typos in any literal) |
| **Catches removed canonical values** | ✅ | ✅ |
| **Catches values added without canonical** | ✅ | ✅ |
| **Implementation cost** | ~1 day | ~2 hours |
| **Runtime cost** | Build step | CI gate only |

The drift checker delivers identical guarantees with lower complexity
because templates are read by humans far more often than they're
generated.

#### Allowlists

Two narrow exemption sets:

1. **`ALLOWLIST`** — common JSON field names that pass status-shape
   but aren't statuses: `pass`, `task`, `choice`, `name`, `value`, etc.
   These shouldn't be flagged even if they appear in status-like
   contexts.

2. **`HELPER_OPERATION_STATUSES`** — operation-result values returned
   by helper Lambdas as Pass-state Output. Currently:
   - `restarted` — `restart_task` helper signals successful restart.

Adding to either allowlist requires explicit justification in code
comments — these are exceptions to canonical enforcement and must
have a documented reason.

#### Pattern scope (deliberate narrowness)

- The DDB AV check is scoped to `:status` / `:newstatus` / `:s`
  attribute placeholders only. Other attribute names (`:nf` for
  `notification_failed`, `:lu` for `last_updated`, etc.) carry
  diagnostic data with no canonical contract.
- JSONata comparisons inside `{% ... %}` blocks have JSON-escaped
  quotes that defeat regex matching. The Pass-state and DDB-AV
  checks cover the surfaces where status writes actually appear.
- Tested coverage: 12/14 canonical TaskStatus values appear in some
  template; `pending` and `succeeded` don't, which is fine — those
  values come from backend Lambda writes, not SFN-direct DDB writes.

#### Tests

11 unit tests in `tests/sdk/test_sfn_template_drift.py`:
- 2 canonical loader tests (sanity).
- 2 allowlist shape tests.
- 1 real-templates-pass test (the committed templates must always pass).
- 6 synthetic-template detection tests (typos, valid values,
  allowlist behavior, DDB pattern detection, non-status DDB
  attribute ignoring).

| Suite | Was | Now | Δ |
|---|---|---|---|
| SDK Python | 901 | **912** | +11 |
| **All gates** | green | **green** | — |

#### What this does NOT do

- **Doesn't generate templates.** Authors still hand-edit `.tpl.json`
  files. The checker catches divergence, doesn't enforce structure.
- **Doesn't check non-status string literals.** Trigger rule names,
  trigger condition keywords, etc. are not validated. The Python
  evaluator handles those; SFN templates rarely embed them.
- **Doesn't fix detected drift automatically.** Failing the CI gate
  forces a human decision: was this an intentional canonical change,
  or a typo?

#### Lesson

When the BACKLOG says "generator", the right first question is "what
does generation give us that validation doesn't?" In this case:
nothing. The substitution would have been pure ceremony — the
templates would still be hand-edited at the placeholder layer, just
with worse readability. The validator catches the same class of
mistakes at lower cost.

"Generator" in the Polyris vocabulary now has two flavors:
- **Codegen** (sync_enums): produces files from canonical, drift
  check ensures they stay in sync.
- **Drift checker** (check_sfn_templates): scans hand-authored
  files for compatibility with canonical.

Both serve SSoT. The codegen flavor wins when the output is
mechanical (TypeScript enums); the drift-check flavor wins when
the output is hand-authored (JSONata expressions).
