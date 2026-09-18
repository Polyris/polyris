"""End-to-end XCom smoke — every task type reads upstream AND writes its output.

Ships a linear chain that exercises every polyris task type polyris 1.0.0 supports,
with each task participating in XCom bidirectionally (reads upstream via
``xcom.get()`` / native inject, writes its output via return / ``xcom.push()`` /
nested SFN Output).

Pipeline shape (strict linear chain — makes cross-hop data flow obvious in the
Console)::

    seed_lambda           (Lambda, returns dict)
      → transform_spark   (Glue ETL Spark, xcom.push)
      → aggregate_pyshell (Glue pythonshell, xcom.push)
      → summary_athena    (Athena SQL — see limitation note)
      → resolve_athena    (Lambda, unpacks QueryExecutionId, returns real value)
      → compute_ecs       (ECS Fargate, xcom.push)
      → render_batch      (Batch Fargate, xcom.push)
      → publish_sfn       (nested Step Functions, native Output)

Per-task xcom pattern:

* ``seed_lambda`` — head of chain, no upstream. Returns a dict; the wrapper
  captures it as the task's xcom output automatically (Lambda return = xcom).
* ``transform_spark`` — Glue ETL (Spark). Reads seed's ``s3_seed`` via
  ``xcom.get(None, "seed_lambda")`` in job code; enriches; writes bronze
  parquet; publishes ``{"bronze_rows", "s3_bronze"}`` via ``xcom.push()``.
* ``aggregate_pyshell`` — Glue pythonshell. Reads transform via ``xcom.get()``;
  aggregates; publishes ``{"silver_count", "s3_silver"}`` via ``xcom.push()``.
* ``summary_athena`` — Athena SQL. Reads ``silver_count`` **at DAG-definition
  time** via ``variables=`` templating (Athena SQL can't call xcom at runtime;
  see DATA_PASSING.md#emr-and-athena--reads-yes-xcompush-no). The task's xcom
  output is the wrapper-captured ``{QueryExecution: {QueryExecutionId: ...}}``
  AWS metadata — Athena has no SDK hook for ``xcom.push()``. Downstream reads
  that metadata and unpacks it.
* ``resolve_athena`` — Lambda. Reads ``summary_athena``'s xcom output
  (``QueryExecutionId``), calls ``boto3.athena.get_query_results``, returns the
  real ``verified_count``. This is the "Lambda-after-Athena" pattern that gives
  Athena results a real xcom payload for anything downstream.
* ``compute_ecs`` — ECS Fargate container. Reads ``resolve_athena`` via
  ``xcom.get(None, "resolve_athena")``; publishes ``{"metrics"}`` via
  ``xcom.push()``. Requires Python-capable container image (see README).
* ``render_batch`` — Batch Fargate container. Reads ``compute_ecs`` via
  ``xcom.get()``; publishes ``{"report_path"}`` via ``xcom.push()``.
* ``publish_sfn`` — nested Step Functions state machine. Receives
  ``render_batch``'s output as its SFN Input under ``$.upstream.render_batch.
  output``; the nested SM returns ``{"published_url"}`` as its Output, which
  the wrapper stores as this task's xcom.

EMR is intentionally omitted: ``xcom.push()`` is not supported for EMR in 1.0.0
(``addStep.sync`` has no Environment field to inject POLYRIS_* env vars, and
injecting via ``HadoopJarStep.Args`` would risk breaking user Spark parsers).
See docs/features/DATA_PASSING.md.

The examples version uses the placeholder-style ``polyris-test-*`` resource
names from ``examples/testing-infra/test-resources.yaml``. A ready-to-run
variant with the same resource names in the polyris-dev account lives in
``examples_temp/17_all_task_types_xcom/`` — same DAG, same handler code.

Run locally (no AWS):  polyris-validate -v
"""
from datetime import timedelta

from polyris import DAG, task


# Athena's `query_string` is passed verbatim to StartQueryExecution — polyris
# does no runtime templating on it (no Jinja `{{ }}`, no JSONata `{% ... %}`;
# those literals would break the SQL parser). So we bake the threshold into
# the query at DAG-build time as a plain Python f-string. `variables=` on
# the DAG still records it as run context, visible in the Console.
SILVER_THRESHOLD = 100


