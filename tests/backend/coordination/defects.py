"""Defects ADR #130 fixes, by id (the ADR's Context table); B-D1 is ADR #133's, and the ids from
B-RN1 on come from the external review of 1.2.0 (ADR #135 and the CHANGELOG's Fixed entries).
B-Q1 is D107's: the harness fails like AWS on an expression that evaluates to undefined.
The ids from R-L1 on are the classes of bug ADR #137 makes impossible, each
pinned by a red scenario in test_scenarios_rows.py before the code exists.

Every id needs a scenario marked `@pytest.mark.defect(id)`, and, once that
scenario is bound, a mutation probe in probes.py that re-introduces the
defect in the new code and makes the scenario fail. test_meta.py checks both.
"""

DEFECTS = {
    # Skipped until the stage that builds their mechanism (stages.py). R-S1 to R-S4 are the four
    # stability blockers of the plan's review (2026-11-16).
    "R-L1": "A lost stream record (a finished upstream, an activation, a Resume) leaves a task waiting until its "
            "limit; it must heal at the run row's next check, within 30 minutes, with one warning for the run",
    "R-S1": "A pass without a time budget: hundreds of root tasks time a pass out, and its retry times out the "
            "same way; a pass out of budget must leave the rest to the next pass at once",
    "R-S2": "A late Start_Run attempt revives a run the engine stopped while it stayed creating",
    "R-S3": "A start that keeps failing is a silent hang; it must warn after 15 minutes and fail after 6 hours, "
            "and a denied start must count toward neither",
    "R-S4": "Restart with dependants races a pass: a decision read before a reset writes a dependant "
            "upstream_failed, and the restart is lost",
    "R-D1": "Two passes over one run at once start a task twice or decide it twice",
    "R-P1": "A Pause or a Stop that lands between a pass's read and its write still lets a task start",
    "R-F1": "A read that fails or comes back partial leads to a start, a close or a run's answer (fail-open)",
    "R-E1": "A run whose rows are all final stays RUNNING because its answer was lost",
    "R-T1": "A row of an active run goes missing: its waiting dependants hang, or a task row without its run "
            "row waits forever",
    "R-O1": "A task runs without the output of an upstream that succeeded: the output expired before a late "
            "Restart, or the success was visible before its output",
    "R-O2": "A result near the 256 KB payload limit loses its success, or one over it hangs the run; over "
            "the limit the task must wait for a decision with States.DataLimitExceeded as the cause",
    "R-W1": "A limit that falls due during a pause fires in the pause, or is lost; it must fire after the "
            "Resume, moved by the paused time",
    "R-U1": "A 1.2 row left in flight across the upgrade hangs instead of being stopped with the cause",
    "R-N2": "A task that runs after a final failure reads an older run's output of the same date; the upstream "
            "must reach it as its status alone (ADR #137 §1, ADR #138 §2)",
    "R-T2": "A timed-out attempt ends the task without its retries; a timeout is a failed attempt (ADR #137 §1)",
    "R-J1": "A task whose work returns a string blocks its dependant: the result was stored as it was (\"ok\"), "
            "which no reader can parse; a result is stored as JSON, and a 1.2 row's string reaches the dependant "
            "as that string (the review of TR-5)",
    "R-N3": "A task beside an upstream that has not succeeded yet in this run (one_success) reads that upstream's "
            "older output of the same date through xcom (the review of TR-5)",
    "R-C1": "A success transaction that DynamoDB cancels (a conflict, a throttled item, a fact time already "
            "taken) loses the success; run_task must read its row and try again with a new time for each fact",
    "B-Q1": "A template reads a field its input no longer carries: AWS fails the state with "
            "States.QueryEvaluationError, while the harness carried undefined on as None and a Catch hid it "
            "(Failure_Complete of the failure handler, found live in 1.2.0, CR-7)",
    "B-RN1": "A task fails before run_task answers its run (a pause token that cannot be saved): the failure "
             "handler skips the answer on helper_failed, and the run stays RUNNING",
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
    "B-X1": "Run-name collisions between asset-AND runs, backfill tiers and same-named tasks",
    "B-M1": "mark_success dates its asset fact from the request, not from the marked run's partition",
    "B-A1": "wait_for=[a, b] wakes on the first event",
    "B-A2": "Mixed [a, b | c] fires on first arrival",
    "B-A3": "Early AND members are not re-checked for freshness",
    "B-C1": "A backfill passes on today's event",
    "B-K1": "Subscription keys collide across runs and pipelines",
    "B-T1": "A second signal overwrites running",
    "B-U1": "Push AND loses the day when Trigger_AND fails after the counter completes",
}
