"""Linear ETL — extract → transform → load, with data flowing down the chain.

Dependencies are created by *calling* one task with another's result: the output
of `extract()` flows into `transform(...)`, and that flows into `load(...)`.

Each task sends data by returning it (xcom). Here every task is a nested Step
Function, so each one receives its upstream's output in its input and reads it as
``$states.input.upstream.<task>.output`` — e.g. the transform state machine reads
``$states.input.upstream.extract.output``. (A Lambda would read the same under
``event["upstream"]``; a Glue/ECS container would call ``xcom.pull("extract")``.)

Run it locally (no AWS):  polyris-validate -v
"""
from polyris import DAG, task

# Reuse one worker state machine here for brevity; in practice each step is
# usually its own state machine. Replace with your real ARN(s).
# NOTE: ARNs below are hardcoded to the testing-infra CloudFormation stack.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="sales-etl",
    schedule="@daily",
    description="Extract sales, transform, then load — a classic linear ETL.",
) as dag:

    @task.sfn(arn=ARN)
    def extract():
        """Pull raw sales rows from the source system."""
        pass

    @task.sfn(arn=ARN)
    def transform():
        """Clean and aggregate the extracted rows.

        Reads the extract output from its input, e.g. a first state with JSONata::

            "Assign": {"rows": "{% $states.input.upstream.extract.output.rows %}"}
        """
        pass

    @task.sfn(arn=ARN)
    def load():
        """Write the aggregated result to the warehouse."""
        pass

    # extract → transform → load
    raw = extract()
    clean = transform(raw)
    load(clean)
