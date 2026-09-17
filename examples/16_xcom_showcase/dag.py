"""XCom showcase — every reader/writer path in one pipeline (0.100.0+).

Deploy this and open the Task Detail modal for each task in the Console to see
every 0.100.0 XCom feature in one place:

* ``xcom.get(event, task)`` uniform reader
* ``xcom.push(value)`` writer for service tasks (Glue)
* Primitive return values that used to be wrapped in ``{"_raw": ...}`` (fixed)
* Loud errors: ``XComMissingError`` / ``XComUpstreamFailedError``
* ``all_done`` trigger with ``raise_on_failure=False`` opt-out
* Console UI: colored per-dep cards, AWS-metadata banner, status badges

Pipeline shape::

    extract_dict ─────┐
    extract_primitive ─┼──> report  (trigger_rule="all_done")
    aggregate_glue ───┘

    extract_dict       Lambda returns a dict — the normal case.
    extract_primitive  Lambda returns 42 — the primitive that used to break
                       (pre-0.100.0: downstream got {"_raw": "42"}, KeyError).
    aggregate_glue     Glue calls xcom.push({...}) — real data flows downstream,
                       not a {"JobRunId": "..."} metadata leak.
    report             Lambda uses xcom.get(event, "...") — loud errors by default,
                       raise_on_failure=False for the flaky upstream.

Handler code lives beside this file in ``lambda_handlers/`` and ``glue_scripts/``.
The README walks through what to observe in Console after each run.

Deploy prerequisites:
    - Base test resources from ``examples/testing-infra/test-resources.yaml``
      (provides polyris-test-lambda you can reuse, or use dedicated functions
      per handler for a cleaner demo — see README).
    - PolyrisTaskWritePolicy attached to the Glue job's IAM role — required
      for xcom.push() (dynamodb:UpdateItem on output#* keys).

Run locally (no AWS):  polyris-validate -v
"""
from datetime import timedelta

from polyris import DAG, task

# Replace the account-id in these ARNs before deploying.
_ACCOUNT = "000000000000"
_REGION = "us-east-1"


with DAG(
    dag_id="xcom-showcase",
    schedule="cron(0 5 * * ? *)",   # 05:00 UTC daily
    description="Every XCom reader/writer path in one pipeline (0.100.0 features).",
    tags=["example", "xcom", "reference"],
    doc_md=(
        "Deploy and open Task Detail for each task to see every 0.100.0 XCom "
        "feature — uniform xcom.get(), xcom.push() for service tasks, primitive "
        "parsing, loud errors, colored Console cards, AWS-metadata banner. "
        "See lambda_handlers/ and glue_scripts/ next to dag.py for the actual "
        "handler code."
    ),
    default_args={
        "execution_timeout": timedelta(minutes=15),
        "orchestration_timeout": timedelta(hours=1),
    },
) as dag:

    # -----------------------------------------------------------------------
    # Producer 1: Lambda that returns a dict (the common case).
    # -----------------------------------------------------------------------
    #
    # Handler: lambda_handlers/extract_dict.py
    #     def handler(event, _context):
    #         return {"rows": 1240, "path": "s3://lake/2026-01-01/data.parquet"}
    #
    # After running, open Task Detail for extract_dict:
    #     Output tab shows the returned dict as clean JSON. No banner.
    # -----------------------------------------------------------------------
    @task.lambda_function(
        function_name=f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:polyris-xcom-extract-dict",
    )
    def extract_dict():
        pass

    # -----------------------------------------------------------------------
    # Producer 2: Lambda that returns a primitive (int, list, None, bool).
    # -----------------------------------------------------------------------
    #
    # Handler: lambda_handlers/extract_primitive.py
    #     def handler(event, _context):
    #         return 42          # or [1, 2, 3], None, True — all fixed in 0.100.0
    #
    # Before 0.100.0: Get_Dep_Output's $isJson heuristic wrapped the value in
    # {"_raw": "42"}, and downstream event["upstream"]["extract_primitive"]["output"]
    # was a dict, not 42 — accessing .rows or [0] blew up with KeyError/TypeError.
    #
    # After 0.100.0: the value flows through untouched. In the report task:
    #     value = xcom.get(event, "extract_primitive")   # → 42
    # -----------------------------------------------------------------------
    @task.lambda_function(
        function_name=f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:polyris-xcom-extract-primitive",
    )
    def extract_primitive():
        pass

    # -----------------------------------------------------------------------
    # Producer 3: Glue with xcom.push() — service task that writes real data.
    # -----------------------------------------------------------------------
    #
    # Script: glue_scripts/aggregate_with_push.py
    #     from polyris import xcom
    #     # ... Spark work ...
    #     xcom.push({"total_rows": 500, "path": "s3://lake/aggregated.parquet"})
    #
    # Without the push (glue_scripts/aggregate_no_push.py):
    #     Task Detail → Output tab shows {"JobRunId": "jr_xyz"} + a warn banner
    #     "This output is an AWS API response, not application data. For
    #     Glue/ECS/Batch tasks, call xcom.push(value) in your job code..."
    #
    # With the push: Output tab shows the real dict, no banner.
    #
    # Downstream reads the same way regardless:
    #     data = xcom.get(event, "aggregate_glue")
    #
    # IAM required: PolyrisTaskWritePolicy on the Glue role (in addition to
    # PolyrisTaskReadPolicy). Cross-account tasks need extra setup — see docs.
    # -----------------------------------------------------------------------
    @task.glue_job(
        job_name="polyris-xcom-aggregate",
        glue_arguments={"--source": "extract_events"},
    )
    def aggregate_glue():
        pass

    # -----------------------------------------------------------------------
    # Consumer: Lambda that reads all three via xcom.get().
    # -----------------------------------------------------------------------
    #
    # trigger_rule="all_done" runs `report` after all upstreams finish, whether
    # they succeeded or not. That means the primitive producer can even be
    # intentionally flaky and we still get to demonstrate error handling.
    #
    # Handler: lambda_handlers/report.py
    #     from polyris import xcom, XComMissingError, XComUpstreamFailedError
    #
    #     def handler(event, _context):
    #         # required — raises XComMissingError if no output, XComUpstreamFailedError
    #         # if the upstream's status is not "success"
    #         d = xcom.get(event, "extract_dict")
    #
    #         # tolerant — returns None instead of raising when the primitive
    #         # producer failed (opt-in for all_done trigger consumers)
    #         p = xcom.get(event, "extract_primitive", raise_on_failure=False)
    #
    #         # normal — Glue's pushed value flows through as-is
    #         a = xcom.get(event, "aggregate_glue")
    #
    #         return {
    #             "dict_rows": d["rows"],
    #             "primitive_value": p,           # int or None
    #             "glue_total": a["total_rows"],
    #         }
    #
    # After running, open Task Detail for report → Input tab shows three
    # colored cards — one per upstream:
    #   * extract_dict         green, expandable JSON payload
    #   * extract_primitive    yellow "failed" (if it did) OR green success
    #   * aggregate_glue       green if pushed, yellow AWS-metadata warning if not
    # -----------------------------------------------------------------------
    @task.lambda_function(
        function_name=f"arn:aws:lambda:{_REGION}:{_ACCOUNT}:function:polyris-xcom-report",
        trigger_rule="all_done",
    )
    def report():
        pass

    # -----------------------------------------------------------------------
    # Wiring — three parallel producers fan into report.
    # -----------------------------------------------------------------------
    [extract_dict(), extract_primitive(), aggregate_glue()] >> report()
