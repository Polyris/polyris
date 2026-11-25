# ADR #135 — A run is told how its task ended by whoever ends it

> **Superseded in part by [ADR #137](adr-137-tasks-as-rows.md) §4 (1.3.0).** Decision 1 and its table go: the dispatcher answers a run once every row is final, and no template answers one. Decisions 2–4 carry over to that one place, and a `stopped` row now counts as finished.

**Context.** A pipeline run waits for each of its tasks on a token, the row's `orchestration_token`. Two places answered it when a task failed: `run_task` on its own failure paths, and the failure handler on every other path. Between them stood a flag, `helper_failed`, which the wrapper set when `run_task` failed and which told the failure handler that `run_task` had answered already. The flag was a guess about another state machine's paths, and in the 1.2.0 rehearsal it was wrong twice (Principle #42). When the flag was missing, the JSONata read it as set (F16: a dependency wait that timed out). When it was set, `run_task` had failed before answering (a pause token that could not be saved). Other paths had no answer, or swallowed a failed one:
- the reconciler closing a task whose wrapper was stopped in the AWS console (Z1, found by the harness invariant below and confirmed live);
- the wrapper's skip answer;
- the console's task actions.

Each time the console showed the task ended while its run stayed `RUNNING` until it timed out waiting for that task, and a pipeline stack with such runs could not be deleted.

**Decision.**

1. Whoever ends a task answers its run's token, and nothing decides not to answer because someone else may have:

   | Who ends the task | Answers with | When the answer cannot get through |
   |---|---|---|
   | `run_task`, the task succeeded | `Send_Pipeline_Success` | `Warn_Run_Left_Running` |
   | `run_task`, its own failure paths | `Send_Pipeline_Failure`, then ends failed | the failure handler answers again, or warns |
   | the failure handler, whenever the wrapper fails | `Send_Task_Failure` | `Warn_Run_Left_Running` |
   | the wrapper, a rule skip or a skip the run asked for | `Auto_Skip_Send_Success` | `Warn_Run_Left_Running` |
   | the reconciler, a task whose wrapper is gone (ADR #130 §3 step 3) | `_tell_the_run` | a `coordination` warning |
   | the console: Skip, Fail, Mark Successful, a Stop that aborts | `answer_run` | a `coordination` warning; the action itself stands |

   The `helper_failed` flag is gone. A wrapper that no longer owns its row (a restart handed the row and its token to a new wrapper) stays quiet: its failure handler ends at `Superseded_Complete`, and the new owner answers.
2. A closed token is not an error. `TaskTimedOut`, `InvalidToken` and `TaskDoesNotExist` (`polyris.coordination.wake.DEAD_TOKEN_CODES`) mean that the run was answered already or has ended. They are never retried.
3. Any other error is retried twice in a template. If it persists, it reaches the bell as a `coordination` warning: the run stays `RUNNING` until someone stops it or it times out waiting for that task (the task's `execution_timeout`, 24 hours by default) (Principle #38).
4. The coordination harness checks, after every step of every scenario, that a run whose task rows have all finished has finished too (ADR #130 invariant 10). Exempt are a run with an execution still working for it and a run the bell says could not be told. A `stopped` task does not count as finished: its run may wait for a restart.

**Key design choices.**

*Answer twice rather than guess.* A second answer costs one refused API call. A missed one leaves a run `RUNNING` with nothing in the console to explain it.

*`run_task` keeps its own failure answer.* It answers first, so the run reports `run_task`'s error and cause; the failure handler's answer is then refused as closed. Dropping it would change what a failed run reports, and correctness does not need it.

*The reconciler answers after it closes the row.* The close is conditional on the row's token and registration, so a run is never told that a task ended when its wrapper is in fact alive. A Lambda that dies between the two leaves the run to its timeout, without a warning; the window is one API call.

*A console action that cannot tell the run still succeeds.* The row has changed and the action cannot be repeated, so a 500 would only tell the operator that something failed which in fact happened.

**Consequences.**
- Templates: the failure handler's `Check_Orchestration_Token`, `Send_Task_Failure` and `Warn_Run_Left_Running`; `run_task`'s `Send_Pipeline_Success`, `Send_Pipeline_Failure` and `Warn_Run_Left_Running`; the wrapper's `Auto_Skip_Send_Success` and `Warn_Run_Left_Running`; the wrapper's catch around `Run_Task` no longer sets `helper_failed`. A wrapper started before the upgrade still sets it, and the failure handler ignores it.
- Code: the reconciler's `_tell_the_run`; the console's `routes.tasks.answer_run`.
- Tests:
  - `tests/sdk/test_run_answers.py` finds every template state that answers a run and requires a role for it. It also checks the token condition, that no template sets `helper_failed`, the closed-token codes against `DEAD_TOKEN_CODES`, and the warning against `warning_item`.
  - `test_coordination_reconcile.py`: a zombie's run is told; when telling fails, a warning; when the run already ended, nothing.
  - The harness: invariant 10, and the scenarios of the pause token, the stopped wrapper, the rule skip and each console action.
- Rule in root `CLAUDE.md` (SFN Templates — Common Pitfalls).
