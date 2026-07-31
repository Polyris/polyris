# Spike: Asset-trigger dedup is silently daily-only, regardless of schedule

**Question.** A pipeline confirmed working: run `orders-clean` → `orders-analytics`
auto-triggers (push model verified live, ADR on `asset_subscriptions_table` fix). Running
`orders-clean` a **second time the same day** did not re-trigger `orders-analytics`. Is
that the right behavior — and does it generalize correctly to producers that run more
than once a day *by design* (hourly, every-15-minutes), not just to an accidental
manual re-run?

**Bottom line.** The "once per day" behavior is intentional-looking but is actually an
**unannounced side effect** of how the AND/OR dedup key is built, not a deliberate
granularity decision. It is calendar-date-only, unconditionally, regardless of the
producer's actual schedule. A genuinely hourly asset-triggered pipeline is affected by
the *same* mechanism, not a variant of it: today's default configuration will silently
process only the **first** of each day's hourly materializations and drop the other 23,
with no error anywhere. This is a correctness gap for a real, unremarkable use case
(hourly ETL feeding an hourly-or-faster consumer), not just an edge case of "I manually
reran the producer."

## How the dedup key is actually built

`notify_asset_consumers`'s AND-path (`Record_Asset_For_AND`) writes to
`queued_asset_events_table` keyed on:

```
dag_date = <consumer_pipeline_name> & '-' & <execution_date>
```

`execution_date` is not computed by the asset-trigger machinery — it is whatever the
**producer's** own `date` field happened to resolve to at wrapper-input time
(`run_task/sfn.tpl.json`: `'execution_date': $states.context.Execution.Input.date`).
That `date` field is `JSONATA_DATE` (`polyris/generators.py`), whose fallback chain is:

```
variables.current_date → input.current_date → $substringBefore($now(), 'T')
```

The last link is the one that fires for any pipeline that hasn't explicitly set a
`current_date` variable — and it always truncates to the **calendar date**, dropping
the time-of-day entirely, regardless of whether the producer's `schedule` is
`@daily`, `@hourly`, or `rate(15 minutes)`. Nothing in the DSL, the generator, or the
registration flow reads the producer's `schedule` string to decide what granularity
`execution_date` should carry. Two runs of an hourly producer on the same calendar day
produce the exact same `dag_date` string, so the second (and third, ... twenty-fourth)
`Record_Asset_For_AND` write hits the *same* conditional-write collision as an
accidental duplicate, and is silently treated as one.

**Consequence, concretely:** an hourly pipeline with `outlets=[some_asset]` feeding a
consumer with `schedule=[some_asset]` will fire the consumer once, on the producer's
first run of the day, and never again until the next calendar day — with zero errors,
zero warnings, and a UI that shows the producer running green 24 times a day while the
consumer quietly does nothing after run #1. This is worse than the "explicit re-run"
case: it is the *default*, expected operating mode for a legitimate schedule shape, not
a one-off human action.

## What already exists to build on

`polyris/granularity.py`'s `infer_cron_cadence()` (ADR #52, built for backfill partition
expansion) already solves the *recognition* half of this problem: given a cron/rate
string, it returns `"hourly" | "daily" | "weekly" | "monthly" | None`. It was written
for a different consumer (backfill date-range expansion) but the inference logic itself
is exactly the input a granularity-aware dedup key would need. Nothing currently wires
it into the asset-trigger path.

## How other orchestrators handle this (for calibration, not imitation)

Both default to the *opposite* extreme — react to every individual materialization
event, no date-level dedup at all — and both have real, documented pain from that
choice:

- **Dataset-based orchestrators:** a single producer run can emit multiple dataset events, and
  some do not reliably process all of them (4 events emitted,
  only some ever reached the consumer). Separately, users have asked for a way
  to reset/day-scope dataset-triggered counting because *lack* of day-scoping breaks
  their AND-across-two-producers case when one producer is manually rerun
  (the exact inverse of what we're looking at: they want what we
  have by default, and don't have a supported way to get it).
- **Dagster Auto-Materialize (eager policy):** re-materializes downstream on every
  upstream materialization by design, which one user described as triggering "lots of
  expensive (and mostly unnecessary) runs" (dagster-io/dagster#20943). Dagster's own team
  moved off the eager `AutoMaterializePolicy` toward `AutomationCondition` partly because
  of exactly this class of "confusing results."

Neither competitor's default is obviously better — both are event-scoped (no date
collapsing) and both have paid for it in lost/duplicate/excessive triggers. polyris's
day-scoped default avoids their specific failure modes; the tradeoff surfaced here is
that day-scoping needs to track the *producer's actual cadence*, not always calendar-day,
which today it does not.

## Options

**A — Leave as calendar-day-only; document the limitation.**
Zero code risk. Correct for the common case (daily-or-slower producers, which covers
every current example — `11_assets_outlets_inlets` through `14_assets_and_or` are all `@daily`).
Wrong by default for any hourly-or-faster asset-producing pipeline, silently. Requires
every such user to discover and manually override `current_date` with an hour-aware
expression themselves — undocumented today, and easy to miss until data quietly stops
flowing downstream.

**B — Derive dedup granularity from the producer's schedule automatically.**
Use `infer_cron_cadence(dag.schedule)` at generation/registration time to pick the
`execution_date` format for a pipeline with outlets: `YYYY-MM-DD` for daily-or-slower,
`YYYY-MM-DD-HH` for hourly, etc. Fixes the silent-drop case with no user action required.
Open design questions, not yet resolved by this spike:
  - `infer_cron_cadence` returns `None` for irregular cron (its own doc: "ambiguous cron
    — caller falls back to daily with warning surfaced in backfill preview"). The same
    fallback-with-warning pattern could apply here, but "surfaced in backfill preview"
    doesn't exist for the trigger path — needs its own surfacing (deploy-time warning?
    UI badge?).
  - Multi-asset AND (`14_assets_and_or`'s `combined-report`) can depend on producers with
    *different* cadences (one daily, one hourly). What does "the same materialization
    batch" mean when the AND-counter's `dag_date` key is keyed by the *consumer's* name,
    but must somehow reconcile two different producer granularities writing into it?
    Needs a decision, not an inference — likely the coarsest of the required assets'
    granularities, but that's a product call, not a technical default.
  - Is this the producer's schedule granularity, or the *consumer's*? They can differ
    (hourly producer, daily-scheduled consumer that only cares about "today's" batch as
    a whole) — plausibly both are legitimate use cases wanting different behavior.

**C — Make the granularity an explicit, first-class DSL parameter instead of inferring it.**
E.g. something like `outlets=[my_asset]` gaining an optional partition-granularity hint,
or documenting that sub-daily asset producers must set
`variables={"current_date": "<hour-aware expr>"}` explicitly, with a worked example.
Puts the decision in the user's hands rather than guessing from cron syntax (which is
inherently ambiguous for some patterns, per `infer_cron_cadence`'s own `None` case) at
the cost of needing to know to do it — same discoverability problem as Option A unless
paired with real documentation and probably a deploy-time check that warns when a
sub-daily-scheduled pipeline has `outlets` and no explicit granularity-aware
`current_date`.

## Not resolved by this spike

Which option to take, and if B, how to resolve the two open design questions above
(irregular-cron fallback surfacing; multi-producer mixed-granularity AND semantics).
This needs a product decision (Principle #6) before any code — the current examples
(`11`–`14`) are all daily and are not affected either way, so there is no release-blocking
urgency, but it is a real, silent-failure-shaped gap for the first person who builds an
hourly asset-triggered pipeline against this engine.
