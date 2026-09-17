"""Glue script demonstrating the metadata leak — INTENTIONALLY does NOT push.

Deploy as the script for `polyris-xcom-aggregate` to see what happens WITHOUT
xcom.push(). This is the pre-0.100.0 default behaviour for service tasks:

    Wrapper stores the Glue startJobRun response as `result`:
        {"JobRunId": "jr_abc123", "StartedOn": "...", ...}

    Downstream reads:
        xcom.get(event, "aggregate_glue")
        → {"JobRunId": "jr_abc123", "StartedOn": "...", ...}
        NOT the aggregation data.

Task Detail Output tab after this runs:
    Warning banner (new in 0.100.0):
        "This output is an AWS API response, not application data. For
        Glue/ECS/Batch tasks, call xcom.push(value) in your job code so
        downstream tasks receive the real output."
    + the AWS response JSON below.

Swap to `aggregate_with_push.py` on the same Glue job to see the difference.
"""
import sys

from awsglue.utils import getResolvedOptions

_args = getResolvedOptions(sys.argv, ["JOB_NAME", "POLYRIS_RUN_DATE", "source"])


def main():
    # Real aggregation would happen here — but we deliberately DON'T push
    # the result. The wrapper falls back to storing the AWS API response.
    print(f"[xcom-showcase] ran without xcom.push() for {_args['POLYRIS_RUN_DATE']}")
    print("[xcom-showcase] downstream will see {JobRunId: ...} instead of real data")
    print("[xcom-showcase] Console Output tab will show the AWS-metadata warning banner")


if __name__ == "__main__":
    main()
