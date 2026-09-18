"""XCom showcase — every reader/writer path in one pipeline (1.0.0+).

Deploy this and open the Task Detail modal for each task in the Console to
see every 1.0.0 XCom feature in one place:

* ``xcom.get(event, task)`` — uniform reader for every task type
* ``xcom.push(value)`` — writer for service tasks (Glue), closes the
  "AWS response metadata leak" trap
* Primitive return values (``42``, ``[1,2,3]``, ``None``) — used to be
  wrapped in ``{"_raw": "..."}`` before 1.0.0
* Loud errors: ``XComMissingError`` / ``XComUpstreamFailedError`` /
  ``XComTruncatedError`` / ``XComManuallyResolvedError``
* ``all_done`` trigger with ``raise_on_failure=False`` opt-out
* Console UI: colored per-dep cards, AWS-metadata banner, status badges

Deploy prerequisites:
    1. polyris SAM stack deployed to the target AWS account.
    2. ``examples/testing-infra/test-resources.yaml`` stack deployed — it
       provisions the four resources this DAG references:
           polyris-test-xcom-extract-dict       Lambda
           polyris-test-xcom-extract-primitive  Lambda
           polyris-test-xcom-report             Lambda (with polyris SDK)
           polyris-test-xcom-aggregate          Glue job (uses xcom.push)
    3. ``polyris-deploy`` from this directory.

Then open the Console → xcom-showcase pipeline → latest run, and walk
through the "What to look at in the Console" section of README.md.

Pipeline shape (fan-in)::

    extract_dict ─────┐
    extract_primitive ─┼──▶ report  (trigger_rule="all_done")
    aggregate_glue ───┘

What each task demonstrates:

* ``extract_dict`` — Lambda returning a dict — the common case.
* ``extract_primitive`` — Lambda returning 42 — the primitive that used to
  break pre-1.0.0 (downstream got ``{"_raw": "42"}`` and KeyError'd).
* ``aggregate_glue`` — Glue job with ``xcom.push({...})`` — real data reaches
  downstream instead of the wrapper storing ``{"JobRunId": "..."}``.
* ``report`` — Lambda using ``xcom.get(event, "...")`` — uniform reader with
  loud errors by default, ``raise_on_failure=False`` opt-out for the
  ``all_done`` trigger case.

Handler code lives beside this file in ``lambda_handlers/`` and
``glue_scripts/`` — identical to the examples/16_xcom_showcase copies since
the test-resources stack installs the same code either way.

Run locally (no AWS):  polyris-validate -v
"""
from datetime import timedelta

from polyris import DAG, task


with DAG(
    dag_id="xcom-showcase",
    schedule="cron(0 5 * * ? *)",   # 05:00 UTC daily
    description="Every XCom reader/writer path in one pipeline (1.0.0 features).",
    tags=["example", "xcom", "reference"],
    doc_md=(
        "Deploy and open Task Detail for each task to see every 1.0.0 XCom "
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
    @task.lambda_function(function_name="polyris-test-xcom-extract-dict")
    def extract_dict():
        """See lambda_handlers/extract_dict.py — pure stdlib handler, returns
        ``{"rows": N, "path": "s3://..."}``. Task Detail Output tab shows the
        dict as clean JSON, no banner."""
        pass

    # -----------------------------------------------------------------------
    # Producer 2: Lambda that returns a primitive.
    # -----------------------------------------------------------------------
    #
    # See lambda_handlers/extract_primitive.py. Pre-1.0.0 this would have
    # wrapped the value in ``{"_raw": "42"}`` via the $isJson heuristic —
    # downstream ``event["upstream"]["extract_primitive"]["output"]`` was a
    # dict, not 42.
    #
    # 1.0.0 replaces the heuristic with ``$exists($parse($safe))``; the value
    # flows through untouched, and ``report`` reads it as ``42`` via xcom.get().
    # -----------------------------------------------------------------------
    @task.lambda_function(function_name="polyris-test-xcom-extract-primitive")
    def extract_primitive():
        pass

    # -----------------------------------------------------------------------
    # Producer 3: Glue with xcom.push() — service task writing real data.
    # -----------------------------------------------------------------------
    #
    # See glue_scripts/aggregate_with_push.py. testing-infra installed this
    # exact script as the Glue job's ScriptLocation. Without the push (see
    # aggregate_no_push.py, deployed manually to compare), the wrapper would
    # store ``{"JobRunId": "..."}`` as the task result and downstream would
    # see AWS metadata instead of the aggregation output.
    #
    # IAM: the Glue role has PolyrisTaskReadPolicy (dynamodb:GetItem) AND
    # PolyrisTaskWritePolicy (dynamodb:UpdateItem on output#* keys) — both
    # attached by the testing-infra stack.
    # -----------------------------------------------------------------------
    @task.glue_job(
        job_name="polyris-test-xcom-aggregate",
        glue_arguments={"--source": "events"},
    )
    def aggregate_glue():
        pass

    # -----------------------------------------------------------------------
    # Consumer: Lambda that reads all three via xcom.get() (1.0.0 API).
    # -----------------------------------------------------------------------
    #
    # trigger_rule="all_done" runs report after every upstream finishes,
    # success or fail. See lambda_handlers/report.py:
    #
    #     dict_output      = xcom.get(event, "extract_dict")
    #     primitive_output = xcom.get(event, "extract_primitive", raise_on_failure=False)
    #     glue_output      = xcom.get(event, "aggregate_glue")
    #
    # Deployment zip: testing-infra's XcomZipBuilder Custom Resource assembles
    # index.py + vendored polyris/xcom.py + minimal polyris/__init__.py into
    # s3://<bucket>/xcom-showcase/report.zip; the Lambda references that S3 key.
    #
    # Task Detail Input tab after run shows three colored per-upstream cards:
    #   * extract_dict         green, expandable JSON
    #   * extract_primitive    green success, or yellow "no output recorded"
    #                          if the handler raised (all_done still fires this)
    #   * aggregate_glue       green if pushed, yellow AWS-metadata banner if not
    # -----------------------------------------------------------------------
    @task.lambda_function(
        function_name="polyris-test-xcom-report",
        trigger_rule="all_done",
    )
    def report():
        pass

    # -----------------------------------------------------------------------
    # Wiring — three parallel producers fan into report.
    # -----------------------------------------------------------------------
    [extract_dict(), extract_primitive(), aggregate_glue()] >> report()
