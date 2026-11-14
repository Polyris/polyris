"""Defects ADR #130 fixes, by id (the ADR's Context table); B-D1 is ADR #133's, and the ids from
B-RN1 on come from the external review of 1.2.0 (ADR #135 and the CHANGELOG's Fixed entries).

Every id needs a scenario marked `@pytest.mark.defect(id)`, and, once that
scenario is bound, a mutation probe in probes.py that re-introduces the
defect in the new code and makes the scenario fail. test_meta.py checks both.
"""

DEFECTS = {
    "B-RN1": "A task fails before run_task answers its run (a pause token that cannot be saved): the failure "
             "handler skips the answer on helper_failed, and the run stays RUNNING",
    "B-RN2": "The reconciler aborts a task whose wrapper is gone without telling its run: the run stays RUNNING",
    "B-RN3": "The wrapper's skip answer swallows a failed answer: the task is skipped, the run stays RUNNING, nothing says why",
    "B-RN4": "A console action swallows a failed answer to the run (Stop), or answers 500 after the row has changed",
    "B-F3": "run_task cannot record a fact after the task succeeded: the consumers hang and nothing says why",
    "B-U4": "The hourly catch-up counts a fact recorded after the one it replays: a cascade='none' backfill "
            "completes an AND consumer",
    "B-S2": "The console's Stop of a run calls a repository method that does not exist: 500, and the run stays RUNNING",
    "B-R3": "A restart that cannot take the row over or start the new wrapper leaves no trace, though the console "
            "says the task was restarted",
    "B-M3": "Mark Successful records a backfill's facts with cascade='auto': a cascade='none' backfill starts consumers",
    "B-P1": "Asset arrives during a pause: subscription deleted, status stays waiting, resume ignores it, hang",
    "B-P2": "Task deps finish during a pause, assets missing: resume runs the task without its assets",
    "B-P3": "A task saves its pause token while resume runs: resume misses it, it waits for its 12-hour heartbeat",
    "B-P4": "pause_waiter cannot save the token: nobody can release the task; it runs after 12 hours, paused or not",
    "B-G2": "A reconciler that times out during registration fails a healthy task instead of leaving it to the sweeper",
    "B-M2": "A Mark Successful whose fact cannot be recorded reports plain success: the tasks waiting on it hang silently",
    "B-F2": "One unreadable fact fails every task waiting on its asset",
    "B-W1": "A wrapper claim whose write applied but whose answer was lost ends as superseded: the task never runs",
    "B-U2": "The push catch-up starts a consumer deployed after the facts for those older facts",
    "B-D1": "A scheduled run of a pipeline with variables gets no date and fails; each task takes the day it "
            "starts, so a run crossing midnight gets two dates and a 23:59 schedule started late the next day's",
    "B-R2": "A restart keeps the old terminal status until the new wrapper registers: a dependant restarted "
            "meanwhile runs on the old success, or is blocked again by the old failure",
    "B-U3": "A push subscription outlives what made it: an asset dropped from the schedule, or a pipeline moved to a cron, "
            "keeps its subscription and gets a warning for every fact",
    "B-O2": "A wrapper a restart replaced registers again and takes the row back: the task runs twice",
    "B-O3": "Registration wakes a pre-upgrade wrapper with deps_ready after an operator's decision: the task runs at once",
    "B-O4": "run_task writes running over an operator's decision whose stops failed: the task runs",
    "B-O5": "A task an operator decided while it was held by a pause is released to run after the resume",
    "B-O1": "An operator resolves a waiting task but its wrapper cannot be stopped: registering again overwrites the row and runs the task",
    "B-R1": "restart_task passes neither wait_for nor the other starter fields: the restart is not gated on assets",
    "B-N1": "Warning rows dated by partition and grouped per run: older warnings never reach the bell",
    "B-S1": "Auto_Skip_Register swallows a failed status write and still notifies dependants",
    "B-G1": "A failed registration SFN never signals the wrapper: silent hang until orchestration_timeout",
    "B-X1": "Run-name collisions between asset-AND runs, backfill tiers and same-named tasks",
    "B-M1": "mark_success dates its asset fact from the request, not from the marked run's partition",
    "B-A1": "wait_for=[a, b] wakes on the first event",
    "B-A2": "Mixed [a, b | c] fires on first arrival",
    "B-A3": "Early AND members are not re-checked for freshness",
    "B-C1": "A backfill passes on today's event",
    "B-K1": "Subscription keys collide across runs and pipelines",
    "B-T1": "A second signal overwrites running",
    "B-U1": "Push AND loses the day when Trigger_AND fails after the counter completes",
    "B-F1": "A consumer-notification failure fails the producer task",
}
