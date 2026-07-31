# Assets — experimental scaffolding & graduation checklist

Assets shipped as an **experimental** feature in **v0.93.0**. They work end to
end (define, produce via `outlets`, consume via `inlets`/`wait_for`,
asset-triggered `schedule`) on **every task type** — `outlets`/`inlets`/`wait_for`
are common task params (ADR #109), so `sfn`, `lambda_`, `glue`, `ecs`, `athena`,
`emr`, and `batch` all accept them uniformly. What's not yet frozen is the **API
shape**, and the OSS build has **no visual asset console** yet.

The remaining temporary scaffolding is tagged with a single greppable token:

```bash
grep -rn "EXPERIMENTAL-ASSETS" polyris/ docs/ README.md
```

## Graduation checklist (when the asset API is frozen / production-ready)

1. **Runtime warning** — `polyris/assets.py`
   Remove the `ExperimentalWarning` class, the module-level `_EXPERIMENTAL_WARNED`
   flag, and the `warnings.warn(...)` call in `Asset.__init__`. Drop
   `ExperimentalWarning` from the package exports in `polyris/__init__.py` and
   from `assets.py`'s docstrings.

2. **Doc banners** — `README.md`, `docs/features/ASSETS.md`,
   `docs/features/ASSET_PULL_FEATURE.md`, `docs/getting-started/TUTORIAL.md`
   Remove the `⚠️ Experimental` banners (each ends with an
   `<!-- EXPERIMENTAL-ASSETS ... -->` comment).

That's it — two buckets, both tied to API stabilization. When the last
`EXPERIMENTAL-ASSETS` marker is gone, delete this file.

## Not on this list (deliberately)

- **Task-type coverage is done.** Assets are wired through the shared
  `CommonTaskKwargs` / `_create_task` path, not per-decorator, so every task type
  (current and future) gets them automatically. There is no "add assets to glue/
  ecs/…" work left.
- **The asset tests are permanent, not experimental.**
  `test_every_task_decorator_accepts_common_kwargs` and
  `test_all_task_types_wire_assets` (in `tests/sdk/test_run_task_template.py`)
  guard the ADR #109 single-source-of-truth contract — a new task type that
  forgets `**common` fails them. Keep them after graduation.
