"""Glue pythonshell — reads transform via xcom.get, aggregates, pushes silver.

Deploy this as the ``ScriptLocation`` of a Glue pythonshell job named
``polyris-test-xcom-all-aggregate-pyshell``:
    Command:
      Name: pythonshell
      PythonVersion: "3.9"
    MaxCapacity: 0.0625   # 1/16 DPU, cheap

Requires ``polyris>=1.0.0`` on the cluster. Glue pythonshell does NOT accept
raw ``s3://`` URIs from ``--additional-python-modules`` for arbitrary wheels
(pip has no native s3 scheme). Use ``--extra-py-files`` which is s3-aware
and accepts .whl/.egg/.py:
    DefaultArguments:
      "--extra-py-files": "s3://<bucket>/polyris-1.0.0-py3-none-any.whl"

IAM (identical to transform_spark):
    - AWSGlueServiceRole
    - PolyrisTaskReadPolicy
    - PolyrisTaskWritePolicy

Task Detail Output tab after run (green card):
    {"silver_count": 250, "s3_silver": "s3://polyris-lake/2026-09-18/silver.parquet"}
"""
import os
import sys

from awsglue.utils import getResolvedOptions  # type: ignore[import-not-found]

from polyris import xcom


# Wrapper injects POLYRIS_* as `--NAME value` arguments; getResolvedOptions
# parses them but doesn't populate os.environ, so mirror explicitly.
# JOB_NAME is intentionally omitted for pythonshell jobs — getResolvedOptions
# treats unlisted args as "required and error-if-missing"; pythonshell doesn't
# reliably auto-add JOB_NAME to sys.argv.
_args = getResolvedOptions(
    sys.argv,
    [
        "POLYRIS_PIPELINE_NAME",
        "POLYRIS_RUN_DATE",
        "POLYRIS_TOKENS_TABLE",
        "POLYRIS_TASK_NAME",
        "POLYRIS_WRAPPER_RUN_ID",
        "variant",
    ],
)
for _k in ("POLYRIS_TASK_NAME", "POLYRIS_WRAPPER_RUN_ID",
           "POLYRIS_TOKENS_TABLE", "POLYRIS_PIPELINE_NAME",
           "POLYRIS_RUN_DATE"):
    os.environ[_k] = _args[_k]


def main():
    run_date = os.environ["POLYRIS_RUN_DATE"]

    # Read the upstream Spark job's push output via xcom.
    bronze = xcom.get(None, "transform_spark")
    # bronze = {"bronze_rows": N, "s3_bronze": "s3://..."}

    # Simple aggregation — real code would read/write parquet.
    silver_count = bronze["bronze_rows"] // 60
    silver_path = f"s3://polyris-lake/{run_date}/silver.parquet"

    xcom.push({"silver_count": silver_count, "s3_silver": silver_path})


if __name__ == "__main__":
    main()
