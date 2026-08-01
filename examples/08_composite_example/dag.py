"""Realistic pipeline — a plausible daily analytics run, end to end.

This is what a real polyris pipeline tends to look like: a mix of services, a
sensible dependency graph, retry/timeout defaults, and a cross-account publish
at the end. Nothing exotic — it just puts the pieces from the earlier examples
together the way you actually would.

Flow:
    ingest (Lambda)
      → clean (Glue) → aggregate (Athena) → build_report (Batch)
      → validate_freshness (Lambda)
    build_report + validate_freshness → publish (cross-account SFN)

Run it locally (no AWS):  polyris-validate -v
"""
from datetime import timedelta

from polyris import DAG, task

with DAG(
    dag_id="daily-analytics",
    schedule="cron(0 5 * * ? *)",          # 05:00 UTC every day
    description="Daily analytics: ingest → clean → aggregate → report → publish.",
    tags=["example", "analytics", "daily"],
    doc_md="End-to-end reference pipeline combining several services.",
    default_args={
        "retry_exponential_backoff": True,
        "execution_timeout": timedelta(hours=1),
        "orchestration_timeout": timedelta(hours=4),
    },
) as dag:

    @task.lambda_(function_name="polyris-test-lambda")
    def ingest():
        """Land yesterday's raw events; return a manifest for downstream tasks.

        Head of the pipeline — reads only the injected run context::

            def handler(event, _c):
                date = event["variables"]["current_date"]
                return {"path": f"s3://raw/{date}/", "rows": 48000}
        """
        pass

    @task.glue(job_name="polyris-test-glue")
    def clean():
        """Dedupe and normalize.

        A Glue container pulls the ingest manifest explicitly::

            from polyris import xcom
            raw = xcom.pull("ingest")           # {"path": ..., "rows": ...}
        """
        pass

    @task.athena(
        query_string="SELECT 1",   # self-contained smoke query
        database="analytics",
        workgroup="polyris-test-wg",
        output_location="s3://polyris-test-000000000000-us-east-1/athena-results/",
    )
    def aggregate():
        """Roll up into the gold daily table."""
        pass

    @task.batch(job_definition="arn:aws:batch:us-east-1:000000000000:job-definition/polyris-test-jobdef:1", job_queue="arn:aws:batch:us-east-1:000000000000:job-queue/polyris-test-queue")
    def build_report():
        """Render the executive report.

        Its upstream is an Athena step, which writes the gold table rather than
        returning rows — so the container queries that table (its run date comes from
        the injected ``POLYRIS_RUN_DATE``). ``pull()`` here would return only Athena's
        execution metadata, not the aggregated data.
        """
        pass

    @task.lambda_(function_name="polyris-test-lambda")
    def validate_freshness():
        """Guardrail: confirm the gold table is fresh before publishing.

        A downstream Lambda gets its upstream automatically (no pull, no IAM). The
        Athena upstream's output is its execution metadata — useful to confirm the
        query ran — while the freshness check itself queries the gold table::

            def handler(event, _c):
                query = event["upstream"]["aggregate"]["output"]   # exec metadata
                # then query gold.daily for its max load timestamp
        """
        pass

    @task.sfn(
        arn="arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn",
    )
    def publish():
        """Publish the report via a nested, cross-account workflow."""
        pass

    # Wire the graph.
    raw = ingest()
    cleaned = clean(raw)
    rolled = aggregate(cleaned)
    report = build_report(rolled)
    fresh = validate_freshness(rolled)

    [report, fresh] >> publish()
