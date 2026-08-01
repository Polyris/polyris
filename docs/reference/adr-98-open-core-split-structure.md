# ADR #98 — Open-core split structure (proprietary roots, no symlink)

> **Status:** Accepted — fully implemented. SDK split (`polyris/ai/` →
> `polyris/_ee/ai/`) and backend split both done: 5 clean Team modules, the 5
> mixed modules (function-level), and `tokens` all moved to `console_api/ee/`.
> OSS surface is exactly **16 free routes**; full build is **57**. Builds on the
> registration seam from ADR #97.
>
> **Amended by ADR #100:** the team↔enterprise enforcement deferred here is decided
> there as a *runtime entitlement* (one build, `POLYRIS_TIER`), not a physical
> strip. The free↔paid strip below is unchanged. With the `/api/capabilities`
> infrastructure route added there, the full paid surface is now **58**.

## Context

ADR #97 made console routes self-register via an explicit list, so *what is
registered* determines the API surface. To ship an open-core build we need
proprietary code physically separable so the OSS build can strip it, with two
hard constraints: **no extra symlinks** (the local `unzip` does not preserve
them — we already nurse `console_api/polyris`), and **no `sys.path` collisions**.

Tiers were agreed against the real 57-route surface, on the heuristic
**authoring + basic read = free; operations / observability / intervention =
Team; governance = Enterprise**:

- **Free (16):** health/metrics; pipeline list/status/executions; DAG view;
  pipeline register + run; task/run/execution-tree reads.
- **Team (41):** backfill; notifications; Slack actions; assets (incl. matrix,
  drift, lineage; basic `list_assets` stays free); per-pipeline metrics/logs;
  pipeline pause/restart; task intervention + runtime task-config edit; execution
  intervention; API tokens.
- **Enterprise (0):** RBAC/SSO/MFA/audit — no such routes exist yet.

## Decision

Proprietary code is stripped from the OSS build. It lives in **two roots, one per
runtime**, deliberately given **distinct package names** so they never clash on
`sys.path`:

1. **SDK** — `polyris/_ee/` (a private subpackage of `polyris`). Holds the AI
   assistant at `polyris/_ee/ai/`; the `polyris-ai` entry point targets
   `polyris._ee.ai.cli:main`. It rides along inside the `polyris` package the
   Lambda already imports (via the existing `console_api/polyris` symlink), so it
   needs **no new symlink**.
2. **Backend** — `sam/lambdas/console_api/ee/` (the `ee` top-level package, local
   to the Lambda's CodeUri). Holds the Team route modules. It is already inside
   the deployable directory, so it too needs **no new symlink**.

**Why two names, not two `ee/`.** An earlier draft put the SDK proprietary code in
a repo-root `ee/` and the backend in `console_api/ee/` — two *top-level* `ee`
packages. `import ee` then resolves to whichever parent dir is first on
`sys.path`, and test suites that prepend `console_api` (e.g. `test_templates`,
`tests/backend/conftest`) cannot share a pytest session with the SDK AI test:
`polyris`'s tests want one `ee`, the backend's want the other. Renaming the SDK
side to `polyris._ee` removes the shared name, so the AI test imports
`polyris._ee.ai` and is immune to any `ee` path ordering. A single shared
repo-root `ee/` symlinked into the Lambda would also have worked, but was rejected
to avoid a second symlink.

**Dependency invariant:**
- OSS code never imports proprietary code — neither `polyris` core importing
  `polyris._ee`, nor `console_api` OSS importing `ee`.
- `ee/team/` never imports `ee/enterprise/`.
- Proprietary code may import OSS (e.g. the AI assistant imports `polyris`; a Team
  route imports `dal`/`routes`).

**Backend registration:** `console_api/main.py` registers the OSS route modules
unconditionally; if the `ee` package is importable it appends `ee.MODULES`
(guarded so a foreign `ee` without `MODULES` is ignored rather than fatal). OSS
build → free routes only; full build → 57. Mixed modules (free + Team handlers in
one file: `pipelines_info`, `pipelines_actions`, `tasks`, `executions`, `assets`)
are split at the function level — free handlers stay in `routes/`, Team handlers
move to `ee/`; shared helpers stay OSS and `ee/` imports them.

**Tests follow code:** a proprietary module's tests live under its proprietary
root (`polyris/_ee/tests/`, `console_api/ee/team/tests/`), so stripping the root
removes both the code and its tests — no per-file exclusion list. The Makefile/CI
SDK target runs `polyris/_ee/tests/`; the console_api target runs
`ee/team/tests/`.

## Consequences

- The OSS build strips `polyris/_ee/` and `console_api/ee/`, and drops the
  `polyris-ai` entry point. Two strip paths, no symlinks, no code cut from files.
- Done: SDK (`ai` → `polyris/_ee/ai`, entry point, test relocated) and the full
  backend — clean Team modules (backfill, notifications, slack, matrix, drift),
  the five mixed modules split at the function level (`pipelines_info`,
  `pipelines_actions`, `tasks`, `executions`, `assets`), and `tokens` (clean Team
  module; the `api_tokens_repo` auth check stays in core). Repo-root Team tests
  also relocated under `ee/team/tests/` (`test_stop_restart`, `test_idempotency`,
  and the source-location smoke checks for moved handlers), so the OSS test tree
  references no proprietary code. The route-table contract is tier-split the same
  way: `tests/sdk/test_templates.py` asserts only the free subset (green at both
  16 and 57 routes); `ee/team/tests/test_route_table_ee.py` pins the full 57-route
  surface and runs only when `ee` is present.
- Shared-helper test note: when a Team handler in `ee/` calls a helper that
  stayed OSS (`routes.assets._build_assets_from_pipelines`,
  `routes.tasks.resolve_task_item`), tests patch the helper **where it reads its
  dependency** — i.e. on `routes.<mod>` for the OSS helper, but on `ee.team.<mod>`
  for repos the Team handler uses directly. Patching the wrong module silently
  no-ops.
- Moving a feature between tiers = move its module (or handler) between the OSS
  tree and the proprietary root, and its entry in the registration list.
