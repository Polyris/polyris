"""Multi-service pipeline — one of every task type, and how each reads its data.

polyris speaks to several AWS services from the same DAG: nested Step Functions
(`sfn`), Lambda, Glue, Athena, ECS/Fargate, EMR, and Batch. This pipeline wires
one of each so you can see how they compile side by side, and — the focus here —
how each task type consumes the output of its upstream (xcom):

* A task sends data by returning it. Downstream tasks receive it two ways:
  - **Lambda / Step Functions** get upstream outputs injected into their input
    automatically — read ``event["upstream"]["<task>"]["output"]``. No IAM needed.
  - **Glue / ECS / Batch / Athena / EMR** read it explicitly with
    ``xcom.pull("<task>")`` (attach the published task-read IAM policy). This also
    works for large outputs and is the same call in every container.
* The run's logical date reaches every task the same way: ``event["variables"]``
  for Lambda, and ``POLYRIS_RUN_DATE`` (env / job args) for the service tasks.

Dependencies below are declared by *calling* a task with its upstream's result.

Run it locally (no AWS):  polyris-validate -v
"""
from datetime import timedelta

from polyris import DAG, task

with DAG(
    dag_id="multi-service-etl",
    schedule="cron(0 3 * * ? *)",           # 03:00 UTC daily
    description="One task per AWS service — a tour of the task types.",
    tags=["example", "reference", "multi-service"],
    doc_md="Reference pipeline exercising every polyris task type.",
    default_args={
        "execution_timeout": timedelta(hours=1),
        "orchestration_timeout": timedelta(hours=6),
    },
) as dag:

    @task.lambda_(function_name="polyris-test-lambda")
    def ingest():
        """Kick off ingestion via a Lambda; returns a small manifest.

        This is the head of the pipeline, so it reads only the run context that
        polyris injects into the event::

            def handler(event, _context):
                run_date = event["variables"]["current_date"]
                return {"manifest": f"s3://raw/{run_date}/events.json", "rows": 12000}
        """
        pass

    @task.glue(
        job_name="polyris-test-glue",
        glue_arguments={"--source": "events"},
            )
    def to_bronze():
        """Glue job: raw → bronze.

        A Glue script reads the run date from its job arguments and pulls the
        upstream Lambda's manifest explicitly::

            from awsglue.utils import getResolvedOptions
            from polyris import xcom
            import sys

            args = getResolvedOptions(sys.argv, ["POLYRIS_RUN_DATE"])
            manifest = xcom.pull("ingest")["manifest"]
        """
        pass

    @task.athena(
        query_string="SELECT 1",   # self-contained smoke query
        database="analytics",
        workgroup="polyris-test-wg",
        output_location="s3://polyris-test-000000000000-us-east-1/athena-results/",
    )
    def to_silver():
        """Athena CTAS/INSERT: bronze → silver."""
        pass

    @task.ecs(
        cluster="polyris-test-ecs",
        task_definition="arn:aws:ecs:us-east-1:000000000000:task-definition/polyris-test-task:1",
        launch_type="FARGATE",
        subnets=["subnet-08b2bc98a658e67b4", "subnet-09802d133167abae7"],
        security_groups=["sg-071773f53f202982f"],
        assign_public_ip="ENABLED",
        # Step Functions expects PascalCase override keys (ContainerOverrides / Name).
        container_overrides={
            "ContainerOverrides": [{"Name": "main"}]
        },
    )
    def build_features():
        """Containerized feature build on Fargate.

        Reads its run context from the injected env. Its upstream is an Athena step,
        which shares data by *writing a table* (its task output is only the query's
        execution metadata) — so the container reads the silver location directly::

            import os
            run_date = os.environ["POLYRIS_RUN_DATE"]
            # read s3://.../silver/date=<run_date>/ that the Athena step populated

        (``xcom.pull(...)`` returns rich data only for Lambda/SFN upstreams, whose
        return value *is* the payload.)
        """
        pass

    @task.batch(
        job_definition="arn:aws:batch:us-east-1:000000000000:job-definition/polyris-test-jobdef:1",
        job_queue="arn:aws:batch:us-east-1:000000000000:job-queue/polyris-test-queue",
        batch_parameters={"format": "pdf"},     # static Ref:: parameters
    )
    def render_report():
        """Render the daily report via AWS Batch.

        Note: task parameters are passed literally — a ``{% ... %}`` expression in
        ``batch_parameters`` would reach the job as that literal string, not the
        evaluated value. For dynamic values the container reads the run context
        polyris injects as env and pulls upstream data explicitly::

            import os
            from polyris import xcom

            run_date = os.environ["POLYRIS_RUN_DATE"]
            features = xcom.pull("build_features")
        """
        pass

    # A nested Step Function in another account (cross-account role).
    @task.sfn(
        arn="arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn",
    )
    def publish():
        """Publish results through a nested, cross-account workflow."""
        pass

    # Dependencies — function-call style for the linear spine, `>>` for the fan-in.
    bronze = to_bronze(ingest())
    silver = to_silver(bronze)
    features = build_features(silver)
    # EMR (aggregate) omitted — no test cluster; see testing-infra/README.md
    features >> render_report() >> publish()