with DAG(
    dag_id="all-task-types-xcom",
    schedule="cron(0 6 * * ? *)",   # 06:00 UTC daily
    description="Every polyris 1.0.0 task type, each fully participating in xcom.",
    tags=["example", "xcom", "smoke", "reference"],
    doc_md=(
        "Chain of 8 tasks covering every supported task type in 1.0.0. Each "
        "task reads its upstream via xcom (native inject or xcom.get()) and "
        "writes its output via xcom (native return or xcom.push()). Athena is "
        "the one asymmetric hop — its output is AWS metadata (QueryExecutionId) "
        "which the following Lambda unpacks into a real payload. EMR is "
        "excluded — xcom.push() unsupported in 1.0.0. See dag.py docstring."
    ),
    variables={
        "silver_threshold": SILVER_THRESHOLD,
    },
    default_args={
        "execution_timeout": timedelta(minutes=15),
        "orchestration_timeout": timedelta(hours=1),
    },
) as dag:

    # -----------------------------------------------------------------------
    # 1. Seed — Lambda producer. Returns a dict; wrapper captures the return
    #    value as xcom output automatically. No SDK required in the handler.
    # -----------------------------------------------------------------------
    @task.lambda_function(function_name="polyris-test-xcom-all-seed")
    def seed_lambda():
        """Head of the chain. See lambda_handlers/seed.py — returns
        ``{"batch_id", "rows", "s3_seed"}``. Lambda return values become xcom
        outputs natively; downstream sees the dict verbatim."""
        pass

    # -----------------------------------------------------------------------
    # 2. Transform — Glue ETL Spark. Reads seed via xcom.get; enriches with
    #    Spark; xcom.push()es {bronze_rows, s3_bronze}.
    # -----------------------------------------------------------------------
    #
    # Requires:
    # - Glue job of Command Name "glueetl" (Spark), GlueVersion "4.0"+, DPUs
    # - polyris SDK installed on the cluster via
    #     --additional-python-modules polyris==1.0.0
    #   (or via --extra-py-files s3://.../polyris-1.0.0-*.whl if not on PyPI yet)
    # - IAM: PolyrisTaskReadPolicy (xcom.get) + PolyrisTaskWritePolicy (xcom.push)
    # -----------------------------------------------------------------------
    @task.glue_job(
        job_name="polyris-test-xcom-all-transform-spark",
        glue_arguments={"--source": "seed"},
    )
    def transform_spark():
        """See glue_scripts/transform_spark.py."""
        pass

    # -----------------------------------------------------------------------
    # 3. Aggregate — Glue pythonshell. Reads transform via xcom.get;
    #    aggregates in-memory; xcom.push()es {silver_count, s3_silver}.
    # -----------------------------------------------------------------------
    #
    # Different Glue Command from #2 — Command Name "pythonshell", cheap
    # (0.0625 DPU), used for lightweight/scripting workloads. Same xcom API.
    # -----------------------------------------------------------------------
    @task.glue_job(
        job_name="polyris-test-xcom-all-aggregate-pyshell",
        glue_arguments={"--variant": "pythonshell"},
    )
    def aggregate_pyshell():
        """See glue_scripts/aggregate_pyshell.py."""
        pass

    # -----------------------------------------------------------------------
    # 4. Summary — Athena SQL. Reads ``silver_threshold`` from DAG variables
    #    (Athena can't xcom.get at runtime); output is the wrapper-captured
    #    QueryExecutionId metadata (Athena can't xcom.push either).
    # -----------------------------------------------------------------------
    @task.athena_query(
        query_string=(
            "SELECT COUNT(*) AS above_threshold "
            "FROM analytics.silver_stats "
            f"WHERE silver_count > {SILVER_THRESHOLD}"
        ),
        database="analytics",
        workgroup="polyris-test-wg",
        output_location="s3://polyris-test-000000000000-us-east-1/athena-results/",
    )
    def summary_athena():
        """Athena SQL — threshold baked at DAG-build time (see SILVER_THRESHOLD).
        No xcom.push (SQL can't call Python). Downstream ``resolve_athena``
        unpacks the QueryExecutionId. See dag.py docstring for the limitation."""
        pass

    # -----------------------------------------------------------------------
    # 5. Resolve — Lambda. Reads summary_athena's QueryExecutionId via
    #    xcom.get; calls athena.get_query_results; returns the real value as
    #    the task's xcom output.
    # -----------------------------------------------------------------------
    #
    # This is the documented "Lambda-after-Athena" pattern
    # (docs/features/DATA_PASSING.md#emr-and-athena) that gives Athena results
    # a real xcom payload for anything downstream. The handler carries polyris
    # SDK (needed for xcom.get to unpack the metadata deterministically).
    #
    # IAM:
    # - PolyrisTaskReadPolicy (xcom.get on the summary_athena row)
    # - athena:GetQueryResults on the workgroup
    # - s3:GetObject on the workgroup's output_location
    # -----------------------------------------------------------------------
    @task.lambda_function(function_name="polyris-test-xcom-all-resolve-athena")
    def resolve_athena():
        """See lambda_handlers/resolve_athena.py."""
        pass

    # -----------------------------------------------------------------------
    # 6. Compute — ECS Fargate. Reads resolve_athena via xcom.get; publishes
    #    {metrics} via xcom.push. Requires Python-capable container.
    # -----------------------------------------------------------------------
    #
    # NOTE: the shared test-resources.yaml provisions ``polyris-test-ecs`` with
    # a busybox container that only echoes. To run this pipeline you need a
    # Python-capable image with polyris>=1.0.0 installed; see README for the
    # ECS TaskDefinition snippet.
    # -----------------------------------------------------------------------
    @task.ecs_task(
        cluster="polyris-test-ecs",
        # No trailing `:N` revision — ECS RunTask resolves the family name to
        # the latest ACTIVE revision. Pinning `:1` locks you to whatever code
        # CFN built the first time; the next `sam deploy` bumps the container
        # Command into TaskDefinition:2, but a pinned DAG keeps calling :1.
        task_definition=(
            "arn:aws:ecs:us-east-1:000000000000:task-definition/"
            "polyris-test-xcom-all-compute"
        ),
        launch_type="FARGATE",
        subnets=["subnet-08b2bc98a658e67b4", "subnet-09802d133167abae7"],
        security_groups=["sg-071773f53f202982f"],
        assign_public_ip="ENABLED",
        container_overrides={"ContainerOverrides": [{"Name": "main"}]},
    )
    def compute_ecs():
        """See ecs_task_code/compute.py — bundled into the container image."""
        pass

    # -----------------------------------------------------------------------
    # 7. Render — Batch Fargate. Reads compute_ecs via xcom.get; publishes
    #    {report_path} via xcom.push. Same Python-capable container caveat.
    # -----------------------------------------------------------------------
    @task.batch_job(
        # No `:N` revision — Batch SubmitJob resolves the family name to the
        # latest ACTIVE revision (same reasoning as compute_ecs above).
        job_definition=(
            "arn:aws:batch:us-east-1:000000000000:job-definition/"
            "polyris-test-xcom-all-render"
        ),
        job_queue=(
            "arn:aws:batch:us-east-1:000000000000:job-queue/"
            "polyris-test-queue"
        ),
        batch_parameters={"format": "pdf"},
    )
    def render_batch():
        """See batch_task_code/render.py — bundled into the container image."""
        pass

    # -----------------------------------------------------------------------
    # 8. Publish — nested Step Functions. Receives render_batch's output as
    #    its SFN Input; the nested SM's Output becomes this task's xcom.
    # -----------------------------------------------------------------------
    #
    # No SDK, no push — SFN "just works" because the wrapper stores the
    # nested SM's Output as ``result`` on the output# row. The nested SM
    # definition lives in sfn_definitions/publish.json.
    # -----------------------------------------------------------------------
    @task.sfn(
        arn=(
            "arn:aws:states:us-east-1:000000000000:stateMachine:"
            "polyris-test-xcom-all-publish"
        ),
    )
    def publish_sfn():
        """See sfn_definitions/publish.json — a Pass state that constructs
        ``{"published_url": ...}`` from ``$.upstream.render_batch.output``."""
        pass

    # ------------------------------------------------------------------
    # Wiring — strict linear chain. Function-call form keeps the data-flow
    # visual: each task's return value binds as the next task's upstream.
    # ------------------------------------------------------------------
    seed = seed_lambda()
    bronze = transform_spark(seed)
    silver = aggregate_pyshell(bronze)
    query = summary_athena(silver)
    verified = resolve_athena(query)
    metrics = compute_ecs(verified)
    report = render_batch(metrics)
    report >> publish_sfn()
