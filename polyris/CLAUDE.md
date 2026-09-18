# polyris/CLAUDE.md — SDK coding rules

Rules specific to the `polyris/` SDK package. Apply on top of the root `CLAUDE.md`.
Each rule is here because we shipped a bug that violated it.

---

## Context managers that push/pop shared state must save and restore, never just clear

Any `__enter__` that overwrites shared state (e.g. `dag._current_task_group`) must
first save the *previous* value into `self` (e.g. `self.parent_group = dag._current_task_group`),
and `__exit__` must restore it (`dag._current_task_group = self.parent_group`).
Setting to `None` (or any fixed value) unconditionally breaks nesting: an inner context
manager wipes the outer group's state, so tasks written after the inner block have no group.
The field used for saving (`parent_group`) must be populated in `__enter__`, not just declared.

## Deploy file scanning: filter by locality, not by name; non-zero exits are errors

`_load_dag_from_file` must return only DAGs **defined in the loaded file**, never ones
merely imported into it. Use `sys.modules` after `exec_module` to build a set of all
DAG object IDs known in other modules; exclude those from the result. The `obj.__module__`
attribute does **not** work for this — DAG instances always carry `__module__ == "polyris.dag"`
regardless of where they were instantiated.

`except SystemExit: pass` silently swallows a pipeline file calling `sys.exit(1)`, hiding
load errors. Re-raise non-zero exits: `if code != 0: sys.exit(1)`. Tolerate `exit(0)`.

`_discover_dags_in_dir` must deduplicate by `dag_id`: if two files in the same directory
define the same `dag_id`, keep the first and warn about the duplicate. Silent double-deploy
would run the pipeline twice in the same batch.

## Graph objects: always `@dataclass(eq=False)`

`Task`, `DAG`, and every `Step` subclass must carry `eq=False`.

A plain `@dataclass` generates value-based `__eq__` and sets `__hash__ = None`.
This breaks two things silently:

- `if x not in deps` uses value equality, so `Pass() == Pass()` is `True` — distinct
  graph edges are discarded with no error.
- Objects are unhashable — they cannot be used in sets or as dict keys.

`eq=False` restores identity-based `__eq__` and `__hash__`, which is what graph
membership checks require.

## Duplicate node IDs must be rejected at registration

`DAG.add_task` and `TaskGroup.add_task` raise `ValueError` when a second node with
the same `task_id` is added. Silent append causes topological sort to collapse both
nodes into one, making one task disappear from the execution graph.

`TaskGroup` must run the duplicate check **after** applying the group prefix
(`group_id.task_id`), not on the bare `task_id`.

## `assert` is not validation — use `raise`

`assert name is not None` is stripped by `python -O`. Any check that guards a
user-facing invariant must use an explicit `raise ValueError(...)` or
`raise TypeError(...)`. `assert` is only acceptable for internal invariants that
can never be violated through the public API.

## Decorator is the sole validation boundary

All constraint checks for a task type (required fields, type enforcement, enum
values, cross-parameter exclusivity) belong in the task decorator in `task.py`.
Generators trust what they receive.

Consequences:
- Never use `int()` / `float()` casts in generators to coerce a wrong type.
  `int(0.0625) = 0` silently produces an invalid value that passes AWS type
  validation but has wrong semantics.
- Validate the type explicitly with `isinstance()` in the decorator; raise with
  a clear message pointing to the correct parameter.
- Enum-like string parameters (`"FARGATE"/"EC2"`, `"ENABLED"/"DISABLED"`) are
  validated against a `frozenset` at the decorator level, not in the generator or
  the SFN template.

## Unknown values in dispatch branches → raise, never silently default

Any `if/elif` chain that dispatches on a known set of values (DynamoDB operation,
S3 operation, Glue capacity model) must end with an `else: raise`. A silent
fall-through or a default that "kind of works" produces wrong AWS API behavior
with no error visible to the user.

## `ExecutionAlreadyExists` = success for idempotent SFN starts

When starting a Step Functions execution for registration or one-shot tasks, catch
`ExecutionAlreadyExists` explicitly and treat it as success (`pass`). A concurrent
deploy already completed the work; this call is a no-op. Re-raise all other
`ClientError` codes. Never use a bare `except Exception` here.

## CLI commands: accumulate failures, then `sys.exit`

