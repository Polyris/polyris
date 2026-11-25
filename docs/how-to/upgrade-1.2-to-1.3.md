# Upgrade from 1.2 to 1.3

How to upgrade a platform that runs polyris 1.2 to 1.3. This upgrade has steps of its own: 1.3 runs tasks in a new way, and it takes over no run that 1.2 started. Plan a quiet window; your pipelines start no run between steps 3 and 5. Every other upgrade is one command: [manage-the-platform.md](manage-the-platform.md#upgrade).

## Before you start

A platform older than 1.2.0 goes to 1.2 first: install that release (`POLYRIS_REF=v1.2.3` with `install.sh`), run its `polyris-up`, and wait until 1.2 has finished adopting the older rows for 48 hours.

## Steps

1. **Check your pipelines.** Install polyris 1.3 and run `polyris-validate --all` in the folder that holds your pipelines' folders. Fix what it reports: a pipeline it refuses cannot be redeployed in step 5. Rebuild every bundle that contains the polyris SDK with 1.3 too: a Lambda's package, a Glue job's wheel, an ECS or Batch image.
2. **Grant the roles their new permissions:** [IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#upgrade-from-12-to-13). `PowerUserAccess` covers them.
3. **Drain.** Disable the pipelines' schedules (EventBridge Scheduler, the schedules named `…-schedule`; [how](schedule-and-redeploy.md#pause-and-resume-a-schedule)), so that no new run starts. Decide the failed runs: a run started on 1.2 takes no action after the upgrade. Then let the runs in flight finish, or stop them.
4. **Upgrade.** Run `polyris-up`. If the update fails, it rolls back to 1.2: enable the schedules again, and the platform runs as before.
5. **Redeploy every pipeline** with `polyris-deploy`. `polyris-up` lists them at its end. Until you redeploy a pipeline, its runs fail at once. The redeploy turns the pipeline's schedule back on, also one that was off on purpose; `is_paused_upon_creation=True` keeps it off. Then start again by hand the runs whose schedule fired during the window, the runs `polyris-up` stopped, and the asset-triggered runs whose data came during the window: each of them failed at once, and nothing starts it again.

## What `polyris-up` checks and does

Before it changes anything, `polyris-up` refuses when:

- the platform is older than 1.2.0;
- 1.2 has not finished adopting older rows, or finished less than 48 hours ago;
- a run is in flight.

Each refusal says what to do. `--accept-stopped-runs` upgrades although a run may be in flight or 1.2's adoption has not settled. No flag upgrades a platform older than 1.2.0.

Once the update has succeeded, `polyris-up` stops what 1.2 left running and prints the runs it stopped. A run of a pipeline that you redeployed since is kept.

It then waits until DynamoDB has built the index that the console's run lists read: about 8 minutes on a table of 100,000 rows. You can interrupt the wait; until the index is built, the console says it is being built. At the end it lists the pipelines that still need a redeploy, with the command for each, and a later `polyris-up` lists them again until none is left.

If the upgrade is interrupted, or `polyris-up` reports that it could not stop an execution, fix the cause and run `polyris-up` again. It stops what is left and goes on.

There is no way back to 1.2 but the rollback of a failed update: `polyris-up` refuses to put 1.2 under a platform that runs 1.3, also with `--allow-downgrade`.

## In the console after the upgrade

- A pipeline's status in the list counts runs started on 1.3 only, so it appears with the pipeline's first run after the upgrade.
- **Runs** and **Tasks** without a date show runs started on 1.3. Pick a date to see the older runs of that date.
- A run started on 1.2 that had a task unfinished shows `stale`, and so does that task: the upgrade stopped the run, and 1.3 finishes neither.
