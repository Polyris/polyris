"""Manual (on-demand) pipeline — no schedule.

Omit `schedule` (or pass `schedule=None`) and the pipeline is never triggered
automatically: no EventBridge rule is created. It runs only when you start it
by hand — from the console's **Run** button or the API.

Use this for pipelines you kick off yourself: ad-hoc loads, one-off migrations,
or anything you don't want on a timer. Add `schedule="@daily"` later and a
redeploy will wire up the schedule; remove it again and the redeploy deletes it.

Run it locally (no AWS):  polyris-output --graph
"""
from polyris import DAG, task

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="manual-adhoc-load",
    # no schedule → manual / on-demand only
    description="Runs only when triggered by hand (no timer).",
) as dag:

    @task.sfn(arn=ARN)
    def extract():
        """Pull the data."""
        pass

    @task.sfn(arn=ARN)
    def load():
        """Load it downstream."""
        pass

    extract() >> load()