A CLI command that validates a file or runs tests must:
1. Collect **all** failures before exiting — never `sys.exit(1)` on the first
   failure, which hides the rest.
2. Return a `bool` (or accumulate into a list) from the validation function.
3. Call `sys.exit(0 if success else 1)` in `main()` based on that result.

Returning `None` silently on failure causes the caller to treat failure as success.

## `TaskConfigKey` is the single source of truth for the SDK→SFN contract

Any AWS parameter that flows from the SDK through `task_config` to the SFN
template must be declared in `TaskConfigKey` (in `constants.py`) **first**.

After adding a member: run `python -m polyris.codegen.sync_enums` to regenerate
the four downstream files. A bare string literal on either side (generator or
template) fails `tests/sdk/test_task_config_contract.py`.

## New wrapper input fields require three-file updates + snapshot regeneration

Any field that must flow from the SDK through every task execution and be written to
DynamoDB requires changes in **all three** of these places in the same commit:

1. `generators.py` → `_build_wrapper_input` — include the field in the dict returned
   for each task's SFN input.
2. `sam/sfn_templates/dependency_wrapper/sfn.tpl.json` → `Arguments.Input` block of
   the registration_helper call — thread the field through using JSONata so it is
   forwarded to the helper SFN.
3. `sam/sfn_templates/helpers/registration/sfn.tpl.json` → DDB `PutItem` — write the
   field to the `pipeline-tokens` table so it is queryable later.

Missing any one of the three means the field arrives in the SFN execution context but
is never persisted, or is persisted but never populated, or is populated only for
directly-started executions and not for SDK-generated ones.

After changing `_build_wrapper_input`, regenerate snapshots:
```bash
SNAPSHOT_UPDATE=1 python -m pytest tests/sdk/test_asl_snapshots.py tests/sdk/test_asl_snapshots_steps.py
python -m pytest tests/sdk/test_asl_snapshots.py  # verify
```

## Tests pin AWS type contracts, not just values

After adding a parameter that carries a specific AWS type (integer, float, boolean),
write a test that asserts the type, not just the value:

```python
assert isinstance(task_config[TaskConfigKey.MAX_CAPACITY], float)
assert isinstance(task_config[TaskConfigKey.NUMBER_OF_WORKERS], int)
```

Test against `_build_task_config_and_arn` directly — no jsonata dependency needed
for type contract tests.

## Asset name derivation: keep the bucket, never strip it

`Asset("s3://bucket/path/")` must derive `name = "bucket/path"`, not `"path"`.
Stripping everything up to and including the first `/` after `://` means two
different buckets with the same key path produce identical names — they compare
equal, hash equal, and cross-wire in dependency graphs with no error.

The rule: `name = uri.rstrip('/').split('://')[-1]` and stop. Do not call
`.split('/', 1)[1]` or any equivalent that discards the bucket component.

## AssetAlias in a multi-item list wraps in AssetAny, never extends

In `normalize_asset_schedule`, when an `AssetAlias` appears inside a list with
other items, append `AssetAny(assets=item.assets)` to the outer `AssetAll`.
Never call `assets.extend(item.assets)`.

Why: `AssetAlias` semantics are OR — the pipeline triggers when ANY member fires.
`extend` flattens the alias assets into the outer AND list, silently inverting
the operator: `[alias, a]` becomes `AssetAll([b, c, a])` instead of
`AssetAll([AssetAny([b, c]), a])`.

## EventBridge patterns use `_flatten_asset_names`, never `asset_names`

`AssetAll.asset_names` formats nested `AssetAny` groups as display strings like
`"(ns/b | ns/c)"`. EventBridge event pattern matching requires exact string
values — those display strings will never match a real `asset_name` field.

Use `_flatten_asset_names(node)` (from `polyris/assets.py`) wherever a flat list
of leaf asset name strings is needed: EventBridge patterns, SFN Map `Items` for
subscription registration. `asset_names` is for display/repr only.

## `_create_task` parameters that support `default_args` must default to `None`

Any parameter in `_create_task` that should fall back to `default_args` when not
explicitly passed **must** have `Optional[...] = None` as its signature default —
never a concrete value like `0`, `False`, or `"all_success"`.

A non-None default makes `param is not None` always `True`, so the
`default_args.get(...)` fallback is never reached — the user's DAG-level default is
silently ignored with no error.

The pattern to follow:

