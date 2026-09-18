"""Lambda-after-Athena — unpacks the wrapper-captured QueryExecutionId
metadata into a real xcom payload.

Deploy this as ``polyris-test-xcom-all-resolve-athena`` Lambda function
(``python3.12+``), with ``polyris>=1.0.0`` in the deployment zip.

Why this task exists:
    Athena SQL cannot call ``xcom.push()`` — there's no Python hook. Whatever
    the wrapper writes to ``output#{pipeline}#summary_athena#{date}`` is the
    ``StartQueryExecution`` API response:
        {"QueryExecution": {"QueryExecutionId": "..."}}
    That's AWS metadata, not the query result. This handler reads that
    metadata via ``xcom.get()``, calls ``athena.get_query_results`` to fetch
    the actual result rows, and returns the real value. Downstream tasks
    (``compute_ecs``) can then read a proper xcom payload.

IAM:
    - PolyrisTaskReadPolicy (xcom.get on the summary_athena row)
    - athena:GetQueryResults on the query's workgroup
    - s3:GetObject on the workgroup's output_location bucket

Task Detail Output tab after run:
    Clean JSON, no banner:
        {"verified_count": 42}
"""
import boto3

from polyris import xcom


def handler(event, _context):
    # xcom.get() reads from event.upstream first, falls back to DDB. Athena's
    # wrapper stores the boto3 StartQueryExecution response — a dict with
    # "QueryExecution.QueryExecutionId" under the wrapped-metadata shape the
    # Console banners flag.
    meta = xcom.get(event, "summary_athena")
    qid = meta["QueryExecution"]["QueryExecutionId"]

    athena = boto3.client("athena")
    result = athena.get_query_results(QueryExecutionId=qid)

    # Athena get_query_results returns rows[0] = header row, rows[1:] = data.
    # Our query is `SELECT COUNT(*) AS above_threshold ...` — one row, one
    # column. Coerce to int; empty result → 0.
    data_rows = result["ResultSet"]["Rows"][1:]
    if not data_rows:
        return {"verified_count": 0}
    return {"verified_count": int(data_rows[0]["Data"][0]["VarCharValue"])}
