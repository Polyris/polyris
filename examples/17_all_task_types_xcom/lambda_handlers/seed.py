"""Head of the chain — Lambda producer with no upstream.

Deploy this as ``polyris-test-xcom-all-seed`` Lambda function
(``python3.12+``). No polyris SDK required — a plain ``return`` value is
captured by the wrapper's ``Save_Success`` state and becomes the task's xcom
output automatically.

Task Detail Output tab after run:
    Clean JSON, no banner:
        {
          "batch_id": "2026-09-18",
          "rows": 5000,
          "s3_seed": "s3://polyris-lake/2026-09-18/seed.parquet"
        }
"""


def handler(event, _context):
    # `event` carries the polyris injections: variables, upstream (empty for
    # the head), pipeline_name, date.
    variables = event.get("variables", {})
    run_date = variables.get("current_date", "unknown")
    return {
        "batch_id": run_date,
        "rows": 5000,
        "s3_seed": f"s3://polyris-lake/{run_date}/seed.parquet",
    }
