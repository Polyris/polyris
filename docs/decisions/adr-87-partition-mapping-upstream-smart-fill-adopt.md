# ADR #87 — Partition mapping + upstream smart-fill — adopt Dagster's model as prior art

#### Status

Accepted (concept). Fixes the architecture for partition-level upstream
dependencies and the upstream smart-fill backfill behavior. Detailed
API/SFN implementation deferred to its own follow-up when the work starts;
this ADR locks the *shape* so we don't paint ourselves into a corner.

#### Context

ADR #86 made the asset the product's primary lens and named upstream
smart-fill (build missing dependencies for a date, reuse existing ones) as
the one genuinely new backfill behavior. Designing it surfaced two cases
that the existing single-granularity range expansion (`_floor_to_bucket` /
`_advance` in `partitions.py`) does not cover:

1. **Cross-granularity** — a daily asset depends on an hourly upstream:
   one daily partition for date D corresponds to 24 hourly partitions.
2. **Rolling window** — asset_1 for date D needs asset_2 for a window
   (e.g. the prior 7 days, D-6..D), not just D.

The team's real-world distribution: same-period 1↔1 is the dominant
pattern; cross-granularity and rolling windows are valid but less common.
Crucially, rolling windows are *already* handled at runtime today — a
task's code reads whatever range it needs from storage
(`WHERE date BETWEEN ...`); the orchestrator is not involved. The window
only matters to the orchestrator in one narrow situation: a backfill of a
gap where several consecutive upstream partitions are missing, where a
naive "check only date D" smart-fill would see the dependency as satisfied
and silently produce wrong data.

#### Decision

Adopt Dagster's `PartitionMapping` model as prior art. It is the proven,
field-tested abstraction for exactly these cases, and inventing a worse
one would violate the project's "check comparable projects before building
custom" discipline (CLAUDE.md #12). We mirror its shape, not its code.

The core abstraction is a single function — conceptually
`partitions_needed(asset, D)` — that returns the set of upstream partitions
a target partition depends on. Both problem cases are special cases of this
one function, not separate mechanisms:

- **Default = intersecting time window.** A target partition depends on all
  upstream partitions whose time window intersects its own. This single
  default covers:
  - same-granularity 1↔1 (windows coincide on one partition) — the
    dominant case, zero config;
  - cross-granularity (daily←hourly maps to the 24 covering hourly
    partitions) — also zero config.
- **Override = offset.** A rolling window is expressed as an explicit
  offset on the dependency (Dagster's `start_offset` / `end_offset`;
  Polyris surface TBD, e.g. `inlets=[asset2.offset(days=-6)]`). Opt-in,
  rare, does not complicate the default path.
- **Boundary case = allow-nonexistent.** When mapped upstream partitions
  fall outside the upstream's `partition_start`, do not hard-fail; return
  the in-range subset and warn (mirrors Dagster's
  `allow_nonexistent_upstream_partitions`).

The upstream smart-fill resolver consumes only `partitions_needed(asset, D)`
— it has no separate notion of "window" vs "granularity". It walks the
dependency graph upward, asks the mapping for each edge, checks existence
of each required partition (reusing `_scan_completed_partitions` /
skip-completed logic), and builds the missing ones. Two fill modes (from
ADR #86): `smart` (reuse existing upstream partitions, build only missing)
and `force` (rebuild all upstream regardless).

#### Why this is the right architecture

- One abstraction instead of three special cases (CLAUDE.md #2 — define
  once). The resolver is mapping-agnostic.
- Proven design — Dagster has run this in production for years; we are not
  pioneering a partition-mapping semantics.
- The default covers the dominant pattern (1↔1) *and* cross-granularity at
  zero user cost; only rolling windows need explicit opt-in.
- Forward-compatible: designing `partitions_needed` with the intersecting-
  window default *now* means adding offset overrides later is additive, not
  a breaking re-design. We avoid the corner where 1↔1-only would have to be
  torn up to admit windows.
- Reuses existing machinery: `_floor_to_bucket` / `_advance` already do
  bucket math; existence checks reuse skip-completed; `_find_producers_for_
  asset` already resolves producers.

#### Scope / phasing

- **Phase 1 (when smart-fill work starts):** implement
  `partitions_needed(asset, D)` with the intersecting-window default
  (covers 1↔1 + cross-granularity), the upward graph walk with cycle
  detection + diamond dedup, existence check, and missing-partition build.
  Same-granularity and cross-granularity both work; rolling windows fall
  back to the 1↔1 default with an explicit "window not yet honored at
  orchestration layer" warning if an offset is declared but not yet
  implemented.
- **Phase 2 (when a real rolling-window case needs it):** add the offset
  override DSL surface + its expansion. Additive; the resolver signature
  does not change.

#### Consequences

- The "will granularity / windows cope" question is closed: yes, via one
  mapping abstraction, with the dominant cases free and rolling windows as
  an additive override.
- No silent-wrong-data risk: mixed-granularity and (eventually) windowed
  dependencies resolve to the full covering set; boundary shortfalls warn
  rather than lie.
- Remaining edge-case decisions for the smart-fill implementation ADR:
  stale-vs-reuse rule (ADR #86 leaning: reuse if exists = `smart` mode;
  `force` to rebuild), backfill-vs-scheduled conflict policy, future-date
  validation, max_parallel throttle limit, mark-partition-done (lift
  task-level `mark_success` to partition scope).

#### CLAUDE.md alignment

#2 no duplication (one mapping function, not per-case branches), #6
alignment before implementation (this ADR), #7 ADR for an architectural
decision, #12 maximize reuse (adopt proven Dagster shape; reuse bucket
math, skip-completed, producer resolution).
