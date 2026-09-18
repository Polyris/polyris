"""Batch Fargate container code — reads compute_ecs via xcom.get, pushes report path.

Bundle this into a Python container image that runs as the container of the
Batch JobDefinition ``polyris-test-xcom-all-render``. Dockerfile is identical
to the ECS one — Batch also runs Docker containers under Fargate::

    FROM python:3.12-slim
    RUN pip install polyris==1.0.0
    COPY render.py /app/render.py
    CMD ["python", "/app/render.py"]

Runtime env vars the polyris wrapper injects into the container via
``ContainerProperties.Environment`` (0.100.0+):
    POLYRIS_PIPELINE_NAME
    POLYRIS_RUN_DATE
    POLYRIS_TOKENS_TABLE
    POLYRIS_TASK_NAME
    POLYRIS_WRAPPER_RUN_ID

IAM (on the Batch job role):
    - PolyrisTaskReadPolicy
    - PolyrisTaskWritePolicy

Task Detail Output tab after run (green card):
    {"report_path": "s3://polyris-lake/reports/2026-09-18/report-63.0.pdf"}
"""
import os

from polyris import xcom


def main():
    run_date = os.environ.get("POLYRIS_RUN_DATE", "unknown")

    metrics_out = xcom.get(None, "compute_ecs")
    # metrics_out = {"metrics": {"score": N, "source": "compute_ecs"}}
    score = metrics_out["metrics"]["score"]

    report_path = f"s3://polyris-lake/reports/{run_date}/report-{score}.pdf"
    xcom.push({"report_path": report_path, "score": score})


if __name__ == "__main__":
    main()
