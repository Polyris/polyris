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
