"""Producer #1: Lambda that returns a dict — the common case.

Deploy this as `polyris-xcom-extract-dict` Lambda function.
Runtime: python3.12+. No polyris SDK required for this handler.

Task Detail Output tab after run:
    Clean JSON, no banner:
        {
          "rows": 1240,
          "path": "s3://lake/2026-01-01/data.parquet"
        }
"""


def handler(event, _context):
    # `event` carries pipeline context (pipeline_name, date, variables, upstream).
    # Producers at the head of the DAG don't have upstream — the auto-inject is
    # just an empty {} dict.
    run_date = event.get("date", "unknown")
    return {
        "rows": 1240,
        "path": f"s3://polyris-demo-lake/{run_date}/events.parquet",
    }