```python
# Signature — None means "caller did not pass this"
trigger_rule: Optional[TriggerRuleLiteral] = None,

# Task constructor — fall back to default_args, then the hardcoded default
trigger_rule=(
    trigger_rule if trigger_rule is not None
    else default_args.get('trigger_rule', 'all_success')
),
```

Fields affected historically: `trigger_rule`, `wait_before`, `skip_on_backfill`
(fixed in PR #22). Apply this pattern to every new `_create_task` parameter whose
value a user might want to set via `DAG(default_args={...})`.

## `@dag` decorator must forward `**kwargs` to `DAG()`

The `dag()` function in `helpers.py` accepts `**kwargs` to stay compatible with
`DAG` fields added in the future. Every call to `DAG(...)` inside its `wrapper`
must end with `**kwargs` — omitting it silently drops any field not listed
explicitly in the decorator signature (e.g. `group`, `variables`, `doc_md`,
`default_timeout`). The user sees no error; the field just resets to its
dataclass default.

## All DynamoDB write states in `_build_registration_chain` must carry Retry + Catch

`Register_Pipeline`, `Save_DAG_Snapshot`, and `WriteSubscription` (inside
`Register_Asset_Subscriptions` Map) are all DynamoDB `putItem` states.
A `ThrottlingException` on any of them aborts the entire registration flow
before tasks start and, without a `_notify_warn_` Catch path, the failure is
invisible in the UI.

Every DynamoDB SDK integration state in `_build_registration_chain` must:
1. Carry `_STANDARD_DYNAMODB_RETRY` in its `Retry` block.
2. Carry a `Catch` that routes to its corresponding `Warn_*` state, which
   writes a `_notify_warn_` record to `tokens_table` and then continues
   to the next registration step rather than failing the execution.

`_STANDARD_DYNAMODB_RETRY` (a module-level constant in `generators.py`) is the
single shared definition — don't inline a one-off retry dict.

## Wrapper step retry policy lives in `_add_retry_config`, not duplicated inline

`_build_task_config_and_arn` (Task path) and `_build_step_branch` (wrapper-step
path) must both call `_add_retry_config` to thread the retry contract into
`task_config`. Never inline the `if task.retries: task_config[...] = ...` block
again — that's the duplication that caused `LambdaTask(retries=3)` used as a
wrapper step to silently receive zero retries.

When adding new retry-related fields to `task_config` (new `TaskConfigKey`
members): update `_add_retry_config` only, and both paths pick up the change
automatically.

## DDB `REMOVE` clauses target top-level attributes only — nested-JSON keys are silent no-ops

DynamoDB's `UpdateExpression` `REMOVE #a, #b, #c` operates on **top-level item
attributes**, not on keys nested inside a JSON string stored in one attribute.
If the SFN template Marshall stringifies a `result` dict and stores it in a
single `result` attribute, `REMOVE #manually_resolved` (aimed at
`result._manually_resolved`) does nothing — the field is a JSON key inside a
serialized string, not a DDB attribute. Item passes through untouched, no error,
no log, and the reset-on-rerun contract the REMOVE was meant to enforce silently
fails.

**Why:** hit in 1.0.0 wrapper Init_Output_Row — the reset-on-rerun REMOVE
clause listed `_manually_resolved`, `_operator`, `_reason`, `_pipeline_execution`,
`_resolution`. All five are fields the console_api marker writer stores as
top-level DDB attributes on the canonical row (correct), and the same names
also appear inside `result` on some code paths (misleading). The wrapper's
REMOVE targeted the top-level marker fields — the fix was to prune the list
to fields that are actually top-level on the row the wrapper writes
(`push_count` was the only one that fit that shape after review).

**How to apply:** for every attribute name in a `REMOVE` clause, verify it is
a top-level DDB attribute on the item being updated — not a JSON key inside a
stringified blob. If you want to strip a key from inside a JSON blob, that's
a read-modify-write on the client side, not a DDB `REMOVE`. When the same name
exists at both levels (top attribute AND JSON key), the REMOVE only touches the
attribute — document that explicitly in a comment so the next reader doesn't
assume symmetry.

## Task-config string fields are passed verbatim — no Jinja / JSONata templating

`query_string` (Athena), `batch_parameters` (Batch), and other string-valued
task-config fields are forwarded to the AWS service call literally. There is
no runtime templating layer — a `{{ variable }}` (Airflow-style Jinja) or
`{% $states.input.variables.x %}` (JSONata) placeholder in the string reaches
the service verbatim and either breaks the SQL parser (Athena:
`InvalidRequestException — line 1:N: mismatched input '{'`) or is treated as
a literal parameter value (Batch).

**Why:** the SDK docstring for `@task.athena_query` shows
`query_string="SELECT * FROM sales WHERE date = '{{ ds }}'"` as an example —
which is misleading. That syntax is Airflow's, and polyris doesn't inherit it.
Hit live in 1.0.0 smoke-testing pipeline-17 (`summary_athena`) — the query
`WHERE silver_count > {{ variables.silver_threshold }}` failed at position 85
with `mismatched input '>'` because the `{` bytes went straight to Athena.

**How to apply:** interpolate at DAG-definition time using plain Python
f-strings or `.format()`, not template syntax. If the value must vary at
runtime (per-run date, upstream xcom output), route it through
`variables=` on the DAG (visible in the Console, still constant per-run) or
wrap the service call in a Lambda that does the substitution before invoking
Athena/Batch. Never rely on `{{ }}` or `{% %}` inside `query_string` /
`batch_parameters` / similar until the SDK grows a real templating layer.

```python
# WRONG — sent to Athena verbatim, fails with InvalidRequestException
@task.athena_query(
    query_string="SELECT ... WHERE x > {{ variables.threshold }}",
    ...
)

# RIGHT — interpolated at DAG-build time
THRESHOLD = 100
@task.athena_query(
    query_string=f"SELECT ... WHERE x > {THRESHOLD}",
    ...
)
```

The SDK docstring for `@task.athena_query` should either be updated to reflect
this or the SDK should grow a real Jinja layer that renders task-config strings
at compile time. Both are follow-ups.

## Glue Python install: pythonshell = `--extra-py-files` (S3 wheel only), Spark = also wheel — `git+URL` doesn't work reliably

AWS Glue's Python-install mechanisms are subtly different from stock pip and
have version-specific quirks. The safe rule: **install polyris (or any custom
package) into Glue jobs via an S3 wheel referenced by `--extra-py-files`.**

**Details:**

- **`--extra-py-files`** (accepted by both pythonshell and glueetl/Spark) — S3
  URI pointing to `.whl`, `.egg`, or `.py`. **Works reliably.** Wheels don't
  enforce `requires-python` from `pyproject.toml` when built by our custom
  builder (which omits that metadata), so this also side-steps Glue-version
  vs Python-version mismatches. This is the pattern all our Glue jobs
  (`XcomAggregateGlueJob`, `XcomAllAggregatePyshellGlueJob`,
  `XcomAllTransformSparkGlueJob`) use.
- **`--additional-python-modules`** (glueetl-only) — comma-separated list of
  pip requirements. Documented as "no spaces allowed in the value". PEP 508
  direct-URL syntax (`polyris @ git+https://...`) fails at LAUNCH ERROR with
  `Invalid requirement: '@'` because Glue tokenizes on whitespace and pip
  sees a bare `@`. Compact form (`polyris@git+...` no spaces) is also
  unreliable — not officially supported syntax. Plain PyPI names
  (`polyris==1.0.0`) work if the package is published, but even then Glue
   5.0's pip may fail on `requires-python` mismatches. Best used only for
  well-behaved PyPI packages, never for git URLs.
- **VCS URLs in general** — Glue does not officially support git/hg/svn URLs
  in either mechanism. Anecdotal reports of `--additional-python-modules`
  accepting them exist, but hit our tokenization bug in 1.0.0 pipeline-17
  smoke-test.

**How to apply:**
- Any polyris Glue job that needs the SDK: use `--extra-py-files` +
  `XcomPolyrisWheel` (the CFN Custom Resource that builds the wheel from
  the vendored `polyris/` subtree). Reuse this — don't duplicate the builder.
- If you need to install *only PyPI packages* in a Spark job (no polyris),
  `--additional-python-modules pkg==1.0,other==2.0` (no spaces, no VCS) is
  fine.
- ECS/Batch containers CAN use `pip install polyris @ git+...` in their
  bootstrap Command — pip there is stock, no Glue wrapper interposed.
  See `XcomAllComputeTaskDefinition` / `XcomAllRenderJobDefinition`.
- Never mix — Glue always S3 wheel, containers always git+URL (or pre-built
  image), so consistency in each surface is easy to follow.
