# ADR #97 — Explicit plugin route registration (open-core seam)

> **Status:** Accepted — implemented in v0.91.x (console_api). All route modules
> self-register; `main.py` is the runner over an explicit module list. Behaviour-
> preserving: the route table is unchanged (57 routes), dispatch is unchanged.

## Context

The open-core split needs a way to decide **what is in a build** without cutting
or stripping code. The prior approach extracted a public subset with a publish
script (an `ee/`-strip). That is surgery layered on top of a monolith.

The console API was a monolith: `console_api/main.py` held one flat literal
`ROUTES = {(METHOD, path): (handler, param_key)}` (57 entries) and statically
imported every handler. There was no way for a module — let alone a separate
proprietary package — to contribute routes; the central dict had to name them.
The UI (filesystem routing) and the SDK adapters (static `from . import ...`) are
monolithic in the same way. None of the codebase supported component
self-registration.

A team discussion (with Myroslav) landed on a component/plugin model: a skeleton
with extension points; components register themselves; an **explicit** list of
plugin modules drives a build; proprietary components live in a separate module.
Explicit over implicit (package-discovery) because explicit is easier to read,
order, and reason about. This is the classic registry pattern (ZCA-era); the
modern equivalents are `importlib.metadata` entry points, Protocols, and DI.

## Decision

Introduce a small **explicit route registry** (`console_api/routing.py`):

- `Router` collects `(METHOD, path) -> (handler, param_key)` entries.
- A route module exposes `register(router)` and adds its own routes there.
- `main.py` is the **runner**: it holds an explicit list of route modules and
  calls `register` on each. `ROUTES` becomes `router.table` — the same
  introspection surface and the same dispatch as before.

The open-core split then rides on this seam: open-core builds register the
open-core route modules; a proprietary build adds the modules shipped in the
proprietary package to the list. **What is registered determines the API
surface.** Moving a route between tiers = moving its module between the open and
proprietary registration lists — not cutting files.

Registration is explicit (a listed set of modules), **not** implicit package
discovery (`entry_points` / walking installed packages). Implicit remains a
possible future layer on top, but the default and source of truth is the explicit
list.

The same pattern (registry + explicit manifest) applies to the UI; the SDK
adapters get a named-adapter registry. Those are separate stages.

### Scope boundary

This pattern is for **plugin boundaries** — where a set of extensions must be
switchable per build (routes, UI features, adapters), especially across the
open-core / proprietary line. It is **not** a general coding style: ordinary code
uses plain functions and direct imports. A registry where a plain call would do is
over-engineering and violates Principle #12 (maximize reuse, don't go custom
without need).

## Alternatives considered

- **Decorator registry** (`@route(...)` registering as an import side effect).
  Rejected as the primary form: registration hidden in import side effects is
  harder to debug than an explicit `register(router)` call, which is what the
  team discussion favoured.
- **`importlib.metadata` entry points** (implicit, per-package). The standard
  modern mechanism, but implicit — what is installed activates. Kept as an
  optional future layer; explicit is the chosen default.
- **Pluggy** (pytest's plugin engine). Mature, but built for many extension
  points and third-party authors. Overkill here (single author, few points) and
  a dependency for what a small registry does — against Principle #12.
- **Keep the `ee/`-strip publish script.** Rejected: surgery on a monolith. With
  self-registration the split is configuration, not extraction.

## Consequences

- Adding/moving a route = touch its module, not `main.py`.
- The open-core / proprietary boundary becomes an explicit registration list.
- The central `ROUTES` literal is gone — each module owns its routes via
  `register`. The transitional `Router.load(...)` helper was removed once every
  module was migrated.
