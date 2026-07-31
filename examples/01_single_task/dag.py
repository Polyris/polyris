"""Hello, polyris — the smallest possible pipeline.

A single scheduled task. Everything here runs locally with no AWS account:

    polyris-validate           # check the DAG is valid
    polyris-output --graph     # see the DAG as an ASCII graph
    polyris-output --mermaid   # Mermaid diagram
    polyris-output --json      # the generated Step Functions definition (ASL)

Alerts (Slack / PagerDuty) are configured in the Console UI — Settings → Alerts,
not in the pipeline code.

When you're ready to run it for real:  polyris-deploy
"""
from polyris import DAG, task

# Every @task.sfn invokes an existing AWS Step Functions state machine.
# Swap in one of your own ARNs before deploying.
# NOTE: ARN hardcoded to the testing-infra CloudFormation stack.
HELLO_ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="hello-world",
    schedule="@daily",                    # midnight UTC; also "@hourly", or cron(...)
    description="The smallest polyris pipeline: one task, once a day.",
) as dag:

    @task.sfn(arn=HELLO_ARN)
    def say_hello():
        """Invoke the 'hello' state machine."""
        pass

    say_hello()
