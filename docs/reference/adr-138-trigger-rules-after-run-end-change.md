# ADR #138 — Trigger rules after the run-end change: nine rules over final outcomes

> **Status:** ACCEPTED (2026-11-17; proposed 2026-11-15), revision 7 (2026-11-23: the public `FAILURE_STATUSES` keeps its members, and the engine counts `stopped` as a final failure through its own set; revision 6, 2026-11-17, together with revision 7 of ADR #137: an action on a stopped task counts until a dependant is decided, through the run row's `run_version`). Target release: 1.3.0.
>
> **Supersedes:** ADR #117 and ADR #116; ADR #115, decisions 1 and 4; ADR #114's statement that failure-reactive rules cannot be supported (*Supersedes*, at the end).
>
> **Amends:** ADR #115, decision 5: a skip that the run asked for gets an origin of its own, `run`, and passes like an operator's (§3).
>
> **Related:** ADR #137, whose run end this ADR builds on. It holds the transitions, Restart with dependants, and what a task reads from its upstreams.

## Context

ADR #117 cut the trigger rules from 11 to 5 on one premise: a final failure cancelled the pipeline's `Parallel` before any downstream rule could evaluate. A rule then only ever saw upstreams that had succeeded or been skipped. In that space four names behaved exactly like kept ones, and `one_failed` and `all_failed` could never fire.

ADR #137 removes the premise. A run ends only when every row is final, as in Airflow, Dagster and Prefect, and an operator's Stop is final too. A final failure and a stop now reach the rules of the dependants.

ADR #117's analysis was repeated for the new states, with Airflow 3.4 as the reference, read in its code and its documentation. In that space the names separate again: nine are distinct and reachable (§2). The current code was written for the old space:

- `all_done`, `one_success` and `none_skipped` now run after a final failure, while `docs/features/DSL.md` says that `all_done` runs on the success path only.
- The validation hints give wrong advice. "`none_failed`: use `all_done`, identical in every reachable state" is no longer true.
- `trigger_rules.ALIASES` maps `none_failed` to `all_done` and `none_failed_min_one_success` to `one_success`, so a returning rule would silently run as another one.
- `trigger_rules.count()` counts `stopped` as pending. A dependant of a stopped task would wait until its run's safety limit, a year.
- Two more copies of the rules drift from the engine. The mock runner of `polyris-local` (`polyris/local.py`) records `skipped` where the engine records `upstream_failed`, and runs an unknown rule as `all_success`. The UI's copy (`ui/src/components/TaskDetailModal/helpers.ts`) calls an operator's skip blocking under `all_success`, while the engine runs the task, and still knows `always`.

Other orchestrators answer the question differently. Airflow has named rules, cascades every skip and takes a run's outcome from its leaf tasks. Argo Workflows writes a boolean `depends` expression per task, and Azure Data Factory puts a condition on each dependency edge. Dagster and Prefect have no rules, only data dependencies and hooks. Named rules are enough for 1.3.

## Decision

### 1. Nine rules

A rule reads its task's direct upstreams only, as before. `all_success` stays the default. The first five are today's; the other four return.

| Rule | Runs its task when | Example |
|---|---|---|
| `all_success` (default) | Every upstream succeeded, or was skipped by an operator or by the run | `extract >> transform >> load` |
| `one_success` | One upstream succeeded. The task starts at the first success and does not wait for the others | Two vendors deliver the same prices: process whichever arrives first |
| `all_done` | Every upstream is final, whatever the outcome | Stop the EMR cluster after `load`, also when `load` failed, but not while `load` waits for a decision that may retry it |
| `all_skipped` | Every upstream was skipped | `load_archive` runs when the run skipped `scrape_live` (`skip_tasks`) |
| `none_skipped` | Every upstream is final and none was skipped. Failures pass | A data-quality report on every source, failures included, but not on a run that skipped a source |
| `one_failed` | One upstream failed finally. The task starts at the first final failure | Delete the half-written S3 partition that a failed `load` left |
| `all_failed` | Every upstream failed finally | Both price feeds failed: load yesterday's prices |
| `none_failed` | Every upstream is final and none failed. Skips pass, even when every upstream was skipped | Write the date's `_SUCCESS` marker when nothing failed |
| `none_failed_min_one_success` | Every upstream is final, none failed and at least one succeeded. Skips pass | `report` after `core` and an optional `enrich`, which a rule may skip |

**Rejected names.** `polyris-validate` and `polyris-deploy` reject them, each with its hint. Any other unknown name gets the list of the nine.

| Name | Why not | Hint |
|---|---|---|
| `one_done` | No use found that one of the nine does not cover | `all_done` to wait for every upstream; `one_success` or `one_failed` to react to the first outcome of one kind |
| `all_done_min_one_success` | Airflow added it in 3.1, and its documentation and its code disagree when an upstream was skipped | `one_success`, which starts at the first success instead of waiting for the rest |
| `always` | The task starts at once, so the edge orders nothing. A reader of the DAG, and an `xcom.pull` of that upstream, expect its output to exist | Remove the dependency: a task without upstreams starts with its run |
| `all_done_setup_success` | It is the rule of Airflow's teardown tasks, and polyris has no setup or teardown tasks | `all_done` for a cleanup task |

### 2. What a rule reads

- **Final.** An upstream is final when its status is in `SETTLED_STATUSES`: a terminal status or `stopped`. Every other status is pending, `waiting_decision` included.
- **Final failure.** `failed`, `upstream_failed`, `aborted` and `stopped`: the engine's `transitions.FINAL_FAILURES`, `FAILURE_STATUSES` and `stopped`; the public `FAILURE_STATUSES` keeps its members (ADR #137, *Consequences*). A task fails finally by an operator's Fail, by the decision timeout, by a Stop, by its own rule (`upstream_failed`), or when the engine writes `failed` for it, for example at a wait limit (ADR #137). Only rows from before 1.3 carry `aborted`: nothing writes it any more.
- **No rule fires on a failure that is not final.** A failed task first waits for a human decision (ADR #114), and while it waits it is pending for every rule. `one_failed` therefore runs after the decision, or after the decision timeout (5 hours by default). It is a compensation rule, not an alert: the failure reaches the console bell at once, whatever the date of its run (ADR #137 §6), and the bell stays the alert.
- **When a rule decides.** As today. A rule runs its task as soon as its condition holds: `one_success` at the first success, `one_failed` at the first final failure, every other rule once all its upstreams are final. It skips the task, or makes it `upstream_failed`, only once all its upstreams are final, even when the outcome is already certain (the cells with an arrow below).
- **`skipped` or `upstream_failed`.** As ADR #115 decided, a rule that can no longer be met ends its task `upstream_failed` only when it needs success or no failure and a final failure is present. These rules are `all_success`, `one_success`, `none_failed` and `none_failed_min_one_success`. Otherwise the task is `skipped`, with the rule as the origin of the skip (§3).
- **Unknown names.** An unknown name fails the task at run time, whatever its upstreams' statuses, a task without upstreams included: the dispatcher writes it `failed`, with the cause, instead of running it as `all_success`. Validation rejects such a name before a deploy; the failure at run time covers a pipeline deployed by an SDK that knows a rule this platform does not.
- **`trigger_rules.ALIASES` is deleted.** It would evaluate `none_failed` as `all_done` and `none_failed_min_one_success` as `one_success`.
- **What the task then reads.** A rule reads statuses; the task it runs reads outputs. The task gets each upstream's status from its own run, and an output only from an upstream that succeeded or that an operator or the run skipped (ADR #137 §1). So a task that runs after a final failure (`all_done`, `one_failed`, `none_skipped`) sees that upstream's status, never an older run's output of the same date. Reading that upstream's output through `xcom` raises `XComUpstreamFailedError`. An EMR step is the exception: it gets no environment variables, and so no statuses, a documented limitation (ADR #137 §1).

The rules over two upstreams. S is a success, K a skip by an operator or by the run, F a final failure, and P a status that is not final, `waiting_decision` included. ✅ runs, ⏭ `skipped`, ⛔ `upstream_failed`, ⏳ waits. ⏳→⏭ and ⏳→⛔ wait although the task can no longer run, and give that outcome once P is final.

Each letter stands for several stored values, which every rule reads alike:
- S: `success`, and `succeeded` on rows from before 1.3;
- K: a `skipped` row with the origin `manual` or `run`, and one with no `skip_origin`, which only rows from before 1.3 carry (§3);
- F: `failed`, `upstream_failed` and `stopped`, and `aborted` on rows from before 1.3;
- P: every status that is not final, such as `waiting`, `running` or `waiting_decision`.

| Rule | S,S | S,K | K,K | S,F | F,F | K,F | S,P | F,P | K,P | P,P |
|---|---|---|---|---|---|---|---|---|---|---|
| `all_success` | ✅ | ✅ | ✅ | ⛔ | ⛔ | ⛔ | ⏳ | ⏳→⛔ | ⏳ | ⏳ |
| `one_success` | ✅ | ✅ | ⏭ | ✅ | ⛔ | ⛔ | ✅ | ⏳ | ⏳ | ⏳ |
| `all_done` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⏳ | ⏳ | ⏳ | ⏳ |
| `all_skipped` | ⏭ | ⏭ | ✅ | ⏭ | ⏭ | ⏭ | ⏳→⏭ | ⏳→⏭ | ⏳ | ⏳ |
| `none_skipped` | ✅ | ⏭ | ⏭ | ✅ | ✅ | ⏭ | ⏳ | ⏳ | ⏳→⏭ | ⏳ |
| `one_failed` | ⏭ | ⏭ | ⏭ | ✅ | ✅ | ✅ | ⏳ | ✅ | ⏳ | ⏳ |
| `all_failed` | ⏭ | ⏭ | ⏭ | ⏭ | ✅ | ⏭ | ⏳→⏭ | ⏳ | ⏳→⏭ | ⏳ |
| `none_failed` | ✅ | ✅ | ✅ | ⛔ | ⛔ | ⛔ | ⏳ | ⏳→⛔ | ⏳ | ⏳ |
| `none_failed_min_one_success` | ✅ | ✅ | ⏭ | ⛔ | ⛔ | ⛔ | ⏳ | ⏳→⛔ | ⏳ | ⏳ |

- **A rule's skip.** With a skip by a rule in place of K, only `all_success` changes. It gives ⏭ in the S,K and K,K columns and keeps ⛔ in K,F. In K,P it waits, then gives ⛔ if P fails finally and ⏭ otherwise. Every other rule reads a skip the same, whatever its origin (§3). Airflow skips in the S,K and K,K columns whatever the origin.
- Airflow decides the cells with an arrow at once. The outcome is the same; polyris reaches it later (*Out of scope*).

**No upstream, and three.** A task without upstreams starts with its run under every known rule. Over three upstreams each rule counts the statuses as it does over two. These columns show cases that two upstreams cannot:

| Rule | none | S,K,F | S,K,K | F,F,P |
|---|---|---|---|---|
| `all_success` | ✅ | ⛔ | ✅ | ⏳→⛔ |
| `one_success` | ✅ | ✅ | ✅ | ⏳ |
| `all_done` | ✅ | ✅ | ✅ | ⏳ |
| `all_skipped` | ✅ | ⏭ | ⏭ | ⏳→⏭ |
| `none_skipped` | ✅ | ⏭ | ⏭ | ⏳ |
| `one_failed` | ✅ | ✅ | ⏭ | ✅ |
| `all_failed` | ✅ | ⏭ | ⏭ | ⏳ |
| `none_failed` | ✅ | ⛔ | ✅ | ⏳→⛔ |
| `none_failed_min_one_success` | ✅ | ⛔ | ✅ | ⏳→⛔ |

An unknown rule fails its task in every column of both tables, `none` included.

**Enabling time.** `trigger_rules.enabled_at` gives the time the upstreams first allowed the outcome (ADR #130 §9). It gains a clause for `one_failed`:
- a `one_success` that runs: its earliest success;
- a `one_failed` that runs: its earliest final failure, at the time of the decision or of its timeout, not of the failed work;
- any other outcome: the latest upstream to become final.

### 3. Skips

Every skip records who made it, in `skip_origin`:

| Origin | Who skips | Under `all_success` |
|---|---|---|
| `manual` | An operator, from the console | Passes, as if the task had succeeded |
| `run` | `Start_Run`, for a task named in the run's `skip_tasks` | Passes, as if the task had succeeded |
| `rule` | The dispatcher, because the task's own rule can no longer be met | Cascades: the dependant is skipped too |

- **Only `all_success` reads the origin,** as today. Every other rule reads a skip the same: `one_success` and `none_failed_min_one_success` never count it as a success, `none_skipped` skips its task, and `all_skipped` counts it.
- **The run's skip list is `skip_tasks`,** the one list the engine reads (ADR #60). `Start_Run` does not read `skip_on_backfill`: whoever starts a run names the tasks it skips.
- **Why an operator's or a run's skip passes.** An operator skips a task to let the run go on without it (ADR #115). A run skips the tasks its starter named, typically a task whose output is already there, so that the rest of the pipeline runs on that output (ADR #12). If either cascaded, one skip would silently skip the whole chain below it.
- **Why a rule's skip cascades.** It means that this branch did not apply, for example a fallback that was not needed, so its `all_success` dependants do not apply either. Argo Workflows does the same by default. A bare `depends: "A"` means `A.Succeeded || A.Skipped || A.Daemoned`: a task skipped on purpose lets its dependants run, and a task left out because its own `depends` was false (`Omitted`) does not.
- **Coming from Airflow.** In Airflow every skip cascades under `all_success`, and a join after a branch needs `none_failed_min_one_success`. In polyris only a rule's skip cascades: a task that an operator or the run skipped lets its `all_success` dependants run. To skip a chain by hand, skip each task in it.
- **One constant.** `SkipOrigin` in `polyris/constants.py` holds the three values, and `sync_enums` emits them for the console and the UI (Principles #27 and #28). The console's Skip, `Start_Run` and the dispatcher write them from it. The writers' census, `tests/sdk/test_task_row_writers.py`, fails any write of `skipped` without an origin.
- Only `rule` cascades, so a row skipped before 1.3, which may carry no origin, passes as it did.

### 4. Stop

- **`stopped` is a final failure for every rule,** as `aborted` already is. ADR #137 makes a Stop final for the run; this ADR makes it final for the dependants.
- From the Stop on, the dispatcher decides the stopped task's dependants as after a Fail, at the moments §2 gives: a `one_failed` dependant runs at once, and a rule that the Stop blocks waits until all its upstreams are final. A dependant that needs the task to succeed then becomes `upstream_failed`, and an `all_done` dependant runs.
- Until a dependant is decided, a Restart or Mark Successful of the stopped task still counts for it: a restarted task is pending again, and a task marked successful is a success. Both raise the run row's `run_version`, which every decision checks, so no dependant is decided from a read taken before the action (ADR #137 §1, §3).
- Once a dependant is decided, a Restart or Mark Successful acts on the stopped task alone, and the dependant keeps its status. Restart with dependants (ADR #137 §3) also resets, in the background, the dependants that the Stop decided, those `upstream_failed` or skipped by a rule, so they wait for the task again.
- Recovery after a Stop, a Fail or a decision timeout is therefore one action, as a Restart inside the decision window already was.
- In 1.2 the dependants of a stopped task waited, so a later Restart or Mark Successful led them on.

### 5. Run outcome

- **A run with a final failure fails,** even when a cleanup (`all_done`) or a compensation (`one_failed`) task succeeded after it. Its execution ends `FAILED`.
- The console's status stays ADR #112's derivation over every row (`derive_execution_status`), read from the run row's summary of them (ADR #137 §4), taken once every row is final, with the run row's mark of a failed direct step. It is `failed`, or `aborted` when no row is `failed` and every final failure is a stop, an abort or an `upstream_failed`.
- A compensation or a fallback (`one_failed`, `all_failed`) runs only after a final failure, so its run always fails. A redundant source fails the run too: when one of two vendors fails finally, the run ends failed, although the `one_success` task ran on the other vendor's prices.
- An acceptable failure is a human's decision on the task. Skip the task, or mark it successful, before its failure is final, and the run can succeed.
- **After the run's end.** A Restart after the run has ended reruns its task, and the console's status follows the rows again: once every row is final, it can turn `success`. The run's execution keeps the outcome it ended with, `FAILED`, and so does a `bulk_backfill` that waited for it. A harness scenario pins both.
- After a Stop of the run, a Restart is accepted once the stop is done, when every row is final and the run's answer is marked. No pass stops the restarted task again (ADR #137 §4).
- This differs from Airflow on purpose. Airflow takes a run's state from its leaf tasks, and its documentation warns that a leaf with `all_done` that succeeds marks the run successful "even if something failed in the middle". Airflow's own system tests add a watcher task, with `one_failed`, that raises to fail the run.
- The console and every caller that waits for a run read its outcome. A cleanup that succeeded must not hide a load that failed. An outcome read from every row also stays the same when someone adds a leaf task.

### 6. One source of truth

- **The rules** are pure functions in `polyris/coordination/trigger_rules.py`: `count`, `evaluate` and `enabled_at`. Each clause of each rule, and of `enabled_at`, has its own test (Principle #48).
- **The names.** `TriggerRule` and `TriggerRuleLiteral` in `polyris/constants.py` list the nine. The engine's known rules, the validation and the UI's type derive from them. The rejected names and their hints live next to the rules, where the validation reads them.
- **`polyris-local`** calls `trigger_rules.evaluate` instead of keeping its own copy, and records its verdict: `skipped` or `upstream_failed`.
- **The UI keeps no copy.** The console computes each upstream's verdict with `trigger_rules.evaluate`, from the rows it already loads, and the task detail shows it (ADR #137 §1). The TypeScript copy in `ui/src/components/TaskDetailModal/helpers.ts` is deleted, with the two lines of `docs/operations/UI.md` that describe it. The console's API then needs no `skip_origin` for the UI to judge a skip.
- **One truth table** in `tests/fixtures/`, as `ddl_parity.json` is: rule, upstream statuses with their origins, and verdict. A pytest checks it against `evaluate`. It holds every cell of both tables in §2, once for each stored value that its letters stand for, the cells of `all_success` with a rule's skip, and an unknown rule.

## Consequences

**For the user**, the 1.3 CHANGELOG entries of this ADR, beside those of ADR #137:
- *Breaking*:
  - After a final failure, its dependants' rules decide. An `all_done`, `one_success` or `none_skipped` dependant now runs after a failed upstream; before, the failure ended the run first. The run still ends failed.
  - A Stop is final for the dependants. A `one_failed` dependant runs at once, and an `all_success` dependant becomes `upstream_failed` once its other upstreams have finished. Until then, a Restart or Mark Successful of the stopped task still counts. Before, the dependants waited for the stopped task and followed it.
- *Changed*:
  - A trigger rule that the platform does not know fails its task with the cause; before, the task ran as `all_success`.
  - The validation hints for the rejected rule names change.
  - `polyris-local` evaluates trigger rules with the engine's code, so its mock runs record `upstream_failed` where the engine does.
- *Added*:
  - The rules `one_failed`, `all_failed`, `none_failed` and `none_failed_min_one_success`.
  - Every skipped task records its origin in `skip_origin`: `manual`, `run` or `rule`.

**Steps without a generator.** Decided separately for the same release: `Choice`, `Map`, `Sensor` and `ShortCircuit`, with the `Condition` helper, are deleted (ADR #137 §1). None had a generator, so a DAG that used one failed at generation. Every skip with the origin `rule` therefore comes from a trigger rule.

**Code.**
- `polyris/constants.py`: the nine names, `SkipOrigin`, and comments that no longer cite the `Parallel` premise; `stopped` counts as a failure through `transitions.FINAL_FAILURES`. Then `python -m polyris.codegen.sync_enums`.
- `polyris/coordination/trigger_rules.py`: the four rules, and the larger set of rules that need success or no failure. Final means `SETTLED_STATUSES`, `enabled_at` gains its `one_failed` clause and `ALIASES` goes. An unknown name raises an error before the upstreams are counted, so a task without upstreams gets it too, and the dispatcher writes it into the row.
- `polyris/validation.py`: the four rejected names and their hints replace the six removed ones.
- `polyris/local.py` follows §6, and `ui/src/components/TaskDetailModal/helpers.ts` is deleted. The console computes the verdicts of §6, and writes `SkipOrigin.MANUAL` on Skip, in the action's transaction.

**Tests.**
- The truth table, read by pytest. A test per clause. Validation tests for the nine names, each rejected name and an unknown one. The writers' census for `skip_origin`.
- Scenarios in the coordination harness:
  - a Fail, then an `all_done` cleanup runs and the run ends failed;
  - `one_failed` waits through a decision and runs after the Fail or the timeout;
  - a Stop makes the dependants `upstream_failed`, the run ends, and a later Restart reruns that task alone;
  - a Restart or Mark Successful of a stopped task, before its dependants are decided, leads them on;
  - Restart with dependants after a Fail reruns the task and the dependants that its failure decided, and leaves an operator's and a run's skips as they are;
  - a rule's skip cascades under `all_success`, and an operator's or a run's skip does not;
  - after a run that succeeded, a later run of the same date fails `load`: its `all_done` dependant sees the failure, and `xcom` raises for the output;
  - a run ends failed, then a Restart of its failed task succeeds: the console's status turns `success`, and the execution stays `FAILED`.

**Docs**, in the same change:
- `docs/features/DSL.md`: the nine rules with their examples, final failures, the skip origins with a part for readers who come from Airflow, and the run outcome.
- `docs/architecture/ARCHITECTURE.md`, `docs/architecture/BACKEND.md`, `docs/operations/TROUBLESHOOTING.md`, `docs/features/DATA_PASSING.md` and `docs/tools/LOCAL_TESTING.md`; `docs/operations/UI.md`, which loses its two lines on the UI's copy; the examples `03_fan_in` and `09_trigger_rules`, and `examples/README.md`.
- The status line of `docs/reference/SPIKE_TRIGGER_RULES.md`, and root `CLAUDE.md` Principle #48, whose enabling time gains the `one_failed` clause.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Airflow's full cascade: every skip skips its `all_success` dependants | One skip by an operator, or by a run's `skip_tasks`, would silently skip every task below it. A run skips a task so that the rest of a pipeline runs on the data already there (ADR #12). To let a run go on past a task, an operator would have to mark it successful, which records asset facts for data it never produced. |
| Argo-style expressions: one boolean `depends` expression per task | More than any pipeline needs today (Principle #18): a parser and an evaluator, and validation of arbitrary expressions. The nine names cover the uses in §1. |
| A condition on each edge, as in Azure Data Factory: success, failure, completion or skip, ANDed over the edges | It changes how every edge of the DSL is written, and "any one of" then needs an extra task. A named rule states the condition once per task. |
| Keeping the five rules | With final failures visible, five rules leave out compensation (`one_failed`), the fallback when every source failed (`all_failed`), and the checks that nothing failed (`none_failed`, `none_failed_min_one_success`). |
| Seven rules, without `none_failed` and `all_failed` | "Airflow 2's named rules without `one_done`, `always` and the teardown rule" is one sentence for a user who migrates. Each rule costs one branch and its tests. |
| An unknown name runs as `all_success`, as before | A silent change of behaviour, for a name that validation already treats as an error. |
| A copy of the rules in the UI, checked against the truth table by a vitest | Two copies drifted from the engine before (*Context*). The console already loads the rows, and the engine's own function can judge them. |

## Out of scope

- **Deciding as soon as the outcome cannot change**, in the cells with an arrow in §2. The outcome is the same and comes later; this is deferred polish.

## Supersedes

- **ADR #117**, whole. Its premise is gone (ADR #137), so its groups of names "identical in every reachable state" and its validation hints no longer hold. §1 replaces its rule set and its hints.
- **ADR #115, decisions 1 and 4**: the canonical set of three, with its note that `all_done` runs on the success path only, and the legacy names accepted as aliases. Its decisions 2 and 3 stand: a skip is a status of its own, and a blocked rule ends `upstream_failed` only after a failure. Its decision 5 stands, amended: only a rule's skip cascades, and §3 adds the `run` origin.
- **ADR #116**, whole. No de-abort is needed: a final failure no longer cancels the run's other tasks (ADR #137), and `all_done` runs after it as one of the nine rules.
- **ADR #114**, its statement that failure-reactive rules cannot be supported. Its decision stands, and this ADR builds on it: a failure is final only after the human decision or its timeout.
