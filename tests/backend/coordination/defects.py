"""Defects ADR #130 fixes, by id (the ADR's Context table).

Every id needs a scenario marked `@pytest.mark.defect(id)`, and, once that
scenario is bound, a mutation probe in probes.py that re-introduces the
defect in the new code and makes the scenario fail. test_meta.py checks both.
"""

DEFECTS = {
    "B-P1": "Asset arrives during a pause: subscription deleted, status stays waiting, resume ignores it, hang",
    "B-P2": "Task deps finish during a pause, assets missing: resume runs the task without its assets",
    "B-P3": "A task saves its pause token while resume runs: resume misses it, it waits for its 12-hour heartbeat",
    "B-P4": "pause_waiter cannot save the token: nobody can release the task; it runs after 12 hours, paused or not",
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
