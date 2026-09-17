"""Glue script demonstrating xcom.push() — real data reaches downstream (0.100.0).

Deploy as the script for `polyris-xcom-aggregate` Glue job:

    aws s3 cp aggregate_with_push.py s3://your-glue-scripts-bucket/xcom-showcase/

Or upload via Glue Console → Job details → Script location.

Requires:
    polyris>=0.100.0
        Install via Glue Job details → --additional-python-modules parameter:
            --additional-python-modules "polyris==0.100.0"

    IAM: Glue role needs BOTH policies from the polyris SAM stack:
        PolyrisTaskReadPolicy   — for the (not used here) xcom.get() fallback path
        PolyrisTaskWritePolicy  — for xcom.push() → dynamodb:UpdateItem on output#*

    (Both exported by the polyris SAM template. Attach via CFN !ImportValue.)

Without this push (see aggregate_no_push.py), the wrapper stores the Glue API's
{"JobRunId": "..."} response as `result`. Downstream tasks reading
event["upstream"]["aggregate_glue"]["output"] get that AWS metadata, not the
real aggregation output — Problem #2 (pre-0.100.0 metadata-leak default), silently for years.

Task Detail Output tab after run (with this push):
    Clean JSON, no banner:
        {"total_rows": 500, "path": "s3://polyris-demo-lake/aggregated.parquet"}

Task Detail Output tab (without push):
    Warning banner:
        "This output is an AWS API response, not application data. For
        Glue/ECS/Batch tasks, call xcom.push(value) in your job code so
        downstream tasks receive the real output."
    + AWS response JSON below.
"""
import sys

from awsglue.utils import getResolvedOptions
from polyris import xcom

# Wrapper injects POLYRIS_TASK_NAME + POLYRIS_WRAPPER_RUN_ID as --args in 0.100.0.
# xcom.push() reads them from env internally — you don't need to touch either.
# (The awsglue.utils.getResolvedOptions() call surfaces them from Glue's argv
# into os.environ for you.)
_args = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME",
        "POLYRIS_PIPELINE_NAME",
        "POLYRIS_RUN_DATE",
        "POLYRIS_TOKENS_TABLE",
        "POLYRIS_TASK_NAME",
        "POLYRIS_WRAPPER_RUN_ID",
        "source",
    ],
)


def main():
    # Fake the aggregation — a real Glue job would read from s3 and aggregate.
    # For a demo we synthesise a plausible output shape.
    #
    # In a real job you might do:
    #     df = spark.read.parquet(f"s3://.../{_args['POLYRIS_RUN_DATE']}/*.parquet")
    #     total_rows = df.count()
    #     out_path = f"s3://.../aggregated/{_args['POLYRIS_RUN_DATE']}.parquet"
    #     df.groupBy("category").sum().write.parquet(out_path)
    total_rows = 500
    out_path = f"s3://polyris-demo-lake/{_args['POLYRIS_RUN_DATE']}/aggregated.parquet"

    # THIS is the line that changes everything for the downstream Lambda:
    # writes real data + a marker the wrapper detects. The wrapper's
    # Check_Task_Pushed sees the marker (with our run's ARN in pushed_run_id)
    # and routes to Save_Success_Preserve, which does NOT overwrite `result`
    # with the AWS {"JobRunId": "..."} response.
    xcom.push({
        "total_rows": total_rows,
        "path": out_path,
        "source": _args["source"],
    })

    print(f"[xcom-showcase] pushed aggregation output for {_args['POLYRIS_RUN_DATE']}")


if __name__ == "__main__":
    main()
