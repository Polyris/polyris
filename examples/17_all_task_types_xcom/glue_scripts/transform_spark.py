"""Glue ETL (Spark) — reads seed via xcom.get, transforms, pushes bronze stats.

Deploy this as the ``ScriptLocation`` of a Glue Spark job named
``polyris-test-xcom-all-transform-spark``:
    Command:
      Name: glueetl
      PythonVersion: "3"
    GlueVersion: "5.0"     # Python 3.11 — matches polyris requires-python >=3.11.

Requires ``polyris>=1.0.0`` on the cluster. Install via ``--extra-py-files``
pointing at an S3 wheel — the SAME mechanism the pythonshell variant uses.
``--additional-python-modules 'polyris @ git+...'`` fails at Glue LAUNCH
(the wrapper tokenizes on whitespace and pip sees a bare `@` — invalid
requirement). See ``polyris/CLAUDE.md`` "Glue Python install" rule::

    DefaultArguments:
      "--extra-py-files": "s3://<bucket>/polyris-1.0.0-py3-none-any.whl"

IAM:
    - AWSGlueServiceRole
    - PolyrisTaskReadPolicy   (xcom.get from upstream)
    - PolyrisTaskWritePolicy  (xcom.push scoped to output#*)

Task Detail Output tab after run (green card, no banner):
    {"bronze_rows": 15000, "s3_bronze": "s3://polyris-lake/2026-09-18/bronze.parquet"}

Without xcom.push the wrapper would store ``{"JobRunId": "jr_..."}`` here and
downstream would receive AWS metadata (the "service task metadata trap" the
Console flags with a yellow banner).
"""
import os
import sys

from awsglue.context import GlueContext  # type: ignore[import-not-found]
from awsglue.utils import getResolvedOptions  # type: ignore[import-not-found]
from pyspark.context import SparkContext  # type: ignore[import-not-found]

from polyris import xcom


# Wrapper injects POLYRIS_* as `--NAME value` arguments (see run_task SFN
# template). getResolvedOptions parses them into a dict but does NOT populate
# os.environ — xcom.push()/xcom.get() read from env, so we mirror explicitly.
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
for _k in ("POLYRIS_TASK_NAME", "POLYRIS_WRAPPER_RUN_ID",
           "POLYRIS_TOKENS_TABLE", "POLYRIS_PIPELINE_NAME",
           "POLYRIS_RUN_DATE"):
    os.environ[_k] = _args[_k]


def main():
    run_date = os.environ["POLYRIS_RUN_DATE"]

    # Read seed via xcom — event=None triggers the DDB path.
    seed = xcom.get(None, "seed_lambda")
    # seed = {"batch_id": ..., "rows": 5000, "s3_seed": "s3://.../seed.parquet"}

    sc = SparkContext.getOrCreate()
    glue_ctx = GlueContext(sc)
    spark = glue_ctx.spark_session

    # In a real ETL we'd `spark.read.parquet(seed["s3_seed"])` here. The
    # sample keeps it deterministic — synthesise a bronze row-count from the
    # seed and note the s3 path for downstream.
    bronze_rows = seed["rows"] * 3
    bronze_path = f"s3://polyris-lake/{run_date}/bronze.parquet"

    # (Uncomment when you have real seed data:)
    # df = spark.read.parquet(seed["s3_seed"])
    # bronze = df.withColumn("enriched", ...)
    # bronze.write.mode("overwrite").parquet(bronze_path)
    # bronze_rows = bronze.count()

    _ = spark  # Silence lint about unused local — real ETL would use it.

    xcom.push({"bronze_rows": bronze_rows, "s3_bronze": bronze_path})


if __name__ == "__main__":
    main()
