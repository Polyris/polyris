"""Direct steps & scheduling — service calls without a wrapper Lambda.

Not everything needs a state machine. "Direct steps" call an AWS service inline:
wait, pass/transform, publish to SNS, enqueue to SQS, read/write S3. They run in
parallel alongside your tasks (they don't take dependencies). This pipeline also
shows a `rate(...)` schedule, `wait_before` (built-in rate limiting on a task),
and DAG-level `variables` computed once at the start of every run.

Run it locally (no AWS):  polyris-validate -v
"""
from polyris import DAG, task, Wait, Pass, SNSTask, SQSTask, S3Task

ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:worker"

with DAG(
    dag_id="direct-steps-demo",
    schedule="rate(6 hours)",
    description="Direct service steps (Wait/Pass/SNS/SQS/S3) mixed with tasks.",
    tags=["example", "direct-steps"],
    variables={
        # Computed at the start of the run, available to every task/step.
        "run_prefix": "{% 'runs/' & $states.input.current_date %}",
    },
) as dag:

    # A task that self-throttles: wait 30s before starting (rate limiting).
    @task.sfn(arn=ARN, wait_before=30)
    def call_rate_limited_api():
        pass

    @task.sfn(arn=ARN)
    def process():
        pass

    # Direct steps — constructed inside the DAG; each runs in parallel.
    Wait(step_id="cooldown", seconds=30)

    Pass(
        step_id="prepare_params",
        output={"path": "{% 's3://bucket/' & $states.input.current_date %}", "mode": "full"},
    )

    S3Task(
        step_id="load_config",
        operation="get_object",
        bucket="pipeline-config",
        key="direct-steps/settings.json",
    )

    S3Task(
        step_id="save_manifest",
        operation="put_object",
        bucket="pipeline-output",
        key="manifests/latest.json",
        body="{% $string($states.input) %}",
        content_type="application/json",
    )

    SNSTask(
        step_id="announce_start",
        topic_arn="arn:aws:sns:us-east-1:000000000000:pipeline-events",
        message="Direct-steps pipeline started",
        subject="pipeline",
    )

    SQSTask(
        step_id="enqueue_followup",
        queue_url="https://sqs.us-east-1.amazonaws.com/000000000000/followups",
        message_body="{% $string($states.input) %}",
        delay_seconds=10,
    )

    call_rate_limited_api()
    process()
