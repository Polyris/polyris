# ADR #109 — Unified common parameters for `@task.<type>` decorators (`CommonTaskKwargs`)

> **Status:** ACCEPTED — implemented. Caller-visible behavior is unchanged for
> every documented usage; typo handling is preserved (ADR #106 D5) with a
> clearer error message.
>
> **Amended (v0.93.0):** the common set changed — `slack_channel` removed,
> assets (`outlets`/`inlets`/`wait_for`) added. See [Amendments](#amendments);
> the historical list in *Context* below is preserved as written.

## Context

All seven `@task` variant decorators (`sfn`, `lambda_`, `glue`, `ecs`,
`athena`, `emr`, `batch`) shared **13 common parameters** (`task_id`, `role`,
`wait_before`, `retries`, `retry_delay`, `retry_exponential_backoff`,
`retry_jitter`, `max_retry_delay`, `execution_timeout`,
`orchestration_timeout`, `trigger_rule`, `slack_channel`,
`skip_on_backfill`), each duplicated in every signature *and* in every
passthrough to `_create_task` — e.g. the `retry_delay` signature line existed
16×. Adding one common parameter meant ~14–16 identical edits (measured three
times during the retry work), which is precisely the fan-out CLAUDE.md #14
warns about, and the direct cause of signature drift risk between variants.

All 13 parameters were already keyword-only in every variant (each signature
has a bare `*` after `_func`), and all 13 defaults were byte-identical to the
defaults on `_create_task`.

## Decision

1. **One declaration:** `CommonTaskKwargs(TypedDict, total=False)` in
   `task.py` declares the 13 common parameters with their real types
   (`Optional[...]` where `None` is a default).
2. **Variant signatures collapse:** each variant keeps only its type-specific
   parameters plus `**common: Unpack[CommonTaskKwargs]` (PEP 692;
   `typing.Unpack` is native on the project floor, Python ≥ 3.11). Type
   checkers and PEP 692-aware IDEs still see and check every common kwarg.
3. **Single source of defaults:** an omitted common kwarg is simply not
   forwarded, so the default comes from `_create_task` — the one place that
   already owned the `default_args` fallback logic.
4. **Strict-kwargs preserved (D5):** `_validate_common_kwargs(name, common)`
   runs before every `_create_task` call and raises
   `TypeError: task.glue() got an unexpected keyword argument 'retrys'` —
   same exception class the pre-unification explicit signatures raised, now
   naming the decorator the user actually called instead of `_create_task`.
5. **Base `@task` (`__call__`) untouched:** it keeps its documented generic
   `**kwargs` catch-all; only the seven typed variants were unified.

`task.py` shrank from 1068 to 946 lines; the 7× passthrough blocks are gone.

## Consequences

- Adding a common task parameter = add it to `CommonTaskKwargs` + to
  `_create_task` (+ the `Task` field). Every variant picks it up
  automatically; the per-variant edit step no longer exists.
- Common parameters are no longer individually listed in
  `inspect.signature()` of the variants. No test or tool in the repo
  introspected those signatures (verified before the change); runtime callers
  are unaffected because the parameters were already keyword-only.
- The D5 typo tests (`tests/sdk/test_task_core.py`) pass unchanged and now
  exercise the shared validator, keeping its raise path covered.

## Amendments

### v0.93.0 — common set updated

The common set has changed since acceptance. The pattern is unchanged; the
membership is not.

- **`slack_channel` removed.** DSL-level alert routing was torn down in ADR #103
  (alerts are configured in the Console UI, not the DSL). `slack_channel` is no
  longer a task parameter and was dropped from `CommonTaskKwargs`.
- **Assets added — `outlets`, `inlets`, `wait_for`.** These had been declared
  only on `@task.sfn`'s explicit signature, so every other task type raised
  `TypeError` on an asset kwarg. That was an oversight that violated *this ADR's
  own rule* — "adding a common parameter = add it to `CommonTaskKwargs`, never
  per-decorator." Assets are correctness-generic below the decorator layer
  (`_create_task`, the `Task` fields, and `generators.py` all handle
  `outlets`/`inlets`/`wait_for` without branching on `task_type`), so they are
  common parameters by nature. Moving them into `CommonTaskKwargs` makes assets
  available uniformly on all seven task types via `**common`.

**Current common set:** `task_id`, `role`, `wait_before`, `retries`,
`retry_delay`, `retry_exponential_backoff`, `retry_jitter`, `max_retry_delay`,
`execution_timeout`, `orchestration_timeout`, `trigger_rule`, `skip_on_backfill`,
`outlets`, `inlets`, `wait_for`.

**Guard against recurrence.** Two tests in `tests/sdk/test_run_task_template.py`
enforce the pattern structurally, so a future task type cannot silently drop it:
`test_every_task_decorator_accepts_common_kwargs` fails if any `@task.*` decorator
omits `**common`, and `test_all_task_types_wire_assets` proves every type lands
`outlets`/`inlets`/`wait_for` on the resulting `Task`. Assets themselves remain
experimental at the API-surface level — see
[`EXPERIMENTAL_ASSETS.md`](EXPERIMENTAL_ASSETS.md).
