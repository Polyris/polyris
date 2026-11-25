# ADR #133 — A run has one date: the date it was started or scheduled for

**Context.** A run's date (`current_date`) keys its task rows, its asset facts and every `wait_for` decision (ADR #130). A run started from the console carries it in its input, and one started by an asset trigger carried it only under `variables`. A scheduled run did not carry it at all: EventBridge Scheduler passed only `triggered_by` and `schedule`, and each task took the day from `$now()` when the pipeline built its input. A run scheduled at 23:59 that started at 00:01 got the next day, and the tasks of a run crossing midnight could get two dates. For a pipeline with `variables=`, `Define_Inputs` passed the missing date on as `null`, which `$exists` counts as present: every task got a `null` date and the run failed. It also dropped `triggered_by`.

**Decision.** A run has one date, the same for every task:

1. the date it was started for: `current_date` in its input, or `variables.current_date`;
2. else the UTC day its schedule fired it for: the schedule's input carries `scheduled_time`, which EventBridge Scheduler fills with `<aws.scheduler.scheduled-time>`;
3. else the UTC day the execution started (`$states.context.Execution.StartTime`).

Never `$now()`. The expressions test values, not presence, so a `null` is no date. `Define_Inputs`, which replaces the input's `variables` with the declared ones, reads the same chain, so a pipeline with `variables=` keeps the date its starter sent. AssetFanout sends the date at the top level as well, as the console does, so a pipeline generated before 1.2 gets it without a redeploy.

**Key design choices.**

*The day it was scheduled for, not the day before.* Airflow dates a run by the start of its data interval, so a daily run at midnight carries the previous day. polyris keeps its convention, the run's own day; only the clock that picks the day changes. Moving to data intervals would shift every date by a day.

*The execution's start time, not the current time.* `StartTime` is one value for every state of a run; `$now()` differs from state to state.

*One field name on both sides.* `generators.SCHEDULED_TIME_FIELD` is written by `deploy.py` into the schedule's input and read by the generated pipeline; `tests/sdk/test_run_date.py` pins the pair (Principle #28).

**Consequences.**
- Pipelines pick this up at their next `polyris-deploy`. Until then, a scheduled pipeline with `variables=` still fails.
- The harness starts a pipeline the way Scheduler does (`World.fire_schedule`, from the generated schedule's input), so the contract runs end to end (B-D1).
